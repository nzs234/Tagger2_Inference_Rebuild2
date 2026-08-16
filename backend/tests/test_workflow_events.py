"""Durable workflow control-plane event and restart-recovery tests."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from tagger2.security import PathAllowlist
from tagger2.workflow.api import create_workflow_router
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.resources import WorkflowResourceCatalog


def _database(tmp_path: Path) -> tuple[WorkflowDatabase, str]:
    database = WorkflowDatabase(tmp_path / "workflow.sqlite3")
    job_id, _ = database.create_job(
        config_json={"source_root": {"relative_path": "images"}},
        config_hash="hash",
        profile="e621",
        work_mode="full_copy",
        overwrite_mode="incremental",
        source_root_id="input",
        output_root_id="output",
        workspace_root=tmp_path / "jobs",
    )
    return database, job_id


def test_status_and_sample_events_are_replayable(tmp_path: Path):
    database, job_id = _database(tmp_path)
    assert database.start_job(job_id)
    database.create_sample(job_id, 1, "images/a.png", "png")
    database.update_sample_status(job_id, 1, "processing", current_module_id="caption")
    assert database.update_job_status(job_id, "running", expected_status="queued")

    events = database.list_events(job_id)
    assert [event["event_type"] for event in events[:2]] == [
        "job_created",
        "status_changed",
    ]
    assert any(event["event_type"] == "sample_created" for event in events)
    assert any(event["event_type"] == "sample_status_changed" for event in events)
    assert events[-1]["event_id"] > events[0]["event_id"]

    cursor = events[1]["event_id"]
    replay = database.list_events(job_id, after_event_id=cursor)
    assert replay
    assert all(event["event_id"] > cursor for event in replay)


def test_restart_marks_only_inflight_jobs_interrupted(tmp_path: Path):
    database, job_id = _database(tmp_path)
    assert database.start_job(job_id)
    assert database.update_job_status(job_id, "running", expected_status="queued")

    interrupted = database.mark_interrupted_jobs()
    assert interrupted == [job_id]
    assert database.get_job(job_id)["status"] == "interrupted"
    event = database.list_events(job_id)[-1]
    assert event["to_status"] == "interrupted"
    assert event["payload"] == {"reason": "process_restart"}

    # Re-running recovery is idempotent: an already interrupted job is not
    # rewritten or given duplicate restart events.
    assert database.mark_interrupted_jobs() == []
    assert database.list_events(job_id)[-1]["event_id"] == event["event_id"]


def test_pin_round_trip_is_durable(tmp_path: Path):
    database, job_id = _database(tmp_path)
    assert database.is_job_pinned(job_id) is False
    assert database.set_job_pinned(job_id, True) is True
    assert database.is_job_pinned(job_id) is True
    assert database.list_events(job_id)[-1]["event_type"] == "job_pinned"
    assert database.set_job_pinned(job_id, False) is True
    assert database.is_job_pinned(job_id) is False
    assert database.list_events(job_id)[-1]["event_type"] == "job_unpinned"


def test_events_route_validates_cursor_and_projects_payload(tmp_path: Path):
    database, job_id = _database(tmp_path)
    allowlist = PathAllowlist()
    source = tmp_path / "input"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    allowlist.register(source, kind="input", root_id="input")
    allowlist.register(output, kind="output", root_id="output", writable=True)

    app = FastAPI()
    app.include_router(
        create_workflow_router(
            allowlist,
            WorkflowResourceCatalog(tmp_path / "resources"),
            database=database,
        )
    )
    with TestClient(app) as client:
        response = client.get(f"/api/v1/workflows/jobs/{job_id}/events")
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == job_id
        assert body["events"][0]["event_type"] == "job_created"
        assert "workspace_path" not in body["events"][0]

        invalid = client.get(
            f"/api/v1/workflows/jobs/{job_id}/events?after_event_id=-1"
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "invalid_event_cursor"


def test_event_stream_replays_from_last_event_id_and_closes_when_terminal(tmp_path: Path):
    database, job_id = _database(tmp_path)
    assert database.update_job_status(job_id, "completed", expected_status="pending")
    events = database.list_events(job_id)
    first_cursor = events[0]["event_id"]
    last_cursor = events[-1]["event_id"]

    app = FastAPI()
    app.include_router(
        create_workflow_router(
            PathAllowlist(),
            WorkflowResourceCatalog(tmp_path / "resources"),
            database=database,
        )
    )
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/workflows/jobs/{job_id}/events/stream",
            headers={"Last-Event-ID": str(first_cursor)},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert f"id: {last_cursor}\n" in response.text
        assert f"id: {first_cursor}\n" not in response.text

        invalid = client.get(
            f"/api/v1/workflows/jobs/{job_id}/events/stream",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "invalid_event_cursor"
