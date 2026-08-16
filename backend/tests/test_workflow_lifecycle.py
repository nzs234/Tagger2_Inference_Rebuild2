"""Tests for job lifecycle: pause/resume, leases and interrupted-run repair."""

import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _setup(tmpdir, sample_count=2):
    from backend.tagger2.workflow.db import WorkflowDatabase
    from backend.tagger2.workflow.lifecycle import JobLifecycle

    database = WorkflowDatabase(Path(tmpdir) / "workflows.sqlite3")
    job_id, workspace = database.create_job(
        config_json={},
        config_hash="h",
        profile="e621",
        work_mode="full_copy",
        overwrite_mode="incremental",
        source_root_id="in",
        output_root_id="out",
        workspace_root=Path(tmpdir) / "jobs",
    )
    for index in range(sample_count):
        database.create_sample(job_id, index, f"{index}.png", "png")
    return database, job_id, workspace, JobLifecycle(database, job_id)


def test_schema_v2_adds_lease_columns():
    """Lease bookkeeping exists so an interrupted sample is detectable."""
    from backend.tagger2.workflow.db_schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 4

    with tempfile.TemporaryDirectory() as tmpdir:
        database, _job_id, _workspace, _lifecycle = _setup(tmpdir)
        with database.connection() as conn:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(workflow_samples)").fetchall()
            }
        assert {"lease_owner", "lease_expires_at", "attempt_count"} <= columns


def test_allowed_job_transitions_are_enforced():
    """A completed job is terminal; pause/resume round-trips."""
    from backend.tagger2.workflow.lifecycle import LifecycleError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, _workspace, lifecycle = _setup(tmpdir)

        # pending -> paused is not allowed; a job must start first.
        with pytest.raises(LifecycleError):
            lifecycle.pause()

        assert lifecycle.resume() == "running"
        assert lifecycle.pause() == "paused"
        assert lifecycle.resume() == "running"
        assert lifecycle.transition("completed") == "completed"

        # Terminal: nothing further is permitted.
        with pytest.raises(LifecycleError):
            lifecycle.resume()


def test_transition_to_same_state_is_a_noop():
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, _workspace, lifecycle = _setup(tmpdir)
        lifecycle.resume()
        assert lifecycle.resume() == "running"


def test_job_status_compare_and_set_rejects_stale_writer():
    """A stale pause/cancel request cannot overwrite a newer state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        database, job_id, _workspace, _lifecycle = _setup(tmpdir, sample_count=0)

        assert database.update_job_status(job_id, "running", expected_status="pending") is True
        assert database.update_job_status(job_id, "paused", expected_status="pending") is False
        current = database.get_job(job_id)
        assert current is not None
        assert current["status"] == "running"


def test_explicit_start_serializes_overlapping_dataset_scopes():
    """Only one queued job may own an overlapping source path at a time."""
    from backend.tagger2.workflow.db import WorkflowDatabase

    with tempfile.TemporaryDirectory() as tmpdir:
        database = WorkflowDatabase(Path(tmpdir) / "workflows.sqlite3")
        workspace_root = Path(tmpdir) / "jobs"
        common = {
            "schema_version": 2,
            "profile": "e621",
            "work_mode": "full_copy",
            "overwrite_mode": "incremental",
            "source_root": {"root_id": "input", "relative_path": "images"},
        }
        first, _ = database.create_job(
            config_json=common,
            config_hash="a",
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root_id="input",
            output_root_id="output",
            workspace_root=workspace_root,
        )
        second_config = dict(common)
        second_config["source_root"] = {"root_id": "input", "relative_path": "images/subset"}
        second, _ = database.create_job(
            config_json=second_config,
            config_hash="b",
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root_id="input",
            output_root_id="output",
            workspace_root=workspace_root,
        )

        assert database.start_job(first) is True
        assert database.start_job(second) is False
        first_row = database.get_job(first)
        second_row = database.get_job(second)
        assert first_row is not None and first_row["status"] == "queued"
        assert second_row is not None and second_row["status"] == "pending"

        # Once the owner reaches a terminal state, a pending sibling can start.
        assert database.update_job_status(first, "completed", expected_status="queued") is True
        assert database.start_job(second) is True


def test_lease_prevents_two_workers_claiming_one_sample():
    """A live lease blocks a second claim; the same owner may re-claim."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, _workspace, lifecycle = _setup(tmpdir)

        assert lifecycle.claim_sample(0, owner="worker-a") is True
        assert lifecycle.claim_sample(0, owner="worker-b") is False
        # The holder can refresh its own lease.
        assert lifecycle.claim_sample(0, owner="worker-a") is True


def test_batch_lease_claim_heartbeat_and_release_are_bounded():
    with tempfile.TemporaryDirectory() as tmpdir:
        database, job_id, _workspace, lifecycle = _setup(tmpdir, sample_count=3)

        claimed = lifecycle.claim_batch([0, 1, 2], owner="batch-worker")
        assert claimed == [0, 1, 2]
        assert lifecycle.heartbeat_samples(claimed, owner="batch-worker") == 3
        assert lifecycle.release_batch(
            {0: "completed", 1: "failed", 2: "skipped"},
            owner="batch-worker",
        ) == 3
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT sample_id, status, lease_owner, lease_expires_at"
                " FROM workflow_samples WHERE job_id = ? ORDER BY sample_id",
                (job_id,),
            ).fetchall()
        assert [(row["sample_id"], row["status"]) for row in rows] == [
            (0, "completed"),
            (1, "failed"),
            (2, "skipped"),
        ]
        assert all(row["lease_owner"] is None and row["lease_expires_at"] is None for row in rows)


def test_concurrent_batch_claims_survive_sqlite_lock_contention():
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, _workspace, lifecycle = _setup(tmpdir, sample_count=20)

        def claim(index: int):
            return lifecycle.claim_batch(range(20), owner=f"worker-{index}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(claim, range(8)))

        winners = [result for result in results if result]
        assert winners == [list(range(20))]


def test_expired_lease_is_reclaimable():
    """A lease that ran out can be taken over by another worker."""
    with tempfile.TemporaryDirectory() as tmpdir:
        database, job_id, _workspace, lifecycle = _setup(tmpdir)

        lifecycle.claim_sample(0, owner="worker-a", lease_seconds=300)
        stale = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        with database.connection() as conn:
            conn.execute(
                "UPDATE workflow_samples SET lease_expires_at = ?"
                " WHERE job_id = ? AND sample_id = 0",
                (stale, job_id),
            )

        assert lifecycle.expired_leases() == [0]
        assert lifecycle.claim_sample(0, owner="worker-b") is True


def test_processing_sample_without_lease_counts_as_expired():
    """A sample interrupted before its lease was recorded is not left stuck."""
    with tempfile.TemporaryDirectory() as tmpdir:
        database, job_id, _workspace, lifecycle = _setup(tmpdir)
        with database.connection() as conn:
            conn.execute(
                "UPDATE workflow_samples SET status = 'processing'"
                " WHERE job_id = ? AND sample_id = 1",
                (job_id,),
            )
        assert lifecycle.expired_leases() == [1]


def test_completed_sample_is_not_reclaimed():
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, _workspace, lifecycle = _setup(tmpdir)
        lifecycle.claim_sample(0, owner="worker-a")
        lifecycle.release_sample(0, status="completed")

        assert lifecycle.claim_sample(0, owner="worker-b") is False
        assert lifecycle.expired_leases() == []
        assert lifecycle.resumable_samples() == [1]


def test_repair_reclaims_then_parks_after_max_attempts():
    """Repeated failures park a sample instead of retrying forever."""
    from backend.tagger2.workflow.lifecycle import MAX_ATTEMPTS

    with tempfile.TemporaryDirectory() as tmpdir:
        database, job_id, workspace, lifecycle = _setup(tmpdir, sample_count=1)

        def expire():
            stale = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            with database.connection() as conn:
                conn.execute(
                    "UPDATE workflow_samples SET lease_expires_at = ?"
                    " WHERE job_id = ? AND sample_id = 0",
                    (stale, job_id),
                )

        for attempt in range(MAX_ATTEMPTS):
            assert lifecycle.claim_sample(0, owner=f"worker-{attempt}") is True
            expire()
            report = lifecycle.repair(workspace)
            if attempt < MAX_ATTEMPTS - 1:
                assert report.reclaimed_samples == 1
                assert report.parked_samples == 0
            else:
                assert report.parked_samples == 1
                assert report.reclaimed_samples == 0

        with database.connection() as conn:
            row = conn.execute(
                "SELECT status, error FROM workflow_samples WHERE job_id = ? AND sample_id = 0",
                (job_id,),
            ).fetchone()
        assert row["status"] == "failed"
        assert "parked after" in row["error"]


def test_repair_reports_journal_state():
    """The commit journal tells repair whether the last commit finished."""
    from backend.tagger2.workflow.commit import CommitJournal

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, workspace, lifecycle = _setup(tmpdir)

        assert lifecycle.repair(workspace).journal_state == "no_commit_attempted"

        journal = CommitJournal(workspace / "commit_journal.jsonl")
        journal.append({"event": "commit_started", "files": 2})
        journal.append({"event": "file_committed", "path": "a.json"})
        # Started, one file written, then the process died.
        interrupted = lifecycle.repair(workspace)
        assert interrupted.journal_state == "interrupted"
        assert interrupted.committed_files == 1

        journal.append({"event": "commit_completed", "committed": 2})
        assert lifecycle.repair(workspace).journal_state == "completed"


def test_repair_reports_skipped_commit():
    """A run blocked by issues is reported as skipped, not interrupted."""
    from backend.tagger2.workflow.commit import CommitJournal

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, workspace, lifecycle = _setup(tmpdir)
        CommitJournal(workspace / "commit_journal.jsonl").append(
            {"event": "commit_skipped", "blocking_issues": 3}
        )
        assert lifecycle.repair(workspace).journal_state == "skipped_due_to_issues"


def test_unknown_job_and_sample_are_errors():
    from backend.tagger2.workflow.lifecycle import JobLifecycle, LifecycleError

    with tempfile.TemporaryDirectory() as tmpdir:
        database, _job_id, _workspace, lifecycle = _setup(tmpdir)

        with pytest.raises(LifecycleError):
            JobLifecycle(database, "nope").resume()
        with pytest.raises(LifecycleError):
            lifecycle.claim_sample(999, owner="worker-a")
        with pytest.raises(LifecycleError):
            lifecycle.release_sample(0, status="teleported")


def test_operation_idempotency_is_scoped_to_job_and_operation_type():
    """A caller key may be reused by independent workflow operations."""
    from backend.tagger2.workflow.db import WorkflowDatabase

    with tempfile.TemporaryDirectory() as tmpdir:
        database = WorkflowDatabase(Path(tmpdir) / "workflows.sqlite3")
        jobs = []
        for index in range(2):
            job_id, _ = database.create_job(
                config_json={},
                config_hash=str(index),
                profile="e621",
                work_mode="full_copy",
                overwrite_mode="incremental",
                source_root_id=f"input-{index}",
                output_root_id=f"output-{index}",
                workspace_root=Path(tmpdir) / "jobs",
            )
            jobs.append(job_id)

        first = database.record_operation(
            jobs[0], "restore", idempotency_key="retry-1", payload={"step": 1}
        )
        replay = database.record_operation(
            jobs[0], "restore", idempotency_key="retry-1", payload={"step": 2}
        )
        other_job = database.record_operation(
            jobs[1], "restore", idempotency_key="retry-1"
        )
        other_type = database.record_operation(
            jobs[0], "discard", idempotency_key="retry-1"
        )

        assert replay == first
        assert len({first, other_job, other_type}) == 3
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT job_id, operation_type, idempotency_key, payload_json "
                "FROM workflow_operations ORDER BY created_at, operation_id"
            ).fetchall()
        assert len(rows) == 3
        assert {str(row[2]) for row in rows} == {"retry-1"}
        assert any(str(row[3]) == '{"step":2}' for row in rows)


def test_commit_journal_sequence_is_unique_under_concurrent_writers():
    """MAX(sequence)+1 allocation is serialized for one job."""
    from backend.tagger2.workflow.db import WorkflowDatabase

    with tempfile.TemporaryDirectory() as tmpdir:
        database = WorkflowDatabase(Path(tmpdir) / "workflows.sqlite3")
        job_id, _ = database.create_job(
            config_json={},
            config_hash="hash",
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root_id="input",
            output_root_id="output",
            workspace_root=Path(tmpdir) / "jobs",
        )

        def append(index: int) -> int:
            return database.record_commit_journal(
                job_id, "file_committed", payload={"index": index}
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            sequences = list(executor.map(append, range(32)))

        assert sorted(sequences) == list(range(32))
        with database.connection() as conn:
            rows = conn.execute(
                "SELECT sequence FROM workflow_commit_journals "
                "WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        assert [int(row[0]) for row in rows] == list(range(32))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
