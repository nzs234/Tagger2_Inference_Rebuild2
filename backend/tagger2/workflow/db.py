"""Workflow database connection and operations."""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import utc_now, canonical_json
from .db_schema import apply_migrations


def default_workflow_database_path() -> Path:
    """Return default workflow database path."""
    from ..config import get_settings
    settings = get_settings()
    return settings.data_dir / "workflows" / "workflows.sqlite3"


class WorkflowDatabase:
    """Workflow database connection manager."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        apply_migrations(self.db_path)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager for database connection."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
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
                    job_id, 1, canonical_json(config_json), config_hash,
                    profile, work_mode, overwrite_mode,
                    source_root_id, output_root_id, str(workspace_path),
                    "pending", now
                )
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
    ) -> None:
        """Update job status."""
        now = utc_now()
        
        with self.connection() as conn:
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
            
            values.append(job_id)
            
            conn.execute(
                f"UPDATE workflow_jobs SET {', '.join(fields)} WHERE job_id = ?",
                tuple(values)
            )

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


__all__ = ["default_workflow_database_path", "WorkflowDatabase"]
