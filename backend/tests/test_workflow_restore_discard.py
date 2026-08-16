"""Dataset lock, Restore and Discard behaviour (plan stage 7 / P0).

These endpoints were added without coverage, and an earlier revision called a
storage method that does not exist, which would have failed only at request
time. The tests below exercise the real HTTP path and a real backup archive.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tagger2.security import PathAllowlist
from tagger2.workflow.api import create_workflow_router
from tagger2.workflow.commit import write_annotation_backup
from tagger2.workflow.contracts import WorkflowJobConfigV1
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.preflight import WorkflowPreflightError, WorkflowPreflightService
from tagger2.workflow.resources import WorkflowResourceCatalog

ORIGINAL = b'{"tags": ["original"]}'


@pytest.fixture
def env(tmp_path: Path):
    source = tmp_path / "in"
    output = tmp_path / "out"
    source.mkdir()
    output.mkdir()

    allowlist = PathAllowlist()
    allowlist.register(source, kind="input", root_id="in", label="in")
    allowlist.register(output, kind="output", root_id="out", label="out", writable=True)

    database = WorkflowDatabase(tmp_path / "wf.sqlite3")
    app = FastAPI()
    app.include_router(
        create_workflow_router(
            allowlist=allowlist,
            resource_catalog=WorkflowResourceCatalog(tmp_path / "res"),
            database=database,
        )
    )
    return {
        "client": TestClient(app),
        "database": database,
        "allowlist": allowlist,
        "source": source,
        "output": output,
        "tmp_path": tmp_path,
    }


def _job(env, *, work_mode="full_copy"):
    return env["database"].create_job(
        config_json={},
        config_hash="h",
        profile="e621",
        work_mode=work_mode,
        overwrite_mode="incremental",
        source_root_id="in",
        output_root_id="out" if work_mode == "full_copy" else None,
        workspace_root=env["tmp_path"] / "jobs",
    )


def _finish(database, job_id, status="completed"):
    database.update_job_status(job_id, "running")
    database.update_job_status(job_id, status)


def _seed_backup(dataset_root: Path, workspace: Path):
    (dataset_root / "a.json").write_bytes(ORIGINAL)
    write_annotation_backup(dataset_root, workspace / "backup.zip", ["a"])
    (dataset_root / "a.json").write_bytes(b'{"tags": ["OVERWRITTEN"]}')


def test_restore_rejects_non_terminal_job(env):
    job_id, workspace = _job(env)
    _seed_backup(env["output"], workspace)

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_state_for_restore"
    # The dataset must be left untouched by a refused restore.
    assert (env["output"] / "a.json").read_bytes() != ORIGINAL


def test_restore_returns_original_annotations(env):
    job_id, workspace = _job(env)
    _seed_backup(env["output"], workspace)
    _finish(env["database"], job_id)

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")

    # Full-copy jobs never mutate the source dataset and therefore have no
    # in-place restore operation.  The output remains an independent artifact.
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "restore_not_applicable"


def test_restore_targets_source_root_for_in_place(env):
    """An in_place job wrote to the source root, so restore must go back there."""
    job_id, workspace = _job(env, work_mode="in_place")
    _seed_backup(env["source"], workspace)
    _finish(env["database"], job_id)

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")

    assert response.status_code == 200
    assert response.json()["root_id"] == "in"
    assert (env["source"] / "a.json").read_bytes() == ORIGINAL


def test_restore_retry_returns_prior_result_without_overwriting_new_changes(env):
    """HTTP retries are idempotent; an already restored dataset is not touched again."""

    job_id, workspace = _job(env, work_mode="in_place")
    _seed_backup(env["source"], workspace)
    _finish(env["database"], job_id)

    first = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")
    assert first.status_code == 200
    assert first.json()["replayed"] is False
    changed_after_restore = b'{"tags": ["edited-after-restore"]}'
    (env["source"] / "a.json").write_bytes(changed_after_restore)

    replay = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert (env["source"] / "a.json").read_bytes() == changed_after_restore
    assert env["database"].get_job(job_id)["restored_at"] is not None


def test_restore_again_requires_an_explicit_operation_id(env):
    job_id, workspace = _job(env, work_mode="in_place")
    _seed_backup(env["source"], workspace)
    _finish(env["database"], job_id)

    first = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")
    assert first.status_code == 200
    (env["source"] / "a.json").write_bytes(b'{"tags": ["changed-again"]}')

    second = env["client"].post(
        f"/api/v1/workflows/jobs/{job_id}/restore",
        json={"operation_id": "operator-restore-2"},
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is False
    assert (env["source"] / "a.json").read_bytes() == ORIGINAL
    with env["database"].connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM workflow_operations"
            " WHERE job_id = ? AND operation_type = 'restore' AND status = 'completed'",
            (job_id,),
        ).fetchone()
    assert int(row["total"]) == 2


def test_restore_failure_is_retryable_and_releases_dataset_lock(env, monkeypatch):
    """A failed restore parks the job for recovery without retaining its lock."""

    from tagger2.workflow.commit import CommitError

    job_id, workspace = _job(env, work_mode="in_place")
    _seed_backup(env["source"], workspace)
    _finish(env["database"], job_id)

    def fail_restore(*_args, **_kwargs):
        raise CommitError("simulated restore failure")

    monkeypatch.setattr("tagger2.workflow.commit.restore_annotation_backup", fail_restore)
    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "restore_failed"
    assert env["database"].get_job(job_id)["status"] == "rollback_required"
    with env["database"].connection() as conn:
        lock = conn.execute(
            "SELECT released_at FROM workflow_dataset_locks WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert lock is None or lock["released_at"] is not None


def test_restore_without_backup_is_404(env):
    job_id, _workspace = _job(env)
    _finish(env["database"], job_id)

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "restore_not_applicable"


def test_restore_response_hides_absolute_paths(env):
    job_id, workspace = _job(env)
    _seed_backup(env["output"], workspace)
    _finish(env["database"], job_id)

    body = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/restore").json()

    serialized = repr(body)
    assert str(env["tmp_path"]) not in serialized
    assert "workspace_path" not in body


def test_discard_removes_workspace(env):
    job_id, workspace = _job(env)
    _finish(env["database"], job_id)
    assert workspace.exists()

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")

    assert response.status_code == 200
    assert response.json()["discarded"] is True
    assert not workspace.exists()
    # The job row survives for audit purposes.
    job = env["database"].get_job(job_id)
    assert job is not None
    assert job["discarded_at"] is not None


def test_discard_rejects_active_job(env):
    job_id, workspace = _job(env)
    env["database"].update_job_status(job_id, "running")

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_state_for_discard"
    assert workspace.exists()


def test_discard_is_idempotent(env):
    job_id, _workspace = _job(env)
    _finish(env["database"], job_id)

    first = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")
    second = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")

    assert first.json()["discarded"] is True
    assert second.status_code == 200
    assert second.json()["discarded"] is False


def test_discard_releases_lock_from_interrupted_job(env):
    job_id, _workspace = _job(env)
    assert env["database"].start_job(job_id, expected_status="pending") is True
    env["database"].update_job_status(job_id, "running", expected_status="queued")
    env["database"].update_job_status(job_id, "interrupted", expected_status="running")

    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")

    assert response.status_code == 200
    with env["database"].connection() as conn:
        row = conn.execute(
            "SELECT released_at FROM workflow_dataset_locks WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    assert row is not None and row["released_at"] is not None


def test_discard_error_does_not_expose_server_path(env, monkeypatch):
    import shutil

    job_id, _workspace = _job(env)
    _finish(env["database"], job_id)

    def fail_remove(path):
        raise OSError(f"cannot remove {path}")

    monkeypatch.setattr(shutil, "rmtree", fail_remove)
    response = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "discard_failed"
    assert str(env["tmp_path"]) not in response.text


def test_discard_retries_cleanup_after_marker_was_persisted(env, monkeypatch):
    import shutil

    job_id, workspace = _job(env)
    _finish(env["database"], job_id)
    real_remove = shutil.rmtree
    attempts = 0

    def interrupt_once(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated power loss")
        real_remove(path)

    monkeypatch.setattr(shutil, "rmtree", interrupt_once)
    first = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")
    assert first.status_code == 500
    assert env["database"].get_job(job_id)["discarded_at"] is not None
    assert workspace.exists()

    second = env["client"].post(f"/api/v1/workflows/jobs/{job_id}/discard")
    assert second.status_code == 200
    assert second.json()["discarded"] is True
    assert not workspace.exists()


def _preflight_config():
    return WorkflowJobConfigV1.from_payload(
        {
            "profile": "e621",
            "work_mode": "full_copy",
            "overwrite_mode": "incremental",
            "source_root": {"root_id": "in", "relative_path": "."},
            "output_root": {"root_id": "out", "relative_path": "."},
            "caption": {"enabled": False},
            "classify": {"enabled": False},
            "replace": {"enabled": False},
            "ocr": {"enabled": False},
            "nl": {"enabled": False},
            "token_budget": {"enabled": False},
        }
    )


@pytest.mark.parametrize("active_status", ["running", "paused", "waiting_count_review"])
def test_dataset_lock_blocks_second_job(env, active_status):
    service = WorkflowPreflightService(
        env["allowlist"],
        WorkflowResourceCatalog(env["tmp_path"] / "res"),
        env["database"],
    )
    config = _preflight_config()
    assert service.validate_config(config)["valid"] is True

    job_id, _workspace = _job(env)
    env["database"].update_job_status(job_id, "running")
    if active_status != "running":
        env["database"].update_job_status(job_id, active_status)

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    assert any("locked" in error for error in excinfo.value.details["errors"])


def test_dataset_lock_releases_after_completion(env):
    service = WorkflowPreflightService(
        env["allowlist"],
        WorkflowResourceCatalog(env["tmp_path"] / "res"),
        env["database"],
    )
    job_id, _workspace = _job(env)
    _finish(env["database"], job_id)

    assert service.validate_config(_preflight_config())["valid"] is True
