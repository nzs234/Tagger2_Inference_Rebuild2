"""Workflow database schema and migrations."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

# Migration checksums are deliberately immutable.  Do not derive the checksum
# from ``SCHEMA_SQL``: changing the current schema must not make an old
# migration appear to have changed underneath an existing database.
MIGRATION_CHECKSUMS = {
    1: "workflow-schema-v1",
    2: "workflow-schema-v2-leases",
    3: "workflow-schema-v3-job-states",
    4: "workflow-schema-v4-restore-discard-state",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    schema_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    job_id TEXT PRIMARY KEY,
    config_version INTEGER NOT NULL CHECK(config_version IN (1, 2)),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    profile TEXT NOT NULL CHECK(profile IN ('e621', 'danbooru')),
    work_mode TEXT NOT NULL CHECK(work_mode IN ('in_place', 'full_copy')),
    overwrite_mode TEXT NOT NULL CHECK(overwrite_mode IN ('incremental', 'rebuild')),
    source_root_id TEXT NOT NULL,
    output_root_id TEXT,
    workspace_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'queued', 'running', 'pausing', 'paused',
        'waiting_count_review', 'waiting_token_review', 'committing',
        'restoring', 'interrupted', 'rollback_required',
        'completed', 'failed', 'cancelling', 'cancelled'
    )),
    current_module_id TEXT,
    total_samples INTEGER NOT NULL DEFAULT 0,
    processed_samples INTEGER NOT NULL DEFAULT 0,
    succeeded_samples INTEGER NOT NULL DEFAULT 0,
    failed_samples INTEGER NOT NULL DEFAULT 0,
    skipped_samples INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    restored_at TEXT,
    discarded_at TEXT
);

-- Durable control-plane events.  ``event_id`` is the per-database monotonic
-- cursor used by the JSON/SSE adapters; payloads are server-side JSON and are
-- projected by the API rather than exposing workflow rows wholesale.
CREATE TABLE IF NOT EXISTS workflow_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_job_pins (
    job_id TEXT PRIMARY KEY REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    pinned_at TEXT NOT NULL
);

-- Durable execution records. These tables are intentionally append-friendly:
-- an operator can inspect a stage, operation or artifact after a worker has
-- exited, while the job row remains the compact current-state projection.
CREATE TABLE IF NOT EXISTS workflow_stage_runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    stage_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    batch_size INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    issue_count INTEGER NOT NULL DEFAULT 0,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_operations (
    operation_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_artifacts (
    artifact_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_resource_snapshots (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL,
    resource_fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, resource_id)
);

CREATE TABLE IF NOT EXISTS workflow_dataset_locks (
    lock_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    root_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_commit_journals (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sequence)
);

CREATE TABLE IF NOT EXISTS workflow_samples (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    relative_image_path TEXT NOT NULL,
    image_format TEXT NOT NULL CHECK(image_format IN ('jpeg', 'png', 'webp', 'bmp')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'completed', 'failed', 'skipped')),
    -- Lease bookkeeping (schema v2): a sample claimed by a worker records who
    -- holds it and when the claim expires, so an interrupted run is detectable
    -- instead of leaving a sample stuck in 'processing' forever.
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    current_module_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sample_id)
);

CREATE TABLE IF NOT EXISTS workflow_issues (
    issue_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    sample_id INTEGER,
    module_id TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info', 'warning', 'error')),
    blocking INTEGER NOT NULL CHECK(blocking IN (0, 1)),
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(job_id, sample_id) REFERENCES workflow_samples(job_id, sample_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_module_summary (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    module_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
    total INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    PRIMARY KEY(job_id, module_id)
);

CREATE TABLE IF NOT EXISTS workflow_resources (
    resource_id TEXT PRIMARY KEY,
    resource_fingerprint TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    source_url TEXT,
    source_timestamp TEXT,
    builder_version TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_count_review (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    count_value TEXT NOT NULL CHECK(count_value IN ('solo', 'duo', 'trio', 'group', 'unknown')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed')),
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sample_id),
    FOREIGN KEY(job_id, sample_id) REFERENCES workflow_samples(job_id, sample_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS workflow_token_budget_review (
    job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
    sample_id INTEGER NOT NULL,
    nl_text TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    token_limit INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('overflow', 'edited', 'recounted', 'rewritten', 'applied')),
    proposal_text TEXT,
    proposal_token_count INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_id, sample_id),
    FOREIGN KEY(job_id, sample_id) REFERENCES workflow_samples(job_id, sample_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_samples_status ON workflow_samples(job_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_events_job_cursor ON workflow_events(job_id, event_id);
CREATE INDEX IF NOT EXISTS idx_workflow_job_pins_pinned_at ON workflow_job_pins(pinned_at);
CREATE INDEX IF NOT EXISTS idx_workflow_stage_runs_job ON workflow_stage_runs(job_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_workflow_operations_job ON workflow_operations(job_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_operations_idempotency
    ON workflow_operations(job_id, operation_type, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_job ON workflow_artifacts(job_id, kind);
CREATE INDEX IF NOT EXISTS idx_workflow_dataset_locks_job ON workflow_dataset_locks(job_id, released_at);
CREATE INDEX IF NOT EXISTS idx_workflow_samples_lease ON workflow_samples(job_id, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_workflow_issues_job ON workflow_issues(job_id, severity, blocking);
CREATE INDEX IF NOT EXISTS idx_workflow_count_review_status ON workflow_count_review(job_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_token_review_status ON workflow_token_budget_review(job_id, status);
"""


def _backup_before_migration(db_path: Path) -> Path | None:
    """Create a consistent SQLite backup before changing a file database."""
    if str(db_path) == ":memory:":
        return None
    backup = db_path.with_name(db_path.name + ".pre-migration.bak")
    if backup.exists():
        backup.unlink()
    source = sqlite3.connect(db_path, timeout=30.0)
    target = sqlite3.connect(backup)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return backup


def _check_database(conn: sqlite3.Connection) -> None:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        raise RuntimeError(f"workflow database integrity_check failed: {integrity!r}")
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"workflow database foreign_key_check failed: {foreign_keys[:3]!r}")


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    """Hash the persisted schema shape without making it a migration checksum."""

    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY type, name"
    ).fetchall()
    payload = "\n".join(
        f"{str(row[0])}\0{str(row[1])}\0{str(row[2])}" for row in rows
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _has_scoped_operation_idempotency_index(conn: sqlite3.Connection) -> bool:
    """Return whether operation retries are scoped by job and operation type.

    V3 initially declared ``idempotency_key`` as a table-level UNIQUE column,
    which incorrectly made a key collide across independent jobs.  Looking at
    the index shape (rather than the SQL text) also handles SQLite's generated
    names and databases created by different SQLite versions.
    """

    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_operations'"
    ).fetchone()
    if table is None:
        # Older pre-v3 databases may not have the operation projection yet;
        # the idempotent SCHEMA_SQL below will create the current shape.
        return True
    scoped = False
    global_key = False
    for row in conn.execute("PRAGMA index_list(workflow_operations)").fetchall():
        # index_list columns are seq, name, unique, origin, partial.
        if len(row) < 3 or int(row[2]) != 1:
            continue
        index_name = str(row[1]).replace("'", "''")
        columns = [
            str(index_row[2])
            for index_row in conn.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
        ]
        if columns == ["job_id", "operation_type", "idempotency_key"]:
            scoped = True
        elif columns == ["idempotency_key"]:
            global_key = True
    return scoped and not global_key


def _rebuild_operation_table_for_scoped_idempotency(conn: sqlite3.Connection) -> None:
    """Replace the original v3 operation table without losing audit rows."""

    # The table has no child tables today, but disable FK enforcement for the
    # rename/drop sequence so this remains safe if a future schema adds one.
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE workflow_operations_scoped (
                operation_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO workflow_operations_scoped
                (operation_id, job_id, operation_type, status, idempotency_key,
                 payload_json, created_at, finished_at)
            SELECT operation_id, job_id, operation_type, status, idempotency_key,
                   payload_json, created_at, finished_at
              FROM workflow_operations
            """
        )
        conn.execute("DROP TABLE workflow_operations")
        conn.execute(
            "ALTER TABLE workflow_operations_scoped RENAME TO workflow_operations"
        )
        conn.execute(
            "CREATE UNIQUE INDEX idx_workflow_operations_idempotency "
            "ON workflow_operations(job_id, operation_type, idempotency_key)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def apply_migrations(db_path: Path) -> None:
    """Apply schema migrations without dropping rows referenced by child tables."""
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0)
    backup = None
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if not cursor.fetchone():
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum, applied_at)"
                " VALUES (?, ?, datetime('now'))",
                (SCHEMA_VERSION, MIGRATION_CHECKSUMS[SCHEMA_VERSION]),
            )
            conn.commit()
            _check_database(conn)
            conn.execute(
                "UPDATE schema_migrations SET schema_fingerprint = ? WHERE version = ?",
                (schema_fingerprint(conn), SCHEMA_VERSION),
            )
            conn.commit()
            return

        migration_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()
        }
        if "schema_fingerprint" not in migration_columns:
            conn.execute("ALTER TABLE schema_migrations ADD COLUMN schema_fingerprint TEXT")
            conn.commit()

        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        applied = int(row[0]) if row and row[0] is not None else 0
        if applied > SCHEMA_VERSION:
            raise RuntimeError(
                f"workflow database schema version {applied} is newer than supported"
                f" version {SCHEMA_VERSION}"
            )

        if applied < 2:
            backup = _backup_before_migration(Path(db_path))
            # v1 -> v2: add lease bookkeeping to existing sample rows. ALTER is
            # used rather than a rebuild so existing job history is preserved.
            # This connection has no row factory, so PRAGMA rows are plain
            # tuples: (cid, name, type, notnull, dflt_value, pk).
            existing = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(workflow_samples)").fetchall()
            }
            for column, ddl in (
                ("lease_owner", "ALTER TABLE workflow_samples ADD COLUMN lease_owner TEXT"),
                (
                    "lease_expires_at",
                    "ALTER TABLE workflow_samples ADD COLUMN lease_expires_at TEXT",
                ),
                (
                    "attempt_count",
                    "ALTER TABLE workflow_samples ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
                ),
            ):
                if column not in existing:
                    conn.execute(ddl)
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum, applied_at)"
                " VALUES (?, ?, datetime('now'))",
                (2, MIGRATION_CHECKSUMS[2]),
            )

        # A database stamped v2 by an interrupted/older build may have the
        # version row but miss one of the lease columns.  Repair that shape
        # before creating the v2 indexes below.
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_samples'"
        ).fetchone():
            existing = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(workflow_samples)").fetchall()
            }
            for column, ddl in (
                ("lease_owner", "ALTER TABLE workflow_samples ADD COLUMN lease_owner TEXT"),
                ("lease_expires_at", "ALTER TABLE workflow_samples ADD COLUMN lease_expires_at TEXT"),
                ("attempt_count", "ALTER TABLE workflow_samples ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    conn.execute(ddl)

        if applied < 3:
            backup = backup or _backup_before_migration(Path(db_path))
            # v2 -> v3: SQLite cannot alter a CHECK constraint in place.  The
            # old implementation dropped the parent with foreign_keys enabled,
            # which cascaded and deleted every sample/issue/review row.  Disable
            # FK enforcement for the atomic table swap; child table definitions
            # continue to reference the original table name after the rename.
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""
                    CREATE TABLE workflow_jobs_v3 (
                        job_id TEXT PRIMARY KEY,
                        config_version INTEGER NOT NULL CHECK(config_version IN (1, 2)),
                        config_json TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        profile TEXT NOT NULL CHECK(profile IN ('e621', 'danbooru')),
                        work_mode TEXT NOT NULL CHECK(work_mode IN ('in_place', 'full_copy')),
                        overwrite_mode TEXT NOT NULL CHECK(overwrite_mode IN ('incremental', 'rebuild')),
                        source_root_id TEXT NOT NULL,
                        output_root_id TEXT,
                        workspace_path TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'pending', 'queued', 'running', 'pausing', 'paused',
                            'waiting_count_review', 'waiting_token_review', 'committing',
                            'restoring', 'interrupted', 'rollback_required',
                            'completed', 'failed', 'cancelling', 'cancelled'
                        )),
                        current_module_id TEXT,
                        total_samples INTEGER NOT NULL DEFAULT 0,
                        processed_samples INTEGER NOT NULL DEFAULT 0,
                        succeeded_samples INTEGER NOT NULL DEFAULT 0,
                        failed_samples INTEGER NOT NULL DEFAULT 0,
                        skipped_samples INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        error TEXT
                    )
                """)
                
                conn.execute("""
                    INSERT INTO workflow_jobs_v3 (
                        job_id, config_version, config_json, config_hash, profile,
                        work_mode, overwrite_mode, source_root_id, output_root_id,
                        workspace_path, status, current_module_id, total_samples,
                        processed_samples, succeeded_samples, failed_samples,
                        skipped_samples, created_at, started_at, finished_at, error
                    )
                    SELECT
                        job_id, config_version, config_json, config_hash, profile,
                        work_mode, overwrite_mode, source_root_id, output_root_id,
                        workspace_path, status, current_module_id, total_samples,
                        processed_samples, succeeded_samples, failed_samples,
                        skipped_samples, created_at, started_at, finished_at, error
                    FROM workflow_jobs
                """)
                conn.execute("DROP TABLE workflow_jobs")
                conn.execute("ALTER TABLE workflow_jobs_v3 RENAME TO workflow_jobs")
                
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum, applied_at)"
                    " VALUES (?, ?, datetime('now'))",
                    (3, MIGRATION_CHECKSUMS[3]),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON")

        if applied < 4:
            backup = backup or _backup_before_migration(Path(db_path))
            existing = {
                str(column[1])
                for column in conn.execute("PRAGMA table_info(workflow_jobs)").fetchall()
            }
            for column in ("restored_at", "discarded_at"):
                if column not in existing:
                    conn.execute(f"ALTER TABLE workflow_jobs ADD COLUMN {column} TEXT")
            conn.execute(
                "INSERT INTO schema_migrations (version, checksum, applied_at)"
                " VALUES (?, ?, datetime('now'))",
                (4, MIGRATION_CHECKSUMS[4]),
            )
            conn.commit()

        # Keep the remaining DDL idempotent so a partially created database heals.
        # Existing v3 databases may still have the old table-level UNIQUE key.
        # Repair that shape in place (without bumping the public schema version)
        # while retaining every operation row and its raw caller key.
        if not _has_scoped_operation_idempotency_index(conn):
            backup = backup or _backup_before_migration(Path(db_path))
            _rebuild_operation_table_for_scoped_idempotency(conn)
        conn.executescript(SCHEMA_SQL)
        # Older databases may have been stamped at the current version before
        # the event table was introduced.  The idempotent DDL above creates it;
        # retain one explicit schema marker so operators can see the control
        # plane was upgraded without changing the version semantics.
        conn.commit()
        _check_database(conn)
        conn.execute(
            "UPDATE schema_migrations SET schema_fingerprint = ? WHERE version = ?",
            (schema_fingerprint(conn), SCHEMA_VERSION),
        )
        conn.commit()
    except Exception:
        # A migration must be recoverable.  Restore the pre-migration image
        # before surfacing the error to the caller.
        if backup is not None and backup.exists():
            try:
                conn.close()
            finally:
                shutil.copy2(backup, db_path)
        raise
    finally:
        # Windows keeps a file handle until the connection is closed, which would
        # break temporary-directory cleanup and workspace discard.
        conn.close()

    # The backup is intentionally retained as a recovery artifact.  It is
    # never used as the live database and can be removed by retention tooling.


__all__ = [
    "MIGRATION_CHECKSUMS",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "apply_migrations",
    "schema_fingerprint",
]

