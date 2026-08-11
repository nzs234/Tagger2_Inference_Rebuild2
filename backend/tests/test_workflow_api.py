"""Contract tests for the mounted Dataset Workflow API surface."""

import json
import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image


@pytest.fixture()
def workflow_client(monkeypatch):
    """Build the real application against a throwaway data directory."""
    from backend.tagger2.config import AppConfig, reset_settings_cache
    from backend.tagger2.main import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "input").mkdir()
        (root / "output").mkdir()
        (root / "models").mkdir()

        monkeypatch.setenv("TAGGER2_DATA_DIR", str(root / "data"))
        monkeypatch.setenv("TAGGER2_CACHE_DIR", str(root / "cache"))
        monkeypatch.setenv("TAGGER2_LOG_DIR", str(root / "logs"))
        monkeypatch.setenv("TAGGER2_ALLOW_LAN", "0")
        reset_settings_cache()

        settings = AppConfig.from_env()
        settings.ensure_directories()
        app = create_app(settings)
        runtime = app.state.runtime
        input_root = runtime.allowlist.register(
            root / "input", kind="input", root_id="wfinput", label="Workflow input"
        )
        runtime.allowlist.register(
            root / "output", kind="output", root_id="wfoutput", label="Workflow output", writable=True
        )

        with TestClient(app) as client:
            yield client, root, input_root

        reset_settings_cache()


def test_workflow_routes_are_mounted(workflow_client):
    """Capabilities and resource listing answer on the versioned prefix."""
    client, _root, _input_root = workflow_client

    response = client.get("/api/v1/workflows/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["profiles"] == ["e621", "danbooru"]
    assert body["work_modes"] == ["in_place", "full_copy"]

    listing = client.get("/api/v1/workflows/resources")
    assert listing.status_code == 200
    assert listing.json() == []


def test_existing_routes_still_answer(workflow_client):
    """Mounting the workflow router must not shadow the existing API."""
    client, _root, _input_root = workflow_client

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    jobs = client.get("/api/v1/jobs")
    assert jobs.status_code == 200


def test_resource_import_requires_allowlisted_path(workflow_client):
    """A client cannot name an arbitrary absolute path for import."""
    client, _root, _input_root = workflow_client

    response = client.post(
        "/api/v1/workflows/resources/import/preview",
        json={
            "root_id": "wfinput",
            "relative_path": "../escape.csv",
            "resource_id": "replace-escape-v1",
            "category": "replace",
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "path_not_allowed"


def test_resource_import_preview_then_apply(workflow_client):
    """Preview reports the real rule breakdown; apply registers the resource."""
    client, root, _input_root = workflow_client

    csv_path = root / "input" / "index.csv"
    csv_path.write_text(
        "source_tag,canonical_e621_tag,action,replacement_tags\n"
        "male,male,pass,male\n"
        "anthro,anthro,replace,furry\n"
        "junk,junk,drop,\n",
        encoding="utf-8",
    )

    payload = {
        "root_id": "wfinput",
        "relative_path": "index.csv",
        "resource_id": "replace-e621-local-v1",
        "category": "replace",
    }

    preview = client.post("/api/v1/workflows/resources/import/preview", json=payload)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["valid"] is True
    assert body["rule_count"] == 2
    assert body["passthrough_count"] == 1
    assert body["fingerprint"]

    applied = client.post("/api/v1/workflows/resources/import/apply", json=payload)
    assert applied.status_code == 200, applied.text
    assert applied.json()["resource_id"] == "replace-e621-local-v1"

    listing = client.get("/api/v1/workflows/resources").json()
    assert [item["resource_id"] for item in listing] == ["replace-e621-local-v1"]

    # Re-previewing now warns that the id is taken instead of failing silently.
    again = client.post("/api/v1/workflows/resources/import/preview", json=payload).json()
    assert any("already registered" in warning for warning in again["warnings"])


def test_resource_import_rejects_invalid_index(workflow_client):
    """An index with a bad row is rejected with its line number, not repaired."""
    client, root, _input_root = workflow_client

    csv_path = root / "input" / "bad.csv"
    csv_path.write_text(
        "source_tag,canonical_e621_tag,action,replacement_tags\n"
        "male,male,pass,female\n",
        encoding="utf-8",
    )

    payload = {
        "root_id": "wfinput",
        "relative_path": "bad.csv",
        "resource_id": "replace-bad-v1",
        "category": "replace",
    }

    preview = client.post("/api/v1/workflows/resources/import/preview", json=payload).json()
    assert preview["valid"] is False
    assert "line 2" in preview["errors"][0]

    applied = client.post("/api/v1/workflows/resources/import/apply", json=payload)
    assert applied.status_code == 400
    assert applied.json()["code"] == "validation_failed"


def test_classify_snapshot_import_preview_then_apply(workflow_client):
    """A classification snapshot imports through its own reader, not the CSV one."""
    client, root, _input_root = workflow_client

    snapshot = {
        "format": "classify-snapshot-v1",
        "profile": "e621",
        "source": {"url": "https://e621.net/db_export/"},
        "tags": [
            {"name": "solo", "category": "general", "post_count": 10},
            {"name": "hatsune_miku", "category": "character", "post_count": 20},
        ],
        "aliases": [{"antecedent_name": "1girl", "consequent_name": "solo"}],
        "implications": [],
    }
    (root / "input" / "classify.json").write_text(json.dumps(snapshot), encoding="utf-8")

    payload = {
        "root_id": "wfinput",
        "relative_path": "classify.json",
        "resource_id": "classify-e621-test-v1",
        "category": "classify",
    }

    preview = client.post("/api/v1/workflows/resources/import/preview", json=payload)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["valid"] is True, body["errors"]
    assert body["profile"] == "e621"
    assert body["tag_count"] == 2
    assert body["alias_count"] == 1
    assert body["rule_count"] == 2
    assert body["category_counts"]["character"] == 1

    applied = client.post("/api/v1/workflows/resources/import/apply", json=payload)
    assert applied.status_code == 200, applied.text
    assert applied.json()["resource_id"] == "classify-e621-test-v1"

    listing = client.get("/api/v1/workflows/resources?category=classify").json()
    assert [item["resource_id"] for item in listing] == ["classify-e621-test-v1"]


def test_classify_snapshot_import_rejects_bad_snapshot(workflow_client):
    """A snapshot with an out-of-profile category is refused with its index."""
    client, root, _input_root = workflow_client

    snapshot = {
        "format": "classify-snapshot-v1",
        "profile": "danbooru",
        "tags": [{"name": "human", "category": "species", "post_count": 1}],
        "aliases": [],
        "implications": [],
    }
    (root / "input" / "bad_classify.json").write_text(json.dumps(snapshot), encoding="utf-8")

    payload = {
        "root_id": "wfinput",
        "relative_path": "bad_classify.json",
        "resource_id": "classify-danbooru-bad-v1",
        "category": "classify",
    }

    preview = client.post("/api/v1/workflows/resources/import/preview", json=payload).json()
    assert preview["valid"] is False
    assert any("tags[0]" in error for error in preview["errors"])

    applied = client.post("/api/v1/workflows/resources/import/apply", json=payload)
    assert applied.status_code == 400
    assert applied.json()["code"] == "validation_failed"


def test_resource_import_rejects_unknown_category(workflow_client):
    """An unknown category is refused rather than read as an arbitrary format."""
    client, root, _input_root = workflow_client

    (root / "input" / "mystery.bin").write_text("whatever", encoding="utf-8")

    payload = {
        "root_id": "wfinput",
        "relative_path": "mystery.bin",
        "resource_id": "mystery-v1",
        "category": "mystery",
    }

    preview = client.post("/api/v1/workflows/resources/import/preview", json=payload).json()
    assert preview["valid"] is False
    assert any("unsupported resource category" in error for error in preview["errors"])


def test_job_preflight_rejects_overlapping_roots(workflow_client):
    """Preflight refuses a job whose output sits inside its source."""
    client, root, _input_root = workflow_client
    (root / "input" / "nested").mkdir()

    response = client.post(
        "/api/v1/workflows/jobs/preflight",
        json={
            "profile": "e621",
            "work_mode": "full_copy",
            "overwrite_mode": "incremental",
            "source_root": {"root_id": "wfinput", "relative_path": ""},
            "output_root": {"root_id": "wfinput", "relative_path": "nested"},
            "caption": {"enabled": False},
            "classify": {"enabled": False},
            "replace": {"enabled": False},
            "ocr": {"enabled": False},
            "token_budget": {"enabled": False},
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "preflight_failed"
    assert "overlap" in body["message"].lower()


def test_job_lifecycle_returns_no_absolute_paths(workflow_client):
    """Creating and reading a job never leaks a server absolute path."""
    client, root, _input_root = workflow_client

    config = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "wfinput", "relative_path": ""},
        "output_root": {"root_id": "wfoutput", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "token_budget": {"enabled": False},
    }

    created = client.post("/api/v1/workflows/jobs", json={"config": config})
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    assert created.json()["status"] == "pending"

    status = client.get(f"/api/v1/workflows/jobs/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["job_id"] == job_id
    assert body["total_samples"] == 0
    serialized = status.text
    assert str(root) not in serialized
    assert "workspace_path" not in body

    issues = client.get(f"/api/v1/workflows/jobs/{job_id}/issues")
    assert issues.status_code == 200
    assert issues.json() == []

    missing = client.get("/api/v1/workflows/jobs/does-not-exist")
    assert missing.status_code == 404


def _seed_count_review(client, root, runtime):
    """Create a job with two samples and seeded count decisions."""
    from backend.tagger2.workflow.count_review import (
        CountReviewStore,
        create_wiki_catalog,
        derive_count_decisions,
    )

    config = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "wfinput", "relative_path": ""},
        "output_root": {"root_id": "wfoutput", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "token_budget": {"enabled": False},
    }
    # Create the job row directly: POST /jobs schedules a background run that
    # would finish the job before the lifecycle assertions can observe it.
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1

    database = runtime.workflow_database
    job_config = WorkflowJobConfigV1.from_payload(config)
    job_id, _workspace = database.create_job(
        config_json=config,
        config_hash=job_config.config_hash(),
        profile=job_config.profile,
        work_mode=job_config.work_mode,
        overwrite_mode=job_config.overwrite_mode,
        source_root_id=job_config.source_root.root_id,
        output_root_id=job_config.output_root.root_id if job_config.output_root else None,
        workspace_root=database.db_path.parent / "jobs",
    )
    database.create_sample(job_id, 0, "a.png", "png")
    database.create_sample(job_id, 1, "b.png", "png")

    class Sample:
        def __init__(self, sample_id, path):
            self.sample_id = sample_id
            self.relative_image_path = path

    evidence = derive_count_decisions(
        [Sample(0, "a.png"), Sample(1, "b.png")],
        {
            "a.png": {"count": "solo", "tags": [], "character": ""},
            "b.png": {"count": "duo", "tags": [], "character": ""},
        },
        wiki_db_path=create_wiki_catalog(root / "wiki.sqlite3"),
    )
    CountReviewStore(database, job_id).initialize(evidence)
    return job_id


def test_count_review_lists_pending_decisions(workflow_client):
    """The review page exposes the proposal plus its evidence."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_count_review(client, root, runtime)

    response = client.get(f"/api/v1/workflows/jobs/{job_id}/count-review")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending"] == 2
    assert [item["sample_id"] for item in body["items"]] == [0, 1]
    first = body["items"][0]
    assert first["proposed_count"] == "solo"
    assert first["status"] == "pending"
    assert "selected_source" in first
    # No absolute server path is exposed.
    assert str(root) not in response.text


def test_count_review_resolve_and_confirm_flow(workflow_client):
    """Export is gated until every decision is reviewed and confirmed."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_count_review(client, root, runtime)

    early = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/confirm",
        json={"confirmed": True},
    )
    assert early.status_code == 409
    assert early.json()["code"] == "count_review_incomplete"

    first = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/resolve",
        json={"sample_id": 0, "expected_version": 1, "count": "solo"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2

    batch = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/resolve-batch",
        json={"items": [{"sample_id": 1, "expected_version": 1, "count": "trio"}]},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["pending"] == 0

    confirmed = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/confirm",
        json={"confirmed": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] is True


def test_count_review_rejects_stale_version_and_bad_input(workflow_client):
    """A stale version is a conflict; an invalid count is a client error."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_count_review(client, root, runtime)

    client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/resolve",
        json={"sample_id": 0, "expected_version": 1, "count": "solo"},
    )
    stale = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/resolve",
        json={"sample_id": 0, "expected_version": 1, "count": "duo"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "count_review_conflict"

    invalid = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/resolve",
        json={"sample_id": 1, "expected_version": 1, "count": "many"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "count_review_invalid"

    refused = client.post(
        f"/api/v1/workflows/jobs/{job_id}/count-review/confirm",
        json={"confirmed": False},
    )
    assert refused.status_code == 400
    assert refused.json()["code"] == "count_review_not_confirmed"


def test_count_review_unknown_job_is_404(workflow_client):
    client, _root, _input_root = workflow_client
    response = client.get("/api/v1/workflows/jobs/nope/count-review")
    assert response.status_code == 404


def test_job_pause_resume_and_repair(workflow_client):
    """Lifecycle transitions are enforced and repair reports journal state."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_count_review(client, root, runtime)

    # A pending job cannot be paused before it runs.
    early = client.post(f"/api/v1/workflows/jobs/{job_id}/pause")
    assert early.status_code == 409
    assert early.json()["code"] == "invalid_transition"

    assert client.post(f"/api/v1/workflows/jobs/{job_id}/resume").json()["status"] == "running"
    assert client.post(f"/api/v1/workflows/jobs/{job_id}/pause").json()["status"] == "paused"
    assert client.post(f"/api/v1/workflows/jobs/{job_id}/resume").json()["status"] == "running"

    repaired = client.post(f"/api/v1/workflows/jobs/{job_id}/repair")
    assert repaired.status_code == 200, repaired.text
    body = repaired.json()
    assert body["journal_state"] == "no_commit_attempted"
    assert body["reclaimed_samples"] == 0
    assert body["resumable_samples"] == 2
    # Repair must not leak the workspace path.
    assert str(root) not in repaired.text


def test_job_list_does_not_leak_server_internals(workflow_client):
    """`GET /jobs` returns the public summary, not raw `workflow_jobs` rows."""
    client, root, runtime = (
        workflow_client[0],
        workflow_client[1],
        workflow_client[0].app.state.runtime,
    )
    job_id = _seed_count_review(client, root, runtime)

    response = client.get("/api/v1/workflows/jobs")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows, "expected the seeded job to be listed"

    row = next(item for item in rows if item["job_id"] == job_id)
    # Server-side detail must not cross the API boundary.
    for leaked in ("workspace_path", "config_json", "config_hash", "config_version"):
        assert leaked not in row, f"{leaked} leaked into the job list"
    # The public summary reports a code, never a free-form exception string.
    assert "error" not in row
    assert "error_code" in row


def test_failed_job_reports_a_code_and_keeps_the_traceback_server_side(workflow_client):
    """A crash yields a stable code to the client and a log in the workspace."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    database = runtime.workflow_database

    job_id = _seed_count_review(client, root, runtime)
    job = database.get_job(job_id)

    from backend.tagger2.workflow.api import _public_error_code
    from backend.tagger2.workflow.pipeline import PipelineError

    # The mapping is what the failure path relies on.
    assert _public_error_code(PipelineError("boom")) == "pipeline_failed"
    assert _public_error_code(RuntimeError("boom")) == "internal_error"

    # Simulate the recorded outcome of a crashed run.
    database.update_job_status(job_id, status="failed", error="pipeline_failed")

    status = client.get(f"/api/v1/workflows/jobs/{job_id}")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["error_code"] == "pipeline_failed"
    assert "Traceback" not in json.dumps(body)
    # The workspace path itself is never handed to the client.
    assert job["workspace_path"] not in json.dumps(body)


def test_pipeline_does_not_run_on_the_event_loop(workflow_client, monkeypatch):
    """The synchronous pipeline must execute off the event loop.

    `run_offline_pipeline` imports the dataset, runs every stage and commits,
    all synchronously. Executing it on the loop stalls every other endpoint for
    the whole run, which is what made status and pause unusable during a real
    job.

    The discriminator is `asyncio.get_running_loop()`: called on the loop thread
    it returns the loop, while a worker thread created by `asyncio.to_thread`
    has no running loop and raises. Comparing thread ids would not work here,
    because TestClient already drives the app from its own thread.
    """
    import asyncio

    client, root, _input_root = workflow_client
    observed: dict[str, bool] = {}

    import backend.tagger2.workflow.pipeline as pipeline_module

    real_pipeline = pipeline_module.run_offline_pipeline

    def _recording_pipeline(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            observed["on_event_loop"] = False
        else:
            observed["on_event_loop"] = True
        return real_pipeline(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "run_offline_pipeline", _recording_pipeline)

    (root / "input" / "sample.txt").write_text("solo", encoding="utf-8")
    Image.new("RGB", (32, 32), color="white").save(root / "input" / "sample.png")

    config = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "wfinput", "relative_path": ""},
        "output_root": {"root_id": "wfoutput", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    }

    created = client.post("/api/v1/workflows/jobs", json={"config": config})
    assert created.status_code == 200, created.text

    assert observed, "pipeline was never invoked"
    assert observed["on_event_loop"] is False, (
        "pipeline ran on the event loop, which blocks every other request"
    )


def test_lifecycle_unknown_job_is_404(workflow_client):
    client, _root, _input_root = workflow_client
    assert client.post("/api/v1/workflows/jobs/nope/pause").status_code == 404
    assert client.post("/api/v1/workflows/jobs/nope/repair").status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def _seed_token_review(client, root, runtime):
    """Create a job with one overflowing caption awaiting review."""
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewStore

    database = runtime.workflow_database
    job_id, _workspace = database.create_job(
        config_json={},
        config_hash="h",
        profile="e621",
        work_mode="full_copy",
        overwrite_mode="incremental",
        source_root_id="wfinput",
        output_root_id="wfoutput",
        workspace_root=root / "jobs",
    )
    database.create_sample(job_id, 0, "a.png", "png")
    TokenBudgetReviewStore(database, job_id).initialize(
        [{"sample_id": 0, "nl_text": "a b c d e", "token_count": 5, "token_limit": 3}]
    )
    return job_id


def test_token_review_lists_overflowing_samples(workflow_client):
    """The review page reports the caption, the budget and the margin."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_token_review(client, root, runtime)

    response = client.get(f"/api/v1/workflows/jobs/{job_id}/token-review")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unresolved"] == 1
    (item,) = body["items"]
    assert item["status"] == "overflow"
    assert item["token_limit"] == 3
    assert item["over_by"] == 2
    assert item["proposal_text"] is None
    # No absolute server path is exposed.
    assert str(root) not in response.text


def test_token_review_counting_actions_fail_closed_without_a_tokenizer(workflow_client):
    """Without the tokenizer resource the API reports unavailable, not a guess."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_token_review(client, root, runtime)

    response = client.post(
        f"/api/v1/workflows/jobs/{job_id}/token-review/review",
        json={"sample_id": 0, "action": "edit", "expected_status": "overflow", "text": "a b"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "token_review_unavailable"

    # The gate stays closed, so export cannot proceed.
    confirm = client.post(
        f"/api/v1/workflows/jobs/{job_id}/token-review/confirm",
        json={"confirmed": True},
    )
    assert confirm.status_code == 409
    assert confirm.json()["code"] == "token_review_incomplete"


def test_token_review_rejects_unknown_actions_and_missing_jobs(workflow_client):
    """The action vocabulary is closed and an unknown job is a 404."""
    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_token_review(client, root, runtime)

    bad = client.post(
        f"/api/v1/workflows/jobs/{job_id}/token-review/review",
        json={"sample_id": 0, "action": "truncate", "expected_status": "overflow"},
    )
    assert bad.status_code == 422

    missing = client.get("/api/v1/workflows/jobs/nope/token-review")
    assert missing.status_code == 404
    assert missing.json()["code"] == "job_not_found"


def test_token_review_apply_flow_clears_the_gate(workflow_client):
    """A counted proposal can be applied, and only then is export unblocked."""
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewStore

    client, root, _input_root = workflow_client
    runtime = client.app.state.runtime
    job_id = _seed_token_review(client, root, runtime)

    # Record the proposal directly: the HTTP edit path needs a tokenizer, which
    # is deliberately absent in this deployment.
    store = TokenBudgetReviewStore(runtime.workflow_database, job_id)
    store.review(
        0,
        action="edit",
        expected_status="overflow",
        text="a b",
        count_tokens=lambda texts: [2 for _ in texts],
    )

    applied = client.post(
        f"/api/v1/workflows/jobs/{job_id}/token-review/review",
        json={"sample_id": 0, "action": "apply", "expected_status": "edited"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["nl_text"] == "a b"

    stale = client.post(
        f"/api/v1/workflows/jobs/{job_id}/token-review/review",
        json={"sample_id": 0, "action": "apply", "expected_status": "edited"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "token_review_conflict"

    confirm = client.post(
        f"/api/v1/workflows/jobs/{job_id}/token-review/confirm",
        json={"confirmed": True},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["unresolved"] == 0
