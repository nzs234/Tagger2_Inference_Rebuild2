"""Job lifecycle: pause, resume, lease expiry and interrupted-run repair.

A workflow job is resumable because three things are durable: the immutable input
manifest, the per-sample status rows, and the commit journal. This module owns the
transitions between them and the repair of a run that stopped mid-flight.

Leases exist so an interrupted worker cannot leave a sample stuck in
``processing`` forever. An expired lease is reclaimable; a sample that keeps
failing is parked rather than retried without limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .commit import CommitJournal

# Terminal states cannot transition further; a paused job resumes to running.
ALLOWED_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"paused", "waiting_count_review", "waiting_token_review", "completed", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled", "failed"}),
    "waiting_count_review": frozenset({"running", "cancelled", "failed"}),
    "waiting_token_review": frozenset({"running", "cancelled", "failed"}),
    "completed": frozenset(),
    "failed": frozenset({"running"}),
    "cancelled": frozenset(),
}
TERMINAL_JOB_STATES = frozenset({"completed", "cancelled"})
DEFAULT_LEASE_SECONDS = 300
MAX_ATTEMPTS = 3


class LifecycleError(RuntimeError):
    """Raised when a lifecycle transition is not permitted."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
        self.database.update_job_status(self.job_id, target)
        return target

    def pause(self) -> str:
        return self.transition("paused")

    def resume(self) -> str:
        return self.transition("running")

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

        now = _now()
        expires = _iso(now + timedelta(seconds=lease_seconds))
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT status, lease_owner, lease_expires_at, attempt_count"
                " FROM workflow_samples WHERE job_id = ? AND sample_id = ?",
                (self.job_id, sample_id),
            ).fetchone()
            if row is None:
                raise LifecycleError(f"unknown sample: {sample_id}")
            if str(row["status"]) in {"completed", "skipped"}:
                return False

            existing_expiry = _parse(row["lease_expires_at"])
            if (
                str(row["status"]) == "processing"
                and existing_expiry is not None
                and existing_expiry > now
                and str(row["lease_owner"] or "") != owner
            ):
                return False

            conn.execute(
                "UPDATE workflow_samples"
                " SET status = 'processing', lease_owner = ?, lease_expires_at = ?,"
                "     attempt_count = attempt_count + 1, updated_at = ?"
                " WHERE job_id = ? AND sample_id = ?",
                (owner, expires, _iso(now), self.job_id, sample_id),
            )
        return True

    def release_sample(self, sample_id: int, *, status: str, error: str | None = None) -> None:
        """Finish a sample and drop its lease."""

        if status not in {"completed", "failed", "pending", "skipped"}:
            raise LifecycleError(f"unsupported sample status: {status!r}")
        with self.database.connection() as conn:
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
            for sample_id in self.expired_leases():
                row = conn.execute(
                    "SELECT attempt_count FROM workflow_samples"
                    " WHERE job_id = ? AND sample_id = ?",
                    (self.job_id, sample_id),
                ).fetchone()
                attempts = int(row["attempt_count"]) if row else 0
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

