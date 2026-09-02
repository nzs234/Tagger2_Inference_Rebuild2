"""SQLite WAL persistence for jobs, items, events, profiles and artifacts."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


JOB_STATES = frozenset(
    {"queued", "running", "paused", "cancelling", "cancelled", "succeeded", "failed", "interrupted"}
)
ITEM_STATES = frozenset({"pending", "running", "succeeded", "failed", "skipped", "cancelled"})
TERMINAL_JOB_STATES = frozenset({"cancelled", "succeeded", "failed"})
TERMINAL_ITEM_STATES = frozenset({"succeeded", "failed", "skipped", "cancelled"})

VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelling", "cancelled", "failed", "interrupted"},
    "running": {"paused", "cancelling", "cancelled", "succeeded", "failed", "interrupted"},
    "paused": {"running", "cancelling", "cancelled", "failed", "interrupted"},
    "cancelling": {"cancelled", "failed", "interrupted"},
    "cancelled": {"queued"},
    "succeeded": {"queued"},
    "failed": {"queued"},
    "interrupted": {"queued", "running", "cancelling", "cancelled", "failed"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def config_digest(value: Mapping[str, Any] | str) -> str:
    encoded = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_SECRET_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api_keys",
        "apikeys",
        "authorization",
        "password",
        "secret",
        "token",
        "hf_token",
        "hftoken",
    }
)


def redact_secrets(value: Any) -> Any:
    """Recursively remove credentials before persistence or event logging."""

    if hasattr(value, "model_dump"):
        value = value.model_dump()  # type: ignore[union-attr]
    if isinstance(value, Mapping):
        return {
            str(key): ("[configured]" if str(key).casefold() in _SECRET_NAMES and item else redact_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    return value


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _bounded(value: str | None, limit: int = 4096) -> str | None:
    if value is None:
        return None
    value = str(value).replace("\x00", "")
    return value if len(value) <= limit else value[: limit - 3] + "..."


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    mode: str
    state: str
    phase: str
    config: dict[str, Any]
    config_hash: str
    source_root_id: str | None
    output_root_id: str | None
    total: int
    processed: int
    succeeded: int
    skipped: int
    failed: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class JobItemRecord:
    id: str
    job_id: str
    ordinal: int
    image_id: str
    source_root_id: str | None
    relative_path: str
    source_hash: str | None
    config_hash: str
    status: str
    attempts: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None
    duration_ms: float | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class EventRecord:
    seq: int
    job_id: str
    created_at: str
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    job_id: str
    item_id: str
    kind: str
    path: str
    source_hash: str
    config_hash: str
    schema_version: str
    content_hash: str
    created_at: str


class SQLiteStorage:
    """Small synchronous repository with one short-lived connection per call.

    SQLite's WAL and busy timeout allow worker threads and the API thread to
    operate concurrently.  Transactions that claim or transition work use
    ``BEGIN IMMEDIATE`` so an item cannot be processed twice.
    """

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = str(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._write_lock = threading.RLock()
        self._anchor: sqlite3.Connection | None = None
        self._uri = False
        self._connect_target = self.path
        if self.path == ":memory:":
            self._uri = True
            self._connect_target = f"file:tagger2-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._anchor = self._open()
        else:
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._connect_target,
            timeout=max(0.1, self.busy_timeout_ms / 1000),
            isolation_level=None,
            check_same_thread=False,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
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
        with self._write_lock, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def close(self) -> None:
        if self._anchor is not None:
            self._anchor.close()
            self._anchor = None

    def __enter__(self) -> "SQLiteStorage":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK(mode IN ('local','online')),
            state TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'queued',
            config_json TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            source_root_id TEXT,
            output_root_id TEXT,
            total INTEGER NOT NULL DEFAULT 0,
            processed INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_state_created ON jobs(state, created_at);

        CREATE TABLE IF NOT EXISTS job_items (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            image_id TEXT NOT NULL,
            source_root_id TEXT,
            relative_path TEXT NOT NULL,
            source_hash TEXT,
            config_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            result_json TEXT,
            error TEXT,
            duration_ms REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(job_id, image_id)
        );
        CREATE INDEX IF NOT EXISTS idx_items_job_status_ordinal ON job_items(job_id, status, ordinal);

        CREATE TABLE IF NOT EXISTS job_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            event_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_job_seq ON job_events(job_id, seq);

        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            item_id TEXT NOT NULL REFERENCES job_items(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(item_id, kind, path)
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_lookup ON artifacts(item_id, source_hash, config_hash, schema_version);
        CREATE INDEX IF NOT EXISTS idx_artifacts_path_lookup ON artifacts(path, kind, source_hash, config_hash, schema_version);

        CREATE TABLE IF NOT EXISTS provider_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            base_url TEXT NOT NULL,
            config_json TEXT NOT NULL,
            secret_ref TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS model_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            config_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        PRAGMA user_version=1;
        """
        with self._write_lock, self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(schema)
        self.recover_interrupted()

    def recover_interrupted(self) -> list[str]:
        """Mark orphaned work interrupted and make running items resumable."""

        now = utc_now()
        recovered: list[str] = []
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE state IN ('running','cancelling') ORDER BY created_at"
            ).fetchall()
            recovered = [str(row["id"]) for row in rows]
            if not recovered:
                return []
            placeholders = ",".join("?" for _ in recovered)
            connection.execute(
                f"UPDATE jobs SET state='interrupted', phase='interrupted', updated_at=?, error=? WHERE id IN ({placeholders})",
                (now, "process stopped before the job completed", *recovered),
            )
            connection.execute(
                f"UPDATE job_items SET status='pending', updated_at=?, error=NULL WHERE status='running' AND job_id IN ({placeholders})",
                (now, *recovered),
            )
            for job_id in recovered:
                self._insert_event(
                    connection,
                    job_id,
                    {"job_id": job_id, "state": "interrupted", "phase": "interrupted", "error": "job interrupted by restart"},
                    now,
                )
        return recovered

    def create_job(
        self,
        mode: str,
        config: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]] = (),
        *,
        source_root_id: str | None = None,
        output_root_id: str | None = None,
        job_id: str | None = None,
    ) -> JobRecord:
        if not isinstance(items, Sequence):
            items = list(items)
        mode = str(getattr(mode, "value", mode)).lower()
        if mode not in {"local", "online"}:
            raise ValueError("job mode must be local or online")
        job_id = job_id or uuid.uuid4().hex
        safe_config = redact_secrets(dict(config))
        # Hash the executable config, but never credentials.  Secret rotation
        # should not invalidate otherwise identical on-disk captions.
        digest = config_digest(safe_config)
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO jobs
                (id,mode,state,phase,config_json,config_hash,source_root_id,output_root_id,total,created_at,updated_at)
                VALUES(?,?,'queued','queued',?,?,?,?,?,?,?)""",
                (
                    job_id,
                    mode,
                    canonical_json(safe_config),
                    digest,
                    source_root_id,
                    output_root_id,
                    len(items),
                    now,
                    now,
                ),
            )
            for ordinal, item in enumerate(items):
                self._insert_item(connection, job_id, ordinal, item, digest, source_root_id, now)
            self._insert_event(
                connection,
                job_id,
                {
                    "job_id": job_id,
                    "state": "queued",
                    "phase": "queued",
                    "processed": 0,
                    "total": len(items),
                    "succeeded": 0,
                    "skipped": 0,
                    "failed": 0,
                },
                now,
            )
        record = self.get_job(job_id)
        if record is None:
            raise RuntimeError(f"job {job_id} missing after create_job insert")
        return record

    def _insert_item(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        ordinal: int,
        item: Mapping[str, Any],
        digest: str,
        default_root: str | None,
        now: str,
    ) -> str:
        item_id = str(item.get("id") or uuid.uuid4().hex)
        image_id = str(item.get("image_id") or item.get("upload_id") or item_id)
        relative_path = str(item.get("relative_path") or item.get("file_name") or image_id)
        payload = redact_secrets(dict(item.get("payload") or {}))
        connection.execute(
            """INSERT INTO job_items
            (id,job_id,ordinal,image_id,source_root_id,relative_path,source_hash,config_hash,status,payload_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (
                item_id,
                job_id,
                ordinal,
                image_id,
                item.get("source_root_id") or default_root,
                relative_path,
                item.get("source_hash"),
                digest,
                canonical_json(payload),
                now,
                now,
            ),
        )
        return item_id

    def add_items(self, job_id: str, items: Sequence[Mapping[str, Any]]) -> list[str]:
        if not items:
            return []
        now = utc_now()
        result: list[str] = []
        with self.transaction() as connection:
            job = connection.execute("SELECT config_hash,source_root_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["state"] not in {"queued", "paused", "interrupted"}:
                raise ValueError("items can only be added before or while a job is paused")
            start = int(connection.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM job_items WHERE job_id=?", (job_id,)).fetchone()[0])
            for offset, item in enumerate(items):
                result.append(self._insert_item(connection, job_id, start + offset, item, job["config_hash"], job["source_root_id"], now))
            connection.execute("UPDATE jobs SET total=total+?, updated_at=? WHERE id=?", (len(items), now, job_id))
        return result

    def get_job(self, job_id: str) -> JobRecord | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_record(row) if row else None

    def list_jobs(self, *, state: str | None = None, limit: int = 100, offset: int = 0) -> list[JobRecord]:
        query = "SELECT * FROM jobs"
        params: list[Any] = []
        if state:
            query += " WHERE state=?"
            params.append(state)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend((max(1, min(1000, int(limit))), max(0, int(offset))))
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._job_record(row) for row in rows]

    def transition_job(
        self,
        job_id: str,
        state: str,
        *,
        phase: str | None = None,
        error: str | None = None,
        force: bool = False,
        event: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        state = str(getattr(state, "value", state)).lower()
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state: {state}")
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            old = str(row["state"])
            if state != old and not force and state not in VALID_TRANSITIONS.get(old, set()):
                raise ValueError(f"invalid job transition: {old} -> {state}")
            started = row["started_at"] or (now if state == "running" else None)
            finished = now if state in TERMINAL_JOB_STATES else None
            connection.execute(
                """UPDATE jobs SET state=?,phase=?,error=?,updated_at=?,started_at=?,finished_at=? WHERE id=?""",
                (state, phase or state, _bounded(error), now, started, finished, job_id),
            )
            if state in TERMINAL_JOB_STATES:
                # Full accuracy matters at terminal states: reconcile the
                # counters from the item table instead of trusting the values
                # that item updates maintain incrementally.
                self._refresh_counters(connection, job_id, now)
            current = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            data = self._event_from_row(current)
            if event:
                data.update(redact_secrets(dict(event)))
            self._insert_event(connection, job_id, data, now)
        record = self.get_job(job_id)
        if record is None:
            raise RuntimeError(f"job {job_id} missing after transition to {state}")
        return record

    set_job_state = transition_job

    def claim_next_item(self, job_id: str) -> JobItemRecord | None:
        now = utc_now()
        with self.transaction() as connection:
            job = connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["state"] != "running":
                return None
            row = connection.execute(
                "SELECT * FROM job_items WHERE job_id=? AND status='pending' ORDER BY ordinal LIMIT 1", (job_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE job_items SET status='running',attempts=attempts+1,error=NULL,updated_at=? WHERE id=? AND status='pending'",
                (now, row["id"]),
            )
            current = connection.execute("SELECT * FROM job_items WHERE id=?", (row["id"],)).fetchone()
            job_row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            event = self._event_from_row(job_row)
            event.update({"phase": "processing", "current_item": current["image_id"]})
            self._insert_event(connection, job_id, event, now)
        return self._item_record(current)

    def update_item(
        self,
        item_id: str,
        status: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float | None = None,
        source_hash: str | None = None,
    ) -> JobItemRecord:
        status = str(getattr(status, "value", status)).lower()
        if status not in ITEM_STATES:
            raise ValueError(f"unknown item state: {status}")
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT job_id,status FROM job_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            allowed = {
                "pending": {"running", "skipped", "cancelled"},
                "running": {"succeeded", "failed", "skipped", "cancelled", "pending"},
                "failed": {"pending", "running"},
                "cancelled": {"pending"},
                "succeeded": set(),
                "skipped": set(),
            }
            if status != row["status"] and status not in allowed.get(str(row["status"]), set()):
                raise ValueError(f"invalid item transition: {row['status']} -> {status}")
            connection.execute(
                """UPDATE job_items SET status=?,result_json=?,error=?,duration_ms=?,
                source_hash=COALESCE(?,source_hash),updated_at=? WHERE id=?""",
                (
                    status,
                    canonical_json(redact_secrets(result)) if result is not None else None,
                    _bounded(error),
                    duration_ms,
                    source_hash,
                    now,
                    item_id,
                ),
            )
            self._bump_job_counters(connection, str(row["job_id"]), str(row["status"]), status, now)
            current = connection.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            self._insert_event(connection, str(row["job_id"]), self._event_from_row(job), now)
        return self._item_record(current)

    def reset_failed_items(self, job_id: str) -> int:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE job_items SET status='pending',result_json=NULL,error=NULL,duration_ms=NULL,updated_at=? WHERE job_id=? AND status='failed'",
                (now, job_id),
            )
            count = cursor.rowcount
            if count:
                connection.execute(
                    "UPDATE jobs SET state='queued',phase='queued',error=NULL,finished_at=NULL,updated_at=? WHERE id=?",
                    (now, job_id),
                )
                self._refresh_counters(connection, job_id, now)
                row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                self._insert_event(connection, job_id, self._event_from_row(row), now)
        return count

    def cancel_pending_items(self, job_id: str) -> int:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE job_items SET status='cancelled',updated_at=? WHERE job_id=? AND status IN ('pending','running')",
                (now, job_id),
            )
            self._refresh_counters(connection, job_id, now)
        return cursor.rowcount

    def refresh_job_counters(self, job_id: str) -> JobRecord:
        """Force a full counter recomputation for one job and return the record.

        Reconciliation entry point for callers that need guaranteed accuracy
        (for example after out-of-band item changes).  Day-to-day item updates
        maintain the counters incrementally instead.
        """

        now = utc_now()
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is None:
                raise KeyError(job_id)
            self._refresh_counters(connection, job_id, now)
        record = self.get_job(job_id)
        if record is None:
            raise RuntimeError(f"job {job_id} missing after counter refresh")
        return record

    def list_items(
        self,
        job_id: str,
        *,
        status: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[JobItemRecord]:
        query = "SELECT * FROM job_items WHERE job_id=?"
        params: list[Any] = [job_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY ordinal LIMIT ? OFFSET ?"
        params.extend((max(1, min(10000, int(limit))), max(0, int(offset))))
        with self.connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._item_record(row) for row in rows]

    def get_item(self, item_id: str) -> JobItemRecord | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
        return self._item_record(row) if row else None

    def append_event(self, job_id: str, data: Mapping[str, Any]) -> EventRecord:
        now = utc_now()
        with self.transaction() as connection:
            seq = self._insert_event(connection, job_id, data, now)
        return EventRecord(seq=seq, job_id=job_id, created_at=now, data=dict(redact_secrets(data)))

    def _insert_event(self, connection: sqlite3.Connection, job_id: str, data: Mapping[str, Any], now: str) -> int:
        payload = redact_secrets(dict(data))
        if payload.get("error"):
            payload["error"] = _bounded(str(payload["error"]))
        cursor = connection.execute(
            "INSERT INTO job_events(job_id,created_at,event_json) VALUES(?,?,?)",
            (job_id, now, canonical_json(payload)),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an event sequence")
        return int(cursor.lastrowid)

    def get_events(self, job_id: str, *, after_seq: int = 0, limit: int = 500) -> list[EventRecord]:
        with self.connection() as connection:
            events, _ = self._read_events_since(connection, job_id, after_seq, limit)
        return events

    def read_events_since(
        self,
        job_id: str,
        after_seq: int = 0,
        *,
        limit: int = 500,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[list[EventRecord], str | None]:
        """Read new events plus the current job state over a single connection.

        The SSE poll loop keeps one read connection open for the lifetime of a
        stream and passes it back on every iteration; without ``connection``
        this opens a short-lived one, which also lets a broken connection
        recover on the next call.  The returned state is ``None`` when the job
        no longer exists.
        """

        if connection is None:
            with self.connection() as owned:
                return self._read_events_since(owned, job_id, after_seq, limit)
        return self._read_events_since(connection, job_id, after_seq, limit)

    @staticmethod
    def _read_events_since(
        connection: sqlite3.Connection, job_id: str, after_seq: int, limit: int
    ) -> tuple[list[EventRecord], str | None]:
        rows = connection.execute(
            "SELECT * FROM job_events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
            (job_id, max(0, int(after_seq)), max(1, min(5000, int(limit)))),
        ).fetchall()
        events = [
            EventRecord(seq=int(row["seq"]), job_id=row["job_id"], created_at=row["created_at"], data=_json_load(row["event_json"], {}))
            for row in rows
        ]
        job_row = connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        return events, (str(job_row["state"]) if job_row is not None else None)

    def get_latest_event(self, job_id: str) -> EventRecord | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? ORDER BY seq DESC LIMIT 1", (job_id,)
            ).fetchone()
        if row is None:
            return None
        return EventRecord(
            seq=int(row["seq"]),
            job_id=row["job_id"],
            created_at=row["created_at"],
            data=_json_load(row["event_json"], {}),
        )

    def record_artifact(
        self,
        *,
        job_id: str,
        item_id: str,
        kind: str,
        path: str | Path,
        source_hash: str,
        config_hash: str,
        schema_version: str,
        content_hash: str,
        artifact_id: str | None = None,
    ) -> ArtifactRecord:
        artifact_id = artifact_id or uuid.uuid4().hex
        now = utc_now()
        normalized_path = str(Path(path).resolve())
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO artifacts
                (id,job_id,item_id,kind,path,source_hash,config_hash,schema_version,content_hash,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id,kind,path) DO UPDATE SET
                    source_hash=excluded.source_hash,config_hash=excluded.config_hash,
                    schema_version=excluded.schema_version,content_hash=excluded.content_hash,created_at=excluded.created_at""",
                (
                    artifact_id,
                    job_id,
                    item_id,
                    kind,
                    normalized_path,
                    source_hash,
                    config_hash,
                    schema_version,
                    content_hash,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE item_id=? AND kind=? AND path=?", (item_id, kind, normalized_path)
            ).fetchone()
        return self._artifact_record(row)

    def find_artifact(
        self,
        item_id: str | None = None,
        *,
        kind: str = "anima_json",
        path: str | Path | None = None,
        source_hash: str | None = None,
        config_hash: str | None = None,
        schema_version: str | None = None,
    ) -> ArtifactRecord | None:
        query = "SELECT * FROM artifacts WHERE kind=?"
        params: list[Any] = [kind]
        if item_id is not None:
            query += " AND item_id=?"
            params.append(item_id)
        if path is not None:
            query += " AND path=?"
            params.append(str(Path(path).resolve()))
        for column, value in (("source_hash", source_hash), ("config_hash", config_hash), ("schema_version", schema_version)):
            if value is not None:
                query += f" AND {column}=?"
                params.append(value)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self.connection() as connection:
            row = connection.execute(query, params).fetchone()
        return self._artifact_record(row) if row else None

    def list_artifacts(self, item_id: str) -> list[ArtifactRecord]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM artifacts WHERE item_id=? ORDER BY created_at", (item_id,)).fetchall()
        return [self._artifact_record(row) for row in rows]

    def upsert_provider_profile(
        self,
        profile_id: str,
        *,
        name: str,
        kind: str,
        base_url: str,
        config: Mapping[str, Any],
        secret_ref: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        safe_config = redact_secrets(dict(config))
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO provider_profiles(id,name,kind,base_url,config_json,secret_ref,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,kind=excluded.kind,base_url=excluded.base_url,config_json=excluded.config_json,
                secret_ref=excluded.secret_ref,enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (profile_id, name, kind, base_url, canonical_json(safe_config), secret_ref, int(enabled), now, now),
            )
        return self.get_provider_profile(profile_id) or {}

    @staticmethod
    def _provider_profile_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "base_url": row["base_url"],
            "config": _json_load(row["config_json"], {}),
            "secret_ref": row["secret_ref"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_provider_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM provider_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._provider_profile_record(row) if row is not None else None

    def list_provider_profiles(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM provider_profiles ORDER BY name").fetchall()
        return [self._provider_profile_record(row) for row in rows]

    def delete_provider_profile(self, profile_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM provider_profiles WHERE id=?", (profile_id,))
        return cursor.rowcount > 0

    def get_metadata(self, key: str) -> str | None:
        with self.connection() as connection:
            row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None

    def set_metadata(self, key: str, value: str) -> None:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO app_metadata(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (key, value, now),
            )

    def upsert_model_profile(self, profile_id: str, *, name: str, config: Mapping[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO model_profiles(id,name,config_json,created_at,updated_at) VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,config_json=excluded.config_json,updated_at=excluded.updated_at""",
                (profile_id, name, canonical_json(redact_secrets(dict(config))), now, now),
            )
        return {"id": profile_id, "name": name, "config": dict(config), "updated_at": now}

    @staticmethod
    def _model_profile_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "config": _json_load(row["config_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_model_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM model_profiles WHERE id=?", (profile_id,)).fetchone()
        return self._model_profile_record(row) if row is not None else None

    def list_model_profiles(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM model_profiles ORDER BY name").fetchall()
        return [self._model_profile_record(row) for row in rows]

    def delete_model_profile(self, profile_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM model_profiles WHERE id=?", (profile_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _job_record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            mode=row["mode"],
            state=row["state"],
            phase=row["phase"],
            config=_json_load(row["config_json"], {}),
            config_hash=row["config_hash"],
            source_root_id=row["source_root_id"],
            output_root_id=row["output_root_id"],
            total=int(row["total"]),
            processed=int(row["processed"]),
            succeeded=int(row["succeeded"]),
            skipped=int(row["skipped"]),
            failed=int(row["failed"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )

    @staticmethod
    def _item_record(row: sqlite3.Row) -> JobItemRecord:
        return JobItemRecord(
            id=row["id"],
            job_id=row["job_id"],
            ordinal=int(row["ordinal"]),
            image_id=row["image_id"],
            source_root_id=row["source_root_id"],
            relative_path=row["relative_path"],
            source_hash=row["source_hash"],
            config_hash=row["config_hash"],
            status=row["status"],
            attempts=int(row["attempts"]),
            payload=_json_load(row["payload_json"], {}),
            result=_json_load(row["result_json"], None),
            error=row["error"],
            duration_ms=float(row["duration_ms"]) if row["duration_ms"] is not None else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _artifact_record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            job_id=row["job_id"],
            item_id=row["item_id"],
            kind=row["kind"],
            path=row["path"],
            source_hash=row["source_hash"],
            config_hash=row["config_hash"],
            schema_version=row["schema_version"],
            content_hash=row["content_hash"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _bump_job_counters(
        connection: sqlite3.Connection, job_id: str, from_status: str, to_status: str, now: str
    ) -> None:
        """Apply the counter deltas of one item status transition in place.

        Runs inside the caller's transaction so the item update and the job
        counters commit atomically.  ``total`` never changes here (items are
        only added by ``create_job``/``add_items``); processed/succeeded/
        skipped/failed move by the difference in terminal-state membership
        between the old and new item statuses.
        """

        if to_status == from_status:
            return
        deltas = (
            int(to_status in TERMINAL_ITEM_STATES) - int(from_status in TERMINAL_ITEM_STATES),
            int(to_status == "succeeded") - int(from_status == "succeeded"),
            int(to_status == "skipped") - int(from_status == "skipped"),
            int(to_status == "failed") - int(from_status == "failed"),
        )
        if not any(deltas):
            return
        connection.execute(
            """UPDATE jobs SET processed=processed+?,succeeded=succeeded+?,skipped=skipped+?,
            failed=failed+?,updated_at=? WHERE id=?""",
            (*deltas, now, job_id),
        )

    @staticmethod
    def _refresh_counters(connection: sqlite3.Connection, job_id: str, now: str) -> None:
        """Reconcile a job's counters with a full aggregation over its items.

        Not used on the per-item hot path (``update_item`` maintains the
        counters incrementally via ``_bump_job_counters``); reserved for bulk
        item changes and terminal job transitions where full accuracy matters.
        """

        counts = connection.execute(
            """SELECT COUNT(*) AS total,
            SUM(CASE WHEN status IN ('succeeded','failed','skipped','cancelled') THEN 1 ELSE 0 END) AS processed,
            SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
            SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
            FROM job_items WHERE job_id=?""",
            (job_id,),
        ).fetchone()
        connection.execute(
            "UPDATE jobs SET total=?,processed=?,succeeded=?,skipped=?,failed=?,updated_at=? WHERE id=?",
            tuple(int(counts[name] or 0) for name in ("total", "processed", "succeeded", "skipped", "failed"))
            + (now, job_id),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": row["id"],
            "state": row["state"],
            "phase": row["phase"],
            "processed": int(row["processed"]),
            "total": int(row["total"]),
            "succeeded": int(row["succeeded"]),
            "skipped": int(row["skipped"]),
            "failed": int(row["failed"]),
            "current_item": None,
            "error": row["error"],
        }


Database = SQLiteStorage
Storage = SQLiteStorage
JobStore = SQLiteStorage


__all__ = [
    "ArtifactRecord",
    "Database",
    "EventRecord",
    "ITEM_STATES",
    "JOB_STATES",
    "JobItemRecord",
    "JobRecord",
    "JobStore",
    "SQLiteStorage",
    "Storage",
    "TERMINAL_ITEM_STATES",
    "TERMINAL_JOB_STATES",
    "canonical_json",
    "config_digest",
    "redact_secrets",
    "utc_now",
]
