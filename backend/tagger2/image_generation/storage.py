"""Durable SQLite storage for image generation jobs and artifacts."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..common import sha256_bytes as hash_bytes, utc_now
from ..storage import canonical_json, config_digest


JOB_STATES = frozenset({"queued", "running", "cancelling", "cancelled", "succeeded", "partial", "failed", "interrupted", "deleting"})
ATTEMPT_STATES = frozenset({"pending", "running", "succeeded", "failed", "cancelled"})
TERMINAL_STATES = frozenset({"cancelled", "succeeded", "partial", "failed"})
IMAGE_GENERATION_SCHEMA_VERSION = 1


def _json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class ImageGenerationStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._anchor: sqlite3.Connection | None = None
        self._target = self.path
        self._uri = False
        if self.path == ":memory:":
            self._uri = True
            self._target = f"file:image-generation-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._anchor = self._open()
        else:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._target, timeout=5.0, isolation_level=None, check_same_thread=False, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            yield connection
        finally:
            connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS image_generation_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS image_generation_jobs (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            phase TEXT NOT NULL,
            config_json TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model TEXT NOT NULL,
            family TEXT NOT NULL,
            operation TEXT NOT NULL,
            requested_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error_code TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_image_jobs_state_created ON image_generation_jobs(state, created_at);
        CREATE TABLE IF NOT EXISTS image_generation_references (
            job_id TEXT NOT NULL REFERENCES image_generation_jobs(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            size_bytes INTEGER NOT NULL,
            PRIMARY KEY(job_id, ordinal)
        );
        CREATE TABLE IF NOT EXISTS image_generation_attempts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES image_generation_jobs(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            requested_count INTEGER NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            parser_route TEXT,
            finish_reason TEXT,
            text_json TEXT NOT NULL DEFAULT '[]',
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_image_attempts_job_state ON image_generation_attempts(job_id, state, ordinal);
        CREATE TABLE IF NOT EXISTS image_generation_artifacts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES image_generation_jobs(id) ON DELETE CASCADE,
            attempt_id TEXT NOT NULL REFERENCES image_generation_attempts(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_image_artifacts_job ON image_generation_artifacts(job_id, ordinal);
        CREATE TABLE IF NOT EXISTS image_generation_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES image_generation_jobs(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_image_events_job_seq ON image_generation_events(job_id, seq);
        """
        with self._lock, self.connection() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > IMAGE_GENERATION_SCHEMA_VERSION:
                raise RuntimeError(
                    "image generation database schema is newer than this application"
                )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(schema)
            connection.execute(
                f"PRAGMA user_version={IMAGE_GENERATION_SCHEMA_VERSION}"
            )
            connection.execute(
                """INSERT INTO image_generation_meta(key,value) VALUES('schema_version',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (str(IMAGE_GENERATION_SCHEMA_VERSION),),
            )

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def recover_interrupted(self) -> list[str]:
        recovered: list[str] = []
        now = utc_now()
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id,state FROM image_generation_jobs WHERE state IN ('running','cancelling')"
            ).fetchall()
            recovered = [str(row["id"]) for row in rows if str(row["state"]) == "running"]
            cancelling = [str(row["id"]) for row in rows if str(row["state"]) == "cancelling"]
            if recovered:
                marks = ",".join("?" for _ in recovered)
                connection.execute(
                    f"UPDATE image_generation_jobs SET state='interrupted',phase='interrupted',updated_at=?,error_code='job_interrupted',error_message='job interrupted by restart' WHERE id IN ({marks})",
                    (now, *recovered),
                )
                connection.execute(
                    f"UPDATE image_generation_attempts SET state='pending',updated_at=?,error_code=NULL,error_message=NULL WHERE state='running' AND job_id IN ({marks})",
                    (now, *recovered),
                )
                for job_id in recovered:
                    self._event(connection, job_id, "interrupted", {"reason": "restart"}, now)
            if cancelling:
                marks = ",".join("?" for _ in cancelling)
                connection.execute(
                    f"UPDATE image_generation_jobs SET state='cancelled',phase='cancelled',updated_at=?,finished_at=?,error_code=NULL,error_message=NULL WHERE id IN ({marks})",
                    (now, now, *cancelling),
                )
                connection.execute(
                    f"UPDATE image_generation_attempts SET state='cancelled',updated_at=?,error_code='image_job_cancelled',error_message='cancelled during restart' WHERE state IN ('pending','running') AND job_id IN ({marks})",
                    (now, *cancelling),
                )
                for job_id in cancelling:
                    self._event(connection, job_id, "cancelled", {"reason": "restart"}, now)
        return recovered

    def job_ids(self, state: str) -> list[str]:
        """Return a stable snapshot of job IDs before workers mutate state."""

        if state not in JOB_STATES:
            raise ValueError(f"invalid image job state: {state}")
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM image_generation_jobs WHERE state=? ORDER BY created_at, id",
                (state,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def create_job(
        self,
        *,
        job_id: str,
        provider_id: str,
        model: str,
        family: str,
        operation: str,
        requested_count: int,
        config: Mapping[str, Any],
        attempts: int,
        references: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        safe_config = dict(config)
        digest = config_digest(safe_config)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO image_generation_jobs
                (id,state,phase,config_json,config_hash,provider_id,model,family,operation,requested_count,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "queued", "queued", canonical_json(safe_config), digest, provider_id, model, family, operation, requested_count, now, now),
            )
            for reference in references:
                connection.execute(
                    """INSERT INTO image_generation_references
                    (job_id,ordinal,relative_path,sha256,mime_type,width,height,size_bytes)
                    VALUES(?,?,?,?,?,?,?,?)""",
                    (job_id, reference["ordinal"], reference["relative_path"], reference["sha256"], reference["mime_type"], reference.get("width"), reference.get("height"), reference["size_bytes"]),
                )
            for ordinal in range(attempts):
                connection.execute(
                    """INSERT INTO image_generation_attempts
                    (id,job_id,ordinal,requested_count,state,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (uuid.uuid4().hex, job_id, ordinal, 1 if attempts > 1 else requested_count, "pending", now, now),
                )
            self._event(connection, job_id, "queued", {"requested_count": requested_count}, now)
        return self.get_job(job_id) or {}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM image_generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            return self._public_job(connection, row)

    def list_jobs(self, *, limit: int = 50, offset: int = 0, query: str = "", state: str | None = None) -> tuple[list[dict[str, Any]], int]:
        conditions: list[str] = []
        params: list[Any] = []
        if state:
            conditions.append("state=?")
            params.append(state)
        if query.strip():
            conditions.append("(model LIKE ? OR provider_id LIKE ? OR config_json LIKE ?)")
            needle = f"%{query.strip()}%"
            params.extend((needle, needle, needle))
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connection() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) FROM image_generation_jobs{where}", params).fetchone()[0])
            rows = connection.execute(
                f"SELECT * FROM image_generation_jobs{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(200, int(limit))), max(0, int(offset))),
            ).fetchall()
            return [self._public_job(connection, row) for row in rows], total

    def get_references(self, job_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM image_generation_references WHERE job_id=? ORDER BY ordinal", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def claim_attempt(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM image_generation_attempts WHERE job_id=? AND state='pending' ORDER BY ordinal LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE image_generation_attempts SET state='running',attempts=attempts+1,updated_at=? WHERE id=? AND state='pending'",
                (now, row["id"]),
            )
            connection.execute(
                "UPDATE image_generation_jobs SET state='running',phase='generating',started_at=COALESCE(started_at,?),updated_at=? WHERE id=? AND state IN ('queued','interrupted','running')",
                (now, now, job_id),
            )
            self._event(connection, job_id, "attempt_started", {"ordinal": row["ordinal"]}, now)
            current = connection.execute("SELECT * FROM image_generation_attempts WHERE id=?", (row["id"],)).fetchone()
            return dict(current) if current else None

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        parser_route: str | None = None,
        finish_reason: str | None = None,
        texts: Sequence[str] = (),
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if state not in ATTEMPT_STATES:
            raise ValueError(f"invalid image attempt state: {state}")
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT job_id FROM image_generation_attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            connection.execute(
                """UPDATE image_generation_attempts SET state=?,parser_route=?,finish_reason=?,text_json=?,error_code=?,error_message=?,updated_at=? WHERE id=?""",
                (state, parser_route, finish_reason, canonical_json(list(texts)[:16]), error_code, str(error_message)[:500] if error_message else None, now, attempt_id),
            )
            self._event(connection, str(row["job_id"]), "attempt_finished", {"attempt_id": attempt_id, "state": state, "error_code": error_code}, now)

    def release_attempt(self, attempt_id: str) -> None:
        """Return an in-flight attempt to pending during application shutdown."""

        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT job_id,state FROM image_generation_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if str(row["state"]) != "running":
                return
            connection.execute(
                "UPDATE image_generation_attempts SET state='pending',error_code=NULL,error_message=NULL,updated_at=? WHERE id=?",
                (now, attempt_id),
            )
            self._event(
                connection,
                str(row["job_id"]),
                "attempt_released",
                {"attempt_id": attempt_id, "reason": "shutdown"},
                now,
            )

    def record_artifact(self, *, job_id: str, attempt_id: str, ordinal: int, relative_path: str, mime_type: str, width: int | None, height: int | None, data: bytes, source: str) -> dict[str, Any]:
        artifact_id = uuid.uuid4().hex
        now = utc_now()
        digest = hash_bytes(data)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO image_generation_artifacts
                (id,job_id,attempt_id,ordinal,relative_path,mime_type,width,height,size_bytes,sha256,source,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,ordinal) DO UPDATE SET
                    attempt_id=excluded.attempt_id,relative_path=excluded.relative_path,
                    mime_type=excluded.mime_type,width=excluded.width,height=excluded.height,
                    size_bytes=excluded.size_bytes,sha256=excluded.sha256,source=excluded.source""",
                (artifact_id, job_id, attempt_id, ordinal, relative_path, mime_type, width, height, len(data), digest, source, now),
            )
            row = connection.execute(
                "SELECT * FROM image_generation_artifacts WHERE job_id=? AND ordinal=?",
                (job_id, ordinal),
            ).fetchone()
        return dict(row) if row else {"id": artifact_id, "job_id": job_id, "attempt_id": attempt_id, "ordinal": ordinal, "relative_path": relative_path, "mime_type": mime_type, "width": width, "height": height, "size_bytes": len(data), "sha256": digest, "source": source, "created_at": now}

    def finalize_job(self, job_id: str, *, state: str, error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]:
        if state not in JOB_STATES:
            raise ValueError(f"invalid image job state: {state}")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE image_generation_jobs SET state=?,phase=?,updated_at=?,finished_at=?,error_code=?,error_message=? WHERE id=?",
                (state, state, now, now if state in TERMINAL_STATES else None, error_code, str(error_message)[:500] if error_message else None, job_id),
            )
            self._event(connection, job_id, state, {"error_code": error_code}, now)
        return self.get_job(job_id) or {}

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM image_generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = str(row["state"])
            if state in TERMINAL_STATES:
                return self.get_job(job_id) or {}
            connection.execute("UPDATE image_generation_jobs SET state='cancelling',phase='cancelling',updated_at=? WHERE id=?", (now, job_id))
            connection.execute("UPDATE image_generation_attempts SET state='cancelled',updated_at=? WHERE job_id=? AND state='pending'", (now, job_id))
            self._event(connection, job_id, "cancelling", {}, now)
        return self.get_job(job_id) or {}

    def reset_retryable(self, job_id: str) -> int:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM image_generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            job_state = str(row["state"])
            states = ("failed", "cancelled")
            placeholders = ",".join("?" for _ in states)
            count = int(connection.execute(
                f"SELECT COUNT(*) FROM image_generation_attempts WHERE job_id=? AND state IN ({placeholders})",
                (job_id, *states),
            ).fetchone()[0])
            # A candidate-count request can complete its attempt while still
            # producing fewer images than requested. Re-run that attempt so a
            # partial job is actually retryable instead of becoming stuck.
            if count == 0 and job_state == "partial":
                count = int(connection.execute(
                    "SELECT COUNT(*) FROM image_generation_attempts WHERE job_id=? AND state='succeeded'",
                    (job_id,),
                ).fetchone()[0])
                if count:
                    connection.execute(
                        "UPDATE image_generation_attempts SET state='pending',error_code=NULL,error_message=NULL,updated_at=? WHERE job_id=? AND state='succeeded'",
                        (now, job_id),
                    )
            if count:
                connection.execute(
                    f"UPDATE image_generation_attempts SET state='pending',error_code=NULL,error_message=NULL,updated_at=? WHERE job_id=? AND state IN ({placeholders})",
                    (now, job_id, *states),
                )
                connection.execute("UPDATE image_generation_jobs SET state='queued',phase='queued',finished_at=NULL,error_code=NULL,error_message=NULL,updated_at=? WHERE id=?", (now, job_id))
                self._event(connection, job_id, "retry_queued", {"count": count}, now)
            return count

    # Backwards-compatible internal name used by older callers.
    reset_failed = reset_retryable

    def list_artifacts(self, job_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM image_generation_artifacts WHERE job_id=? ORDER BY ordinal", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_attempt_artifacts(self, attempt_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM image_generation_artifacts WHERE attempt_id=? ORDER BY ordinal",
                (attempt_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM image_generation_artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None

    def mark_deleting(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM image_generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = str(row["state"])
            if state not in TERMINAL_STATES and state != "deleting":
                raise ValueError("job is not terminal")
            if state != "deleting":
                connection.execute(
                    "UPDATE image_generation_jobs SET state='deleting',phase='deleting',updated_at=? WHERE id=?",
                    (now, job_id),
                )
                self._event(connection, job_id, "deleting", {}, now)
        return self.get_job(job_id) or {}

    def delete_job(self, job_id: str) -> bool:
        with self.transaction() as connection:
            row = connection.execute("SELECT state FROM image_generation_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return False
            if str(row["state"]) not in TERMINAL_STATES and str(row["state"]) != "deleting":
                raise ValueError("job is not terminal")
            connection.execute("DELETE FROM image_generation_jobs WHERE id=?", (job_id,))
            return True

    def _event(self, connection: sqlite3.Connection, job_id: str, event_type: str, payload: Mapping[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO image_generation_events(job_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (job_id, event_type, canonical_json(dict(payload)), now),
        )

    def _public_job(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        job_id = str(row["id"])
        attempts = connection.execute("SELECT state,COUNT(*) AS count FROM image_generation_attempts WHERE job_id=? GROUP BY state", (job_id,)).fetchall()
        counts = {str(item["state"]): int(item["count"]) for item in attempts}
        artifacts = [dict(item) for item in connection.execute("SELECT id,ordinal,mime_type,width,height,size_bytes,sha256,source FROM image_generation_artifacts WHERE job_id=? ORDER BY ordinal", (job_id,)).fetchall()]
        config = _json(row["config_json"], {})
        return {
            "id": job_id,
            "state": row["state"],
            "phase": row["phase"],
            "provider_id": row["provider_id"],
            "model": row["model"],
            "family": row["family"],
            "operation": row["operation"],
            "requested_count": int(row["requested_count"]),
            "completed_count": len(artifacts),
            "attempt_counts": counts,
            "config": config,
            "config_hash": row["config_hash"],
            "reference_count": int(connection.execute("SELECT COUNT(*) FROM image_generation_references WHERE job_id=?", (job_id,)).fetchone()[0]),
            "artifacts": artifacts,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }


__all__ = [
    "IMAGE_GENERATION_SCHEMA_VERSION",
    "ImageGenerationStorage",
    "JOB_STATES",
    "TERMINAL_STATES",
    "hash_bytes",
    "utc_now",
]
