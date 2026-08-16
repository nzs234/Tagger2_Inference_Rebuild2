"""End-to-end regression tests for immutable review continuations."""

import json
import tempfile
from pathlib import Path

from PIL import Image
import pytest


def _config(
    *,
    count_review: bool = True,
    policy: bool = True,
    token_budget: bool = False,
):
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV2

    return WorkflowJobConfigV2.from_payload(
        {
            "profile": "e621",
            "work_mode": "full_copy",
            "overwrite_mode": "incremental",
            "source_root": {"root_id": "in", "relative_path": ""},
            "output_root": {"root_id": "out", "relative_path": ""},
            "recursive": False,
            "caption": {"enabled": True, "model_id": "test-model"},
            "classify": {"enabled": False},
            "replace": {"enabled": True, "resource_id": "replace-test"},
            "ocr": {"enabled": False},
            "nl": {
                "enabled": True,
                "api_enabled": True,
                "reuse_original_nl": False,
                "use_image": False,
            },
            "count_review": {"enabled": count_review},
            "policy": {"enabled": policy},
            "token_budget": {"enabled": token_budget, "max_tokens": 2},
            "export": {"format": "json"},
        }
    )


class _Predictor:
    def __init__(self):
        self.calls = 0

    def predict_tags(self, _path):
        from backend.tagger2.workflow.stages.caption import CaptionTag

        self.calls += 1
        return [CaptionTag("anthro", 0.99)]


class _NlClient:
    def __init__(self):
        self.calls = 0

    def complete(self, _request):
        self.calls += 1
        return json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "nl": "A reviewed caption.",
                                    "count": "solo",
                                    "layout": "single_scene",
                                    "sameCharacterRepeated": False,
                                }
                            )
                        },
                    }
                ]
            }
        ).encode("utf-8")


def test_review_continuation_freezes_upstream_and_applies_count_before_policy(monkeypatch):
    from backend.tagger2.workflow.count_review import CountReviewStore
    from backend.tagger2.workflow.db import WorkflowDatabase
    from backend.tagger2.workflow.pipeline import run_offline_pipeline
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, PolicyConfig

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output, workspace = root / "source", root / "output", root / "workspace"
        source.mkdir()
        output.mkdir()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(source / "a.png")
        replacement = root / "replace.csv"
        replacement.write_text(
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "anthro,anthro,replace,anthro\n",
            encoding="utf-8",
        )

        config = _config()
        database = WorkflowDatabase(root / "workflows.sqlite3")
        job_id, _ = database.create_job(
            config_json=config.to_dict(),
            config_hash=config.config_hash(),
            profile=config.profile,
            work_mode=config.work_mode,
            overwrite_mode=config.overwrite_mode,
            source_root_id="in",
            output_root_id="out",
            workspace_root=root / "jobs",
        )
        database.update_job_status(job_id, "running", expected_status="pending")

        policy_calls: list[str] = []
        import backend.tagger2.workflow.pipeline as pipeline_module

        real_apply_policy = pipeline_module.apply_policy
        real_replace_projection = pipeline_module.replace_projection
        replacement_calls = 0

        def recording_policy(*args, **kwargs):
            projection = args[0]
            policy_calls.append(str(projection.get("count")))
            return real_apply_policy(*args, **kwargs)

        def recording_replacement(*args, **kwargs):
            nonlocal replacement_calls
            replacement_calls += 1
            return real_replace_projection(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, "apply_policy", recording_policy)
        monkeypatch.setattr(pipeline_module, "replace_projection", recording_replacement)
        predictor = _Predictor()
        nl_client = _NlClient()
        policy = PolicyConfig(
            seed="review-test",
            artistEnabled=False,
            artistDropoutProbability=0.0,
            qualityEnabled=False,
            qualityDropoutProbability=0.0,
            appearanceNlEnabled=True,
            solo=CoupledProbabilities(1.0, 0.0),
            nonSolo=CoupledProbabilities(0.0, 0.0),
            unknown=CoupledProbabilities(0.0, 0.0),
        )

        first = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=replacement,
            tag_predictor=predictor,
            nl_client=nl_client,
            policy_config=policy,
            database=database,
            job_id=job_id,
        )

        assert first.committed_files == 0
        assert predictor.calls == 1
        assert nl_client.calls == 1
        assert replacement_calls == 1
        assert policy_calls == []
        assert (workspace / "checkpoints" / "count_review.json").is_file()

        store = CountReviewStore(database, job_id)
        row = store.page()[0]
        store.resolve(row["sample_id"], expected_version=1, count="solo")

        second = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=replacement,
            tag_predictor=predictor,
            nl_client=nl_client,
            policy_config=policy,
            database=database,
            job_id=job_id,
        )

        assert second.committed_files == 2
        assert predictor.calls == 1
        assert nl_client.calls == 1
        assert replacement_calls == 1
        artifacts = database.list_artifacts(job_id, kind="staged_export")
        assert {item["relative_path"] for item in artifacts} == {"a.json", "a.png"}
        assert policy_calls == ["solo"]
        payload = json.loads((output / "a.json").read_text(encoding="utf-8"))
        assert payload["count"] == "solo"


def test_checkpoint_tampering_fails_closed():
    from backend.tagger2.workflow.projection_checkpoint import (
        ProjectionCheckpointError,
        load_projection_checkpoint,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        target = workspace / "checkpoints"
        target.mkdir()
        (target / "count_review.json").write_text('{"digest":"bad"}', encoding="utf-8")
        try:
            load_projection_checkpoint(
                workspace,
                job_id="job",
                config_hash="config",
                resource_fingerprints={},
            )
        except ProjectionCheckpointError as exc:
            assert "digest" in str(exc)
        else:
            raise AssertionError("tampered checkpoint was accepted")


def test_checkpoint_disk_full_fails_closed_and_cleans_partial(monkeypatch):
    from backend.tagger2.workflow.projection_checkpoint import (
        ProjectionCheckpointError,
        write_projection_checkpoint,
    )

    class Sample:
        sample_id = 1
        relative_image_path = "a.png"
        annotation_key = "a"
        image_format = "png"

    with tempfile.TemporaryDirectory() as tmpdir:
        import backend.tagger2.workflow.projection_checkpoint as checkpoint_module

        def no_space(_stream):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(checkpoint_module.os, "fsync", no_space)
        with pytest.raises(ProjectionCheckpointError, match="could not be written"):
            write_projection_checkpoint(
                Path(tmpdir),
                stage_cursor="projection",
                job_id="job",
                config_hash="hash",
                resource_fingerprints={},
                samples=[Sample()],
                projections={
                    "1": {
                        "quality": [],
                        "count": "",
                        "character": "",
                        "series": "",
                        "artist": "",
                        "appearance": [],
                        "tags": [],
                        "environment": [],
                        "nl": "",
                    }
                },
                report={},
            )
        assert not (Path(tmpdir) / "checkpoints" / "projection.json.partial").exists()


def test_token_review_continuation_only_applies_reviewed_nl(monkeypatch):
    from backend.tagger2.workflow.db import WorkflowDatabase
    from backend.tagger2.workflow.pipeline import run_offline_pipeline
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, PolicyConfig
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewStore

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output, workspace = root / "source", root / "output", root / "workspace"
        source.mkdir()
        output.mkdir()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(source / "a.png")
        replacement = root / "replace.csv"
        replacement.write_text(
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "anthro,anthro,replace,anthro\n",
            encoding="utf-8",
        )

        config = _config(count_review=False, token_budget=True)
        database = WorkflowDatabase(root / "workflows.sqlite3")
        job_id, _ = database.create_job(
            config_json=config.to_dict(),
            config_hash=config.config_hash(),
            profile=config.profile,
            work_mode=config.work_mode,
            overwrite_mode=config.overwrite_mode,
            source_root_id="in",
            output_root_id="out",
            workspace_root=root / "jobs",
        )
        database.update_job_status(job_id, "running", expected_status="pending")

        import backend.tagger2.workflow.pipeline as pipeline_module

        real_apply_policy = pipeline_module.apply_policy
        real_replace_projection = pipeline_module.replace_projection
        policy_calls = 0
        replacement_calls = 0

        def recording_policy(*args, **kwargs):
            nonlocal policy_calls
            policy_calls += 1
            return real_apply_policy(*args, **kwargs)

        def recording_replacement(*args, **kwargs):
            nonlocal replacement_calls
            replacement_calls += 1
            return real_replace_projection(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, "apply_policy", recording_policy)
        monkeypatch.setattr(pipeline_module, "replace_projection", recording_replacement)
        predictor = _Predictor()
        nl_client = _NlClient()
        policy_config = PolicyConfig(
            seed="token-review-test",
            artistEnabled=False,
            artistDropoutProbability=0.0,
            qualityEnabled=False,
            qualityDropoutProbability=0.0,
            appearanceNlEnabled=True,
            solo=CoupledProbabilities(1.0, 0.0),
            nonSolo=CoupledProbabilities(0.0, 0.0),
            unknown=CoupledProbabilities(0.0, 0.0),
        )

        def count_tokens(texts):
            return [
                1
                if "ok" in (value.decode("utf-8") if isinstance(value, bytes) else value)
                else 10
                for value in texts
            ]

        first = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=replacement,
            tag_predictor=predictor,
            nl_client=nl_client,
            policy_config=policy_config,
            token_counter=count_tokens,
            database=database,
            job_id=job_id,
        )

        assert first.committed_files == 0
        assert predictor.calls == 1
        assert nl_client.calls == 1
        assert replacement_calls == 1
        assert policy_calls == 1
        assert (workspace / "checkpoints" / "token_review.json").is_file()

        store = TokenBudgetReviewStore(database, job_id)
        row = store.page()[0]
        edited = store.review(
            row["sample_id"],
            action="edit",
            expected_status="overflow",
            text="ok",
            count_tokens=count_tokens,
        )
        store.review(
            row["sample_id"],
            action="apply",
            expected_status=edited["status"],
            count_tokens=count_tokens,
        )

        second = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=replacement,
            tag_predictor=predictor,
            nl_client=nl_client,
            policy_config=policy_config,
            token_counter=count_tokens,
            database=database,
            job_id=job_id,
        )

        assert second.committed_files == 2
        assert predictor.calls == 1
        assert nl_client.calls == 1
        assert replacement_calls == 1
        assert policy_calls == 1
        payload = json.loads((output / "a.json").read_text(encoding="utf-8"))
        assert payload["nl"] == "ok"


def test_restart_resumes_from_general_projection_without_provider_calls(monkeypatch):
    from backend.tagger2.workflow.db import WorkflowDatabase
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output, workspace = root / "source", root / "output", root / "workspace"
        source.mkdir()
        output.mkdir()
        Image.new("RGB", (8, 8), (10, 20, 30)).save(source / "a.png")
        replacement = root / "replace.csv"
        replacement.write_text(
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "anthro,anthro,replace,anthro\n",
            encoding="utf-8",
        )

        config = _config(count_review=False, token_budget=False)
        database = WorkflowDatabase(root / "workflows.sqlite3")
        job_id, _ = database.create_job(
            config_json=config.to_dict(),
            config_hash=config.config_hash(),
            profile=config.profile,
            work_mode=config.work_mode,
            overwrite_mode=config.overwrite_mode,
            source_root_id="in",
            output_root_id="out",
            workspace_root=root / "jobs",
        )
        database.update_job_status(job_id, "running", expected_status="pending")
        database.update_job_status(job_id, "pausing", expected_status="running")

        import backend.tagger2.workflow.pipeline as pipeline_module

        real_replace_projection = pipeline_module.replace_projection
        replacement_calls = 0

        def recording_replacement(*args, **kwargs):
            nonlocal replacement_calls
            replacement_calls += 1
            return real_replace_projection(*args, **kwargs)

        monkeypatch.setattr(pipeline_module, "replace_projection", recording_replacement)
        predictor = _Predictor()
        nl_client = _NlClient()
        first = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=replacement,
            tag_predictor=predictor,
            nl_client=nl_client,
            database=database,
            job_id=job_id,
        )

        assert first.committed_files == 0
        assert predictor.calls == 1
        assert nl_client.calls == 1
        assert replacement_calls == 1
        assert (workspace / "checkpoints" / "projection.json").is_file()

        database.update_job_status(job_id, "running", expected_status="pausing")
        second = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=replacement,
            tag_predictor=predictor,
            nl_client=nl_client,
            database=database,
            job_id=job_id,
        )

        assert second.committed_files == 2
        assert predictor.calls == 1
        assert nl_client.calls == 1
        assert replacement_calls == 1
