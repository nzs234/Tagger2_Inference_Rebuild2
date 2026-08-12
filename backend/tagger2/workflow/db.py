"""Workflow database connection and operations."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .contracts import canonical_json, utc_now
from .db_schema import MIGRATION_CHECKSUMS, SCHEMA_SQL, SCHEMA_VERSION, apply_migrations


class DatabaseConcurrencyError(RuntimeError):
    """Raised when a compare-and-set update loses a concurrent race."""


def default_workflow_database_path() -> Path:
    """Return default workflow database path."""
    from ..config import get_settings
    settings = get_settings()
    data_dir = settings.data_dir
    if data_dir is None:
        raise RuntimeError("application data_dir is not configured")
    return data_dir / "workflows" / "workflows.sqlite3"


class WorkflowDatabase:
    """Workflow database connection manager."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._memory_conn: sqlite3.Connection | None = None
        # A memory database is backed by one shared connection.  SQLite does
        # not permit two ``BEGIN IMMEDIATE`` statements on that connection at
        # once, so serialize the small write transactions that implement the
        # durable control-plane records.  File-backed databases still rely on
        # SQLite's database lock, while this lock also avoids needless retries
        # for callers sharing one ``WorkflowDatabase`` instance.
        self._write_lock = threading.RLock()
        
        # For :memory: databases, create and hold a persistent connection
        if str(db_path) == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", timeout=30.0, check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.execute("PRAGMA foreign_keys=ON")
            self._memory_conn.execute("PRAGMA busy_timeout=30000")
            # Apply migrations directly to this connection.  Keep the same
            # immutable migration marker as file-backed databases; deriving a
            # checksum from the current schema made in-memory tests disagree
            # with the persisted database format.
            self._memory_conn.executescript(SCHEMA_SQL)
            self._memory_conn.execute(
                "INSERT INTO schema_migrations (version, checksum, applied_at)"
                " VALUES (?, ?, datetime('now'))",
                (SCHEMA_VERSION, MIGRATION_CHECKSUMS[SCHEMA_VERSION]),
            )
            self._memory_conn.commit()
        else:
            apply_migrations(self.db_path)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connection."""
        if self._memory_conn is not None:
            # For :memory:, yield the persistent connection without closing
            try:
                yield self._memory_conn
                self._memory_conn.commit()
            except Exception:
                self._memory_conn.rollback()
                raise
        else:
            # For file-based databases, create a new connection each time
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def create_job(
        self,
        config_json: dict[str, Any],
        config_hash: str,
        profile: str,
        work_mode: str,
        overwrite_mode: str,
        source_root_id: str,
        output_root_id: str | None,
        workspace_root: Path,
    ) -> tuple[str, Path]:
        """Create a new workflow job and reserve its workspace directory.

        The identifier is allocated first so the workspace path can be derived
        from it, and the directory is created before the row is inserted so a
        visible job always has a workspace on disk.
        """
        job_id = uuid.uuid4().hex
        workspace_path = Path(workspace_root) / job_id
        workspace_path.mkdir(parents=True, exist_ok=False)
        now = utc_now()
        
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_jobs (
                    job_id, config_version, config_json, config_hash,
                    profile, work_mode, overwrite_mode,
                    source_root_id, output_root_id, workspace_path,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, int(config_json.get("schema_version", 1)), canonical_json(config_json), config_hash,
                    profile, work_mode, overwrite_mode,
                    source_root_id, output_root_id, str(workspace_path),
                    "pending", now
                )
            )
            self._insert_event(
                conn,
                job_id,
                "job_created",
                to_status="pending",
            )

        return job_id, workspace_path

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get job by ID."""
        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM workflow_jobs WHERE job_id = ?",
                (job_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_jobs(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List all jobs."""
        with self.connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM workflow_jobs
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset)
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_job_status(
        self,
        job_id: str,
        status: str,
        current_module_id: str | None = None,
        error: str | None = None,
        *,
        expected_status: str | None = None,
    ) -> bool:
        """Update a job status, optionally using a compare-and-set guard.

        ``expected_status`` is intentionally checked in the SQL ``WHERE``
        clause rather than after a preceding read.  That makes lifecycle
        controls safe when two workers (or a worker and an HTTP request) race
        to pause, resume, cancel, or recover the same job.  The old call shape
        remains valid and returns ``True`` when a row was updated.
        """
        now = utc_now()
        
        with self.connection() as conn:
            current_row = conn.execute(
                "SELECT status FROM workflow_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            current_status = str(current_row["status"]) if current_row else None
            fields = ["status = ?"]
            values: list[Any] = [status]
            
            if current_module_id is not None:
                fields.append("current_module_id = ?")
                values.append(current_module_id)
            
            if error is not None:
                fields.append("error = ?")
                values.append(error)
            
            if status == "running":
                fields.append("started_at = ?")
                values.append(now)
            elif status in ("completed", "failed", "cancelled"):
                fields.append("finished_at = ?")
                values.append(now)
            
            where = "job_id = ?"
            values.append(job_id)
            if expected_status is not None:
                where += " AND status = ?"
                values.append(expected_status)
            
            cursor = conn.execute(
                f"UPDATE workflow_jobs SET {', '.join(fields)} WHERE {where}",
                tuple(values)
            )
            if cursor.rowcount == 1 and current_status is not None and current_status != status:
                self._insert_event(
                    conn,
                    job_id,
                    "status_changed",
                    from_status=current_status,
                    to_status=status,
                )
                if status in {"completed", "failed", "cancelled", "interrupted"}:
                    conn.execute(
                        "UPDATE workflow_dataset_locks SET released_at = ? "
                        "WHERE job_id = ? AND released_at IS NULL",
                        (now, job_id),
                    )
            return cursor.rowcount == 1

    def start_job(self, job_id: str, *, expected_status: str = "pending") -> bool:
        """Atomically queue a job after acquiring its dataset lock.

        Preflight checks are advisory.  This method is the authoritative start
        boundary: ``BEGIN IMMEDIATE`` serialises competing starts and the
        status update is guarded by ``expected_status``.  A pending job does
        not hold a lock, so multiple drafts may be created and exactly one can
        win when they are started.
        """

        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM workflow_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) != expected_status:
                return False

            requested_scopes = _job_lock_scopes(row)
            candidates = conn.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE status IN (
                    'queued', 'running', 'pausing', 'paused',
                    'waiting_count_review', 'waiting_token_review',
                    'committing', 'restoring', 'interrupted',
                    'rollback_required', 'cancelling'
                  )
                  AND job_id <> ?
                """,
                (job_id,),
            ).fetchall()
            for candidate in candidates:
                for requested_root, requested_relative in requested_scopes:
                    for candidate_root, candidate_relative in _job_lock_scopes(candidate):
                        if requested_root == candidate_root and _relative_paths_overlap(
                            requested_relative, candidate_relative
                        ):
                            return False

            cursor = conn.execute(
                "UPDATE workflow_jobs SET status = 'queued'"
                " WHERE job_id = ? AND status = ?",
                (job_id, expected_status),
            )
            if cursor.rowcount == 1:
                for root_id, relative_path in requested_scopes:
                    self._insert_dataset_lock(
                        conn, job_id, root_id, relative_path
                    )
                self._insert_event(
                    conn,
                    job_id,
                    "status_changed",
                    from_status=expected_status,
                    to_status="queued",
                )
            return cursor.rowcount == 1

    @staticmethod
    def _insert_dataset_lock(
        conn: sqlite3.Connection, job_id: str, root_id: str, relative_path: str
    ) -> None:
        conn.execute(
            "INSERT INTO workflow_dataset_locks "
            "(lock_id, job_id, root_id, relative_path, acquired_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, job_id, root_id, relative_path, utc_now()),
        )

    def release_dataset_locks(self, job_id: str) -> None:
        """Release durable lock records after a terminal operation."""

        with self.connection() as conn:
            conn.execute(
                "UPDATE workflow_dataset_locks SET released_at = ? "
                "WHERE job_id = ? AND released_at IS NULL",
                (utc_now(), job_id),
            )

    def record_operation(
        self,
        job_id: str,
        operation_type: str,
        *,
        idempotency_key: str,
        status: str = "completed",
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Persist an idempotent control-plane operation.

        ``idempotency_key`` is the caller-owned operation identity.  Replaying
        a request updates the existing row instead of creating a second audit
        record, which makes Restore/Discard safe across HTTP retries.
        """

        if not operation_type or any(char.isspace() for char in operation_type):
            raise ValueError("operation_type must be a non-empty token")
        if not idempotency_key:
            raise ValueError("idempotency_key must be non-empty")
        operation_id = uuid.uuid4().hex
        now = utc_now()
        finished_at = now if status in {"completed", "failed", "cancelled"} else None
        with self._write_lock:
            with self.connection() as conn:
                # Take the writer lock before checking for an existing row.
                # Without this, two processes can both observe no row and
                # race to insert the same idempotency key.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    "SELECT operation_id, job_id, operation_type FROM workflow_operations "
                    "WHERE job_id = ? AND operation_type = ? AND idempotency_key = ?",
                    (job_id, operation_type, idempotency_key),
                ).fetchone()
                if existing is not None:
                    operation_id = str(existing["operation_id"])
                    conn.execute(
                        "UPDATE workflow_operations SET status = ?, payload_json = ?, "
                        "finished_at = COALESCE(?, finished_at) WHERE operation_id = ?",
                        (
                            status,
                            canonical_json(payload or {}),
                            finished_at,
                            operation_id,
                        ),
                    )
                else:
                    conn.execute(
                        "INSERT INTO workflow_operations "
                        "(operation_id, job_id, operation_type, status, idempotency_key, "
                        "payload_json, created_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            operation_id,
                            job_id,
                            operation_type,
                            status,
                            idempotency_key,
                            canonical_json(payload or {}),
                            now,
                            finished_at,
                        ),
                    )
        return operation_id

    def record_stage_run(
        self,
        job_id: str,
        stage_id: str,
        *,
        status: str,
        run_id: str | None = None,
        batch_size: int = 0,
        total: int = 0,
        processed: int = 0,
        issue_count: int = 0,
        checkpoint: dict[str, Any] | None = None,
    ) -> str:
        """Create or update one durable stage-run projection."""

        if not stage_id or any(char.isspace() for char in stage_id):
            raise ValueError("stage_id must be a non-empty token")
        if status not in {"pending", "running", "completed", "failed", "skipped"}:
            raise ValueError(f"unsupported stage status: {status!r}")
        stage_run_id = run_id or uuid.uuid4().hex
        now = utc_now()
        started_at = now if status == "running" else None
        finished_at = now if status in {"completed", "failed", "skipped"} else None
        with self.connection() as conn:
            if run_id is None:
                conn.execute(
                    "INSERT INTO workflow_stage_runs "
                    "(run_id, job_id, stage_id, status, batch_size, total, processed, "
                    "issue_count, checkpoint_json, started_at, finished_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stage_run_id,
                        job_id,
                        stage_id,
                        status,
                        int(batch_size),
                        int(total),
                        int(processed),
                        int(issue_count),
                        canonical_json(checkpoint or {}),
                        started_at,
                        finished_at,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE workflow_stage_runs SET status = ?, batch_size = ?, total = ?, "
                    "processed = ?, issue_count = ?, checkpoint_json = ?, "
                    "started_at = COALESCE(started_at, ?), "
                    "finished_at = COALESCE(?, finished_at) WHERE run_id = ?",
                    (
                        status,
                        int(batch_size),
                        int(total),
                        int(processed),
                        int(issue_count),
                        canonical_json(checkpoint or {}),
                        started_at,
                        finished_at,
                        stage_run_id,
                    ),
                )
        return stage_run_id

    def record_commit_journal(
        self,
        job_id: str,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Append one durable commit-journal event with a per-job sequence."""

        if not event_type or any(char.isspace() for char in event_type):
            raise ValueError("event_type must be a non-empty token")
        with self._write_lock:
            with self.connection() as conn:
                # The immediate transaction makes MAX(sequence)+1 an atomic
                # per-job allocation for separate file-backed connections;
                # ``_write_lock`` covers the shared :memory: connection case.
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_sequence "
                    "FROM workflow_commit_journals WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                sequence = int(row["next_sequence"] if row is not None else 0)
                conn.execute(
                    "INSERT INTO workflow_commit_journals "
                    "(job_id, sequence, event_type, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job_id, sequence, event_type, canonical_json(payload or {}), utc_now()),
                )
        return sequence

    def record_artifact(
        self,
        job_id: str,
        *,
        kind: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
    ) -> str:
        """Register one validated workspace artifact by relative path."""

        if not kind or any(char.isspace() for char in kind):
            raise ValueError("artifact kind must be a non-empty token")
        if not relative_path or relative_path.startswith(("/", "\\")):
            raise ValueError("artifact path must be relative")
        artifact_id = uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM workflow_artifacts WHERE job_id = ? AND kind = ? "
                "AND relative_path = ?",
                (job_id, kind, relative_path),
            )
            conn.execute(
                "INSERT INTO workflow_artifacts "
                "(artifact_id, job_id, kind, relative_path, sha256, size_bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, job_id, kind, relative_path, sha256, int(size_bytes), utc_now()),
            )
        return artifact_id

    def create_sample(
        self,
        job_id: str,
        sample_id: int,
        relative_image_path: str,
        image_format: str,
    ) -> None:
        """Create a workflow sample."""
        now = utc_now()
        
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_samples (
                    job_id, sample_id, relative_image_path, image_format,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, sample_id, relative_image_path, image_format, "pending", now, now)
            )
            self._insert_event(
                conn,
                job_id,
                "sample_created",
                payload={"sample_id": sample_id, "relative_image_path": relative_image_path},
            )

    def update_sample_status(
        self,
        job_id: str,
        sample_id: int,
        status: str,
        current_module_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update sample status."""
        now = utc_now()
        
        with self.connection() as conn:
            fields = ["status = ?", "updated_at = ?"]
            values: list[Any] = [status, now]
            
            if current_module_id is not None:
                fields.append("current_module_id = ?")
                values.append(current_module_id)
            
            if error is not None:
                fields.append("error = ?")
                values.append(error)
            
            values.extend([job_id, sample_id])
            
            conn.execute(
                f"UPDATE workflow_samples SET {', '.join(fields)} WHERE job_id = ? AND sample_id = ?",
                tuple(values)
            )
            self._insert_event(
                conn,
                job_id,
                "sample_status_changed",
                payload={"sample_id": sample_id, "status": status},
            )

    def create_issue(
        self,
        job_id: str,
        module_id: str,
        code: str,
        severity: str,
        blocking: bool,
        message: str,
        sample_id: int | None = None,
    ) -> str:
        """Create a workflow issue."""
        issue_id = uuid.uuid4().hex
        now = utc_now()
        
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_issues (
                    issue_id, job_id, sample_id, module_id, code,
                    severity, blocking, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_id, job_id, sample_id, module_id, code, severity, int(blocking), message, now)
            )
            self._insert_event(
                conn,
                job_id,
                "issue_created",
                payload={
                    "issue_id": issue_id,
                    "sample_id": sample_id,
                    "module_id": module_id,
                    "code": code,
                    "severity": severity,
                    "blocking": bool(blocking),
                },
            )
        
        return issue_id

    def list_issues(self, job_id: str, blocking_only: bool = False) -> list[dict[str, Any]]:
        """List issues for a job."""
        with self.connection() as conn:
            if blocking_only:
                cursor = conn.execute(
                    """
                    SELECT * FROM workflow_issues
                    WHERE job_id = ? AND blocking = 1 AND resolved_at IS NULL
                    ORDER BY created_at DESC
                    """,
                    (job_id,)
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM workflow_issues
                    WHERE job_id = ? AND resolved_at IS NULL
                    ORDER BY severity DESC, created_at DESC
                    """,
                    (job_id,)
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_jobs_for_path(self, source_root_id: str) -> list[dict[str, Any]]:
        """Get active jobs using the given source root.
        
        Active jobs are those in running, pending, or paused states.
        Used for dataset lock checking during preflight.
        """
        with self.connection() as conn:
            cursor = conn.execute(
                """
                SELECT * FROM workflow_jobs
                WHERE source_root_id = ? 
                  AND status IN (
                    'queued', 'running', 'pausing', 'paused',
                    'waiting_count_review', 'waiting_token_review',
                    'committing', 'restoring', 'interrupted',
                    'rollback_required', 'cancelling'
                  )
                ORDER BY created_at DESC
                """,
                (source_root_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_active_jobs_for_scopes(
        self,
        scopes: list[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """Return active jobs whose normalized source/output scopes overlap."""

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_jobs WHERE status IN ("
                "'queued','running','pausing','paused','waiting_count_review',"
                "'waiting_token_review','committing','restoring','interrupted',"
                "'rollback_required','cancelling')"
            ).fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            candidate = _job_lock_scopes(row)
            if any(
                root_a == root_b and _relative_paths_overlap(path_a, path_b)
                for root_a, path_a in scopes
                for root_b, path_b in candidate
            ):
                matches.append(dict(row))
        return matches

    def record_event(
        self,
        job_id: str,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Append one durable control-plane event and return its cursor."""

        with self.connection() as conn:
            return self._insert_event(
                conn,
                job_id,
                event_type,
                from_status=from_status,
                to_status=to_status,
                payload=payload,
            )

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        job_id: str,
        event_type: str,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Insert an event using an existing transaction.

        This helper intentionally accepts only JSON-like dictionaries from
        callers.  Events are a public replay cursor, so arbitrary exception
        text or filesystem paths must not be written by the control plane.
        """

        if not event_type or any(char.isspace() for char in event_type):
            raise ValueError("event_type must be a non-empty token")
        payload_json = canonical_json(payload or {})
        cursor = conn.execute(
            """
            INSERT INTO workflow_events (
                job_id, event_type, from_status, to_status, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, event_type, from_status, to_status, payload_json, utc_now()),
        )
        return int(cursor.lastrowid or 0)

    def list_events(
        self,
        job_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return a bounded, replayable event page for a workflow job."""

        if after_event_id < 0:
            raise ValueError("after_event_id must be non-negative")
        bounded_limit = max(1, min(int(limit), 500))
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT event_id, job_id, event_type, from_status, to_status,
                       payload_json, created_at
                FROM workflow_events
                WHERE job_id = ? AND event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (job_id, int(after_event_id), bounded_limit),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            events.append(
                {
                    "event_id": int(row["event_id"]),
                    "job_id": str(row["job_id"]),
                    "event_type": str(row["event_type"]),
                    "from_status": row["from_status"],
                    "to_status": row["to_status"],
                    "payload": payload,
                    "created_at": str(row["created_at"]),
                }
            )
        return events

    def mark_interrupted_jobs(self) -> list[str]:
        """Mark in-flight jobs interrupted after a process restart.

        The workflow worker is intentionally in-process today.  A queued or
        running row therefore has no durable worker after a restart and must
        not remain permanently active.  Waiting-for-review and paused rows are
        operator state and are left untouched.
        """

        active_states = (
            "queued", "running", "pausing", "committing", "restoring", "cancelling"
        )
        interrupted: list[str] = []
        with self.connection() as conn:
            placeholders = ", ".join("?" for _ in active_states)
            rows = conn.execute(
                f"SELECT job_id, status FROM workflow_jobs WHERE status IN ({placeholders})",
                active_states,
            ).fetchall()
            for row in rows:
                job_id = str(row["job_id"])
                old_status = str(row["status"])
                conn.execute(
                    "UPDATE workflow_jobs SET status = 'interrupted' "
                    "WHERE job_id = ? AND status = ?",
                    (job_id, old_status),
                )
                self._insert_event(
                    conn,
                    job_id,
                    "status_changed",
                    from_status=old_status,
                    to_status="interrupted",
                    payload={"reason": "process_restart"},
                )
                interrupted.append(job_id)
        return interrupted

    def set_job_pinned(self, job_id: str, pinned: bool) -> bool:
        """Pin/unpin a job for retention and return whether it exists."""

        with self.connection() as conn:
            if conn.execute(
                "SELECT 1 FROM workflow_jobs WHERE job_id = ?", (job_id,)
            ).fetchone() is None:
                return False
            if pinned:
                conn.execute(
                    "INSERT INTO workflow_job_pins(job_id, pinned_at) VALUES (?, ?) "
                    "ON CONFLICT(job_id) DO UPDATE SET pinned_at = excluded.pinned_at",
                    (job_id, utc_now()),
                )
                event_type = "job_pinned"
            else:
                conn.execute("DELETE FROM workflow_job_pins WHERE job_id = ?", (job_id,))
                event_type = "job_unpinned"
            self._insert_event(conn, job_id, event_type)
            return True

    def is_job_pinned(self, job_id: str) -> bool:
        with self.connection() as conn:
            return conn.execute(
                "SELECT 1 FROM workflow_job_pins WHERE job_id = ?", (job_id,)
            ).fetchone() is not None


def _job_source_relative_path(config_json: object) -> str:
    """Extract and normalise a job's source relative path for lock checks."""

    try:
        payload = json.loads(str(config_json)) if isinstance(config_json, str) else config_json
        if isinstance(payload, dict):
            source = payload.get("source_root")
            if isinstance(source, dict):
                value = source.get("relative_path", ".")
            else:
                value = payload.get("source_relative_path", ".")
            if isinstance(value, str):
                return _normalise_relative_path(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return "."


def _job_lock_scopes(row: sqlite3.Row | dict[str, Any]) -> list[tuple[str, str]]:
    """Return normalized source/output path scopes owned by an active job."""

    try:
        payload = json.loads(str(row["config_json"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    scopes: list[tuple[str, str]] = []
    source_root = str(row["source_root_id"])
    source_relative = _job_source_relative_path(row["config_json"])
    scopes.append((source_root, source_relative))
    output_root = row["output_root_id"]
    if output_root:
        output_relative = "."
        if isinstance(payload, dict):
            output = payload.get("output_root")
            if isinstance(output, dict) and isinstance(output.get("relative_path"), str):
                output_relative = _normalise_relative_path(str(output["relative_path"]))
        scopes.append((str(output_root), output_relative))
    return scopes


def _normalise_relative_path(value: str) -> str:
    """Return a stable, separator-independent relative path key."""

    text = value.replace("\\", "/").strip()
    if not text or text == ".":
        return "."
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        # Invalid paths are deliberately treated as the root scope.  Path
        # allowlist validation reports the real error; lock checking must not
        # accidentally allow an escape to bypass a lock.
        return "."
    return "/".join(parts).casefold() or "."


def _relative_paths_overlap(first: str, second: str) -> bool:
    """Whether two normalised relative paths overlap as directory scopes."""

    left = _normalise_relative_path(first)
    right = _normalise_relative_path(second)
    if left == "." or right == ".":
        return True
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


__all__ = [
    "DatabaseConcurrencyError",
    "WorkflowDatabase",
    "default_workflow_database_path",
]
