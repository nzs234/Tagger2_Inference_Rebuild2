"""Opt-in 5k/100k control-plane acceptance for release hardware."""

import os
from pathlib import Path

import pytest

from tagger2.workflow.contracts import utc_now
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.lifecycle import JobLifecycle


@pytest.mark.skipif(
    os.environ.get("TAGGER2_RUN_WORKFLOW_STRESS") != "1",
    reason="set TAGGER2_RUN_WORKFLOW_STRESS=1 for scale acceptance",
)
@pytest.mark.parametrize("sample_count", [5_000, 100_000])
def test_batched_leases_complete_large_control_plane(
    tmp_path: Path,
    sample_count: int,
) -> None:
    database = WorkflowDatabase(tmp_path / f"workflow-{sample_count}.sqlite3")
    job_id, _ = database.create_job(
        config_json={},
        config_hash="scale",
        profile="e621",
        work_mode="full_copy",
        overwrite_mode="incremental",
        source_root_id="input",
        output_root_id="output",
        workspace_root=tmp_path / "jobs",
    )
    now = utc_now()
    with database.connection() as conn:
        conn.executemany(
            "INSERT INTO workflow_samples"
            " (job_id, sample_id, relative_image_path, image_format, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'png', 'pending', ?, ?)",
            (
                (job_id, sample_id, f"images/{sample_id}.png", now, now)
                for sample_id in range(sample_count)
            ),
        )

    lifecycle = JobLifecycle(database, job_id)
    for offset in range(0, sample_count, 500):
        sample_ids = list(range(offset, min(offset + 500, sample_count)))
        assert lifecycle.claim_batch(sample_ids, owner="scale-worker") == sample_ids
        assert lifecycle.heartbeat_samples(sample_ids, owner="scale-worker") == len(sample_ids)
        assert lifecycle.release_batch(
            {sample_id: "completed" for sample_id in sample_ids},
            owner="scale-worker",
        ) == len(sample_ids)

    with database.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, MIN(attempt_count) AS minimum,"
            " MAX(attempt_count) AS maximum FROM workflow_samples"
            " WHERE job_id = ? AND status = 'completed'",
            (job_id,),
        ).fetchone()
    assert int(row["total"]) == sample_count
    assert int(row["minimum"]) == 1
    assert int(row["maximum"]) == 1
