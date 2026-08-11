"""Tests for policy and token budget wired into the pipeline."""

import json
import tempfile
from pathlib import Path

import pytest
from PIL import Image


def _image(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)


def _config(**sections):
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1

    payload = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    }
    payload.update(sections)
    return WorkflowJobConfigV1.from_payload(payload)


def _policy(**overrides):
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, PolicyConfig

    defaults = dict(
        seed="seed-1",
        artistEnabled=True,
        artistDropoutProbability=0.0,
        qualityEnabled=False,
        qualityDropoutProbability=0.0,
        appearanceNlEnabled=False,
        solo=CoupledProbabilities(0.0, 0.0),
        nonSolo=CoupledProbabilities(0.0, 0.0),
        unknown=CoupledProbabilities(0.0, 0.0),
    )
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def test_pipeline_applies_policy_artist_from_directory():
    """Policy appends the artist parsed from the number_artist directory."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output = root / "src", root / "out"
        output.mkdir()
        _image(source / "12_studio" / "a.png")
        (source / "12_studio" / "a.txt").write_text("male, anthro", encoding="utf-8")

        config = _config(recursive=True)
        report = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=root / "ws",
            policy_config=_policy(),
        )

        assert report.exported_samples == 1
        assert report.policy["artist_dropped"] == 0
        payload = json.loads((output / "12_studio" / "a.json").read_text(encoding="utf-8"))
        assert payload["artist"] == "@studio"


def test_pipeline_policy_dropout_is_reproducible():
    """The same seed produces the same dataset across runs."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    def run(seed, out_name, ws_name, root):
        source = root / "src"
        output = root / out_name
        output.mkdir()
        return run_offline_pipeline(
            _config(recursive=True),
            source_root=source,
            output_root=output,
            workspace=root / ws_name,
            policy_config=_policy(seed=seed, artistDropoutProbability=0.5),
        ), output

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for index in range(12):
            _image(root / "src" / f"{index}_artist{index}" / "a.png")
            (root / "src" / f"{index}_artist{index}" / "a.txt").write_text("male", encoding="utf-8")

        first, out_a = run("fixed-seed", "out_a", "ws_a", root)
        second, out_b = run("fixed-seed", "out_b", "ws_b", root)
        third, out_c = run("other-seed", "out_c", "ws_c", root)

        assert first.policy["artist_dropped"] == second.policy["artist_dropped"]

        def artists(output):
            return {
                path.relative_to(output).as_posix(): json.loads(path.read_text(encoding="utf-8"))["artist"]
                for path in sorted(output.rglob("*.json"))
            }

        assert artists(out_a) == artists(out_b)
        # A different seed changes at least one decision across 12 samples.
        assert artists(out_a) != artists(out_c)
        assert third.policy["artist_dropped"] >= 0


def test_pipeline_policy_failure_blocks_commit():
    """An image outside a number_artist directory blocks rather than guessing."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output = root / "src", root / "out"
        source.mkdir()
        output.mkdir()
        _image(source / "a.png")
        (source / "a.txt").write_text("male", encoding="utf-8")

        report = run_offline_pipeline(
            _config(),
            source_root=source,
            output_root=output,
            workspace=root / "ws",
            policy_config=_policy(),
        )

        assert report.failed_samples == 1
        assert report.committed_files == 0
        assert list(output.iterdir()) == []
        assert any(issue.module_id == "policy" for issue in report.issues)


def test_pipeline_token_budget_trims_and_records():
    """Token budget trims within the frozen field order and reports the status."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output = root / "src", root / "out"
        source.mkdir()
        output.mkdir()
        _image(source / "a.png")
        # A standard JSON payload keeps non-trimmable fields (count, character),
        # so trimming cannot empty the payload entirely.
        (source / "a.json").write_text(
            json.dumps(
                {
                    "quality": ["masterpiece"],
                    "count": "solo",
                    "character": "rex",
                    "series": "",
                    "artist": "",
                    "appearance": ["blue_fur"],
                    "tags": ["male", "standing"],
                    "environment": ["forest", "snow"],
                    "nl": "",
                }
            ),
            encoding="utf-8",
        )

        report = run_offline_pipeline(
            _config(token_budget={"enabled": True, "max_tokens": 3}),
            source_root=source,
            output_root=output,
            workspace=root / "ws",
            token_counter=lambda texts: [len(t.decode("utf-8").split()) for t in texts],
        )

        assert report.exported_samples == 1
        assert report.token_budget.get("trimmed") == 1
        payload = json.loads((output / "a.json").read_text(encoding="utf-8"))
        # Quality is trimmed before environment, tags and appearance.
        assert payload["quality"] == []
        assert payload["count"] == "solo"
        assert payload["character"] == "rex"


def test_pipeline_token_budget_overflow_blocks_commit():
    """An impossible budget is a blocking issue, never a silent truncation."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output = root / "src", root / "out"
        source.mkdir()
        output.mkdir()
        _image(source / "a.png")
        (source / "a.json").write_text(
            json.dumps(
                {
                    "quality": [],
                    "count": "solo",
                    "character": "",
                    "series": "",
                    "artist": "",
                    "appearance": [],
                    "tags": [],
                    "environment": [],
                    "nl": "A sentence that cannot be trimmed because nl is not trimmable.",
                }
            ),
            encoding="utf-8",
        )

        report = run_offline_pipeline(
            _config(token_budget={"enabled": True, "max_tokens": 2}),
            source_root=source,
            output_root=output,
            workspace=root / "ws",
            token_counter=lambda texts: [len(t.decode("utf-8").split()) for t in texts],
        )

        assert report.failed_samples == 1
        assert report.committed_files == 0
        assert report.token_budget.get("overflow") == 1
        assert any(issue.code == "token_budget_overflow" for issue in report.issues)


def test_pipeline_token_budget_empty_payload_is_overflow():
    """A budget so small that trimming empties the payload is an overflow."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output = root / "src", root / "out"
        source.mkdir()
        output.mkdir()
        _image(source / "a.png")
        # Tag-only payload: trimming tags to zero leaves nothing to serialize.
        (source / "a.txt").write_text("male, anthro, forest", encoding="utf-8")

        report = run_offline_pipeline(
            _config(token_budget={"enabled": True, "max_tokens": 1}),
            source_root=source,
            output_root=output,
            workspace=root / "ws",
            token_counter=lambda texts: [len(t.decode("utf-8").split()) + 5 for t in texts],
        )

        assert report.failed_samples == 1
        assert report.committed_files == 0
        assert report.token_budget.get("overflow") == 1
        assert any(issue.code == "token_budget_overflow" for issue in report.issues)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
