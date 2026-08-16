"""Job lifecycle: pause, resume, lease expiry and interrupted-run repair.

A workflow job is resumable because three things are durable: the immutable input
manifest, the per-sample status rows, and the commit journal. This module owns the
transitions between them and the repair of a run that stopped mid-flight.

Leases exist so an interrupted worker cannot leave a sample stuck in
``processing`` forever. An expired lease is reclaimable; a sample that keeps
failing is parked rather than retried without limit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .commit import CommitJournal

# Terminal states cannot transition further; a paused job resumes to running.
ALLOWED_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    # ``running`` is retained as a legal pending shortcut for old callers;
    # new API callers should use ``pending -> queued -> running`` via
    # ``WorkflowDatabase.start_job``.  Every transition is guarded by a
    # compare-and-set in :meth:`JobLifecycle.transition`.
    "pending": frozenset({"queued", "running", "cancelled", "failed"}),
    "queued": frozenset({"running", "pausing", "paused", "cancelling", "cancelled", "failed", "interrupted"}),
    "running": frozenset({
        "pausing", "paused", "waiting_count_review", "waiting_token_review",
        "committing", "completed", "failed", "cancelling", "cancelled", "interrupted",
    }),
    "pausing": frozenset({"paused", "cancelling", "cancelled", "failed", "interrupted"}),
    "paused": frozenset({"queued", "running", "cancelling", "cancelled", "failed", "interrupted"}),
    "waiting_count_review": frozenset({"queued", "running", "cancelling", "cancelled", "failed", "interrupted"}),
    "waiting_token_review": frozenset({"queued", "running", "cancelling", "cancelled", "failed", "interrupted"}),
    "committing": frozenset({"completed", "failed", "rollback_required", "cancelling", "cancelled", "interrupted"}),
    "restoring": frozenset({"completed", "failed", "rollback_required", "interrupted"}),
    "interrupted": frozenset({"queued", "running", "restoring", "failed", "cancelled", "rollback_required"}),
    "rollback_required": frozenset({"restoring", "failed", "cancelled"}),
    "cancelling": frozenset({"cancelled", "failed", "interrupted"}),
    # Failed work can be explicitly recovered and re-queued after an operator
    # has inspected the durable issue/lease state.
    "failed": frozenset({"queued", "running", "restoring"}),
    # Restore is an auditable operation and therefore may move a terminal job
    # through ``restoring`` before returning to completed.
    "completed": frozenset({"restoring"}),
    "cancelled": frozenset({"restoring"}),
}
TERMINAL_JOB_STATES = frozenset({"completed", "cancelled"})
DEFAULT_LEASE_SECONDS = 300
MAX_ATTEMPTS = 3


class LifecycleError(RuntimeError):
    """Raised when a lifecycle transition is not permitted."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RepairReport:
    """Outcome of repairing an interrupted job."""

    reclaimed_samples: int
    parked_samples: int
    committed_files: int
    journal_state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "reclaimed_samples": self.reclaimed_samples,
            "parked_samples": self.parked_samples,
            "committed_files": self.committed_files,
            "journal_state": self.journal_state,
        }


class JobLifecycle:
    """Pause, resume and repair operations for one workflow job."""

    def __init__(self, database: Any, job_id: str):
        self.database = database
        self.job_id = job_id

    def _job(self) -> dict[str, Any]:
        job = self.database.get_job(self.job_id)
        if job is None:
            raise LifecycleError(f"unknown job: {self.job_id}")
        return job

    def transition(self, target: str) -> str:
        """Move the job to ``target``, refusing a transition that is not allowed."""

        job = self._job()
        current = str(job["status"])
        allowed = ALLOWED_JOB_TRANSITIONS.get(current, frozenset())
        if target == current:
            return current
        if target not in allowed:
            raise LifecycleError(
                f"cannot move job from {current!r} to {target!r};"
                f" allowed: {sorted(allowed) or 'none'}"
            )
        updated = self.database.update_job_status(
            self.job_id,
            target,
            expected_status=current,
        )
        if not updated:
            # Another worker won the race after our read.  Do not silently
            # report success: callers must reload state and decide whether to
            # retry the operation.
            raise LifecycleError(
                f"concurrent lifecycle update lost for job {self.job_id!r}"
            )
        return target

    def start(self) -> str:
        """Queue a pending job at the explicit start boundary.

        Dataset lock acquisition belongs to ``WorkflowDatabase.start_job``;
        this method is useful for workers/tests that already hold that lock or
        for legacy in-process callers that do not have a queue service.
        """

        return self.transition("queued")

    def pause(self) -> str:
        # Keep the historical synchronous API while allowing the state machine
        # to expose ``pausing`` to a queue worker when it is used explicitly.
        return self.transition("paused")

    def request_pause(self) -> str:
        """Request a pause at the next batch boundary."""

        return self.transition("pausing")

    def resume(self) -> str:
        return self.transition("running")

    def cancel(self) -> str:
        """Mark a job cancelled, preserving the legacy immediate semantics."""

        return self.transition("cancelled")

    def request_cancel(self) -> str:
        """Request cancellation at the next batch boundary."""

        return self.transition("cancelling")

    def recover(self) -> str:
        """Re-queue an interrupted/failed job after durable-state repair."""

        return self.transition("queued")

    def claim_sample(
        self,
        sample_id: int,
        *,
        owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Claim one pending sample, or reclaim one whose lease expired.

        Returns False when another live lease holds the sample, so two workers
        cannot process it at once.
        """

        return sample_id in self.claim_batch(
            [sample_id],
            owner=owner,
            lease_seconds=lease_seconds,
        )

    def claim_batch(
        self,
        sample_ids: Sequence[int],
        *,
        owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> list[int]:
        """Claim up to 500 samples atomically and return the acquired ids."""

        normalized = list(dict.fromkeys(int(sample_id) for sample_id in sample_ids))
        if len(normalized) > 500:
            raise LifecycleError("sample lease batches cannot exceed 500 items")
        if not owner:
            raise LifecycleError("sample lease owner must be non-empty")
        now = _now()
        expires = _iso(now + timedelta(seconds=lease_seconds))
        acquired: list[int] = []
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sample_id in normalized:
                row = conn.execute(
                    "SELECT status, lease_owner, lease_expires_at, attempt_count"
                    " FROM workflow_samples WHERE job_id = ? AND sample_id = ?",
                    (self.job_id, sample_id),
                ).fetchone()
                if row is None:
                    raise LifecycleError(f"unknown sample: {sample_id}")
                status = str(row["status"])
                existing_owner = str(row["lease_owner"] or "")
                if status in {"completed", "skipped"}:
                    continue

                existing_expiry = _parse(row["lease_expires_at"])
                if (
                    status == "processing"
                    and existing_expiry is not None
                    and existing_expiry > now
                    and existing_owner != owner
                ):
                    continue

                if status == "processing" and existing_owner == owner:
                    conn.execute(
                        "UPDATE workflow_samples SET lease_expires_at = ?, updated_at = ?"
                        " WHERE job_id = ? AND sample_id = ?",
                        (expires, _iso(now), self.job_id, sample_id),
                    )
                else:
                    conn.execute(
                        "UPDATE workflow_samples"
                        " SET status = 'processing', lease_owner = ?, lease_expires_at = ?,"
                        "     attempt_count = attempt_count + 1, updated_at = ?"
                        " WHERE job_id = ? AND sample_id = ?",
                        (owner, expires, _iso(now), self.job_id, sample_id),
                    )
                acquired.append(sample_id)
        return acquired

    def heartbeat_samples(
        self,
        sample_ids: Sequence[int],
        *,
        owner: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> int:
        """Extend live leases owned by this worker without adding attempts."""

        normalized = list(dict.fromkeys(int(sample_id) for sample_id in sample_ids))
        if len(normalized) > 500:
            raise LifecycleError("sample lease batches cannot exceed 500 items")
        now = _now()
        expires = _iso(now + timedelta(seconds=lease_seconds))
        refreshed = 0
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sample_id in normalized:
                cursor = conn.execute(
                    "UPDATE workflow_samples SET lease_expires_at = ?, updated_at = ?"
                    " WHERE job_id = ? AND sample_id = ?"
                    " AND status = 'processing' AND lease_owner = ?",
                    (expires, _iso(now), self.job_id, sample_id, owner),
                )
                refreshed += int(cursor.rowcount or 0)
        return refreshed

    def release_batch(
        self,
        outcomes: Mapping[int, str],
        *,
        owner: str,
    ) -> int:
        """Release a claimed batch while refusing to overwrite another lease."""

        if len(outcomes) > 500:
            raise LifecycleError("sample lease batches cannot exceed 500 items")
        if any(status not in {"completed", "failed", "pending", "skipped"} for status in outcomes.values()):
            raise LifecycleError("unsupported sample status in release batch")
        released = 0
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sample_id, status in outcomes.items():
                cursor = conn.execute(
                    "UPDATE workflow_samples"
                    " SET status = ?, error = NULL, lease_owner = NULL,"
                    " lease_expires_at = NULL, updated_at = ?"
                    " WHERE job_id = ? AND sample_id = ? AND lease_owner = ?",
                    (status, _iso(_now()), self.job_id, int(sample_id), owner),
                )
                released += int(cursor.rowcount or 0)
        return released

    def release_sample(self, sample_id: int, *, status: str, error: str | None = None) -> None:
        """Finish a sample and drop its lease."""

        if status not in {"completed", "failed", "pending", "skipped"}:
            raise LifecycleError(f"unsupported sample status: {status!r}")
        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE workflow_samples"
                " SET status = ?, error = ?, lease_owner = NULL, lease_expires_at = NULL,"
                "     updated_at = ?"
                " WHERE job_id = ? AND sample_id = ?",
                (status, error, _iso(_now()), self.job_id, sample_id),
            )

    def expired_leases(self) -> list[int]:
        """Samples still marked processing whose lease has run out."""

        now = _now()
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT sample_id, lease_expires_at FROM workflow_samples"
                " WHERE job_id = ? AND status = 'processing'",
                (self.job_id,),
            ).fetchall()
        expired: list[int] = []
        for row in rows:
            expiry = _parse(row["lease_expires_at"])
            # A processing sample with no expiry was interrupted before its lease
            # was recorded, so treat it as expired rather than leaving it stuck.
            if expiry is None or expiry <= now:
                expired.append(int(row["sample_id"]))
        return expired

    def repair(self, workspace: Path) -> RepairReport:
        """Repair an interrupted run so it can be resumed safely.

        Samples with an expired lease go back to pending, unless they have already
        used their attempts, in which case they are parked as failed for a human
        to look at. The commit journal is read to report whether the last commit
        completed; a partially committed run is reported, never re-driven blindly.
        """

        reclaimed = 0
        parked = 0
        now = _iso(_now())

        with self.database.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Query on the same write transaction so a second repair worker
            # cannot observe and reclaim the same lease concurrently.
            now_dt = _now()
            expired_rows = conn.execute(
                "SELECT sample_id, lease_expires_at, attempt_count"
                " FROM workflow_samples WHERE job_id = ? AND status = 'processing'",
                (self.job_id,),
            ).fetchall()
            expired = []
            for row in expired_rows:
                expiry = _parse(row["lease_expires_at"])
                if expiry is None or expiry <= now_dt:
                    expired.append(row)
            for expired_row in expired:
                sample_id = int(expired_row["sample_id"])
                attempts = int(expired_row["attempt_count"])
                if attempts >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE workflow_samples"
                        " SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL,"
                        "     error = ?, updated_at = ?"
                        " WHERE job_id = ? AND sample_id = ?",
                        (
                            f"parked after {attempts} attempts without completing",
                            now,
                            self.job_id,
                            sample_id,
                        ),
                    )
                    parked += 1
                else:
                    conn.execute(
                        "UPDATE workflow_samples"
                        " SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL,"
                        "     updated_at = ?"
                        " WHERE job_id = ? AND sample_id = ?",
                        (now, self.job_id, sample_id),
                    )
                    reclaimed += 1

        journal_state, committed = _journal_state(workspace)
        return RepairReport(
            reclaimed_samples=reclaimed,
            parked_samples=parked,
            committed_files=committed,
            journal_state=journal_state,
        )

    def resumable_samples(self) -> list[int]:
        """Samples that still need work, in manifest order."""

        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT sample_id FROM workflow_samples"
                " WHERE job_id = ? AND status IN ('pending', 'processing')"
                " ORDER BY sample_id",
                (self.job_id,),
            ).fetchall()
        return [int(row["sample_id"]) for row in rows]


def _journal_state(workspace: Path) -> tuple[str, int]:
    """Summarize the commit journal for one job workspace."""

    journal = CommitJournal(Path(workspace) / "commit_journal.jsonl")
    entries = journal.entries()
    if not entries:
        return "no_commit_attempted", 0

    committed = sum(1 for entry in entries if entry.get("event") == "file_committed")
    events = {str(entry.get("event")) for entry in entries}
    if "commit_completed" in events:
        return "completed", committed
    if "commit_failed" in events:
        return "failed_partway", committed
    if "commit_skipped" in events:
        return "skipped_due_to_issues", committed
    if "commit_started" in events:
        # Started but no terminal event: the process died during the commit.
        return "interrupted", committed
    return "unknown", committed


__all__ = [
    "ALLOWED_JOB_TRANSITIONS",
    "DEFAULT_LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "TERMINAL_JOB_STATES",
    "JobLifecycle",
    "LifecycleError",
    "RepairReport",
]

