"""Workflow database schema and migrations."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    job_id TEXT PRIMARY KEY,
    config_version INTEGER NOT NULL CHECK(config_version = 1),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    profile TEXT NOT NULL CHECK(profile IN ('e621', 'danbooru')),
    work_mode TEXT NOT NULL CHECK(work_mode IN ('in_place', 'full_copy')),
    overwrite_mode TEXT NOT NULL CHECK(overwrite_mode IN ('incremental', 'rebuild')),
    source_root_id TEXT NOT NULL,
    output_root_id TEXT,
    workspace_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'paused', 
        'waiting_count_review', 'waiting_token_review',
        'completed', 'failed', 'cancelled'
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
CREATE INDEX IF NOT EXISTS idx_workflow_samples_lease ON workflow_samples(job_id, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_workflow_issues_job ON workflow_issues(job_id, severity, blocking);
CREATE INDEX IF NOT EXISTS idx_workflow_count_review_status ON workflow_count_review(job_id, status);
CREATE INDEX IF NOT EXISTS idx_workflow_token_review_status ON workflow_token_budget_review(job_id, status);
"""


def apply_migrations(db_path: Path) -> None:
    """Apply schema migrations to workflows database."""
    # Skip directory creation for in-memory databases
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    
    checksum = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
    conn = sqlite3.connect(db_path, timeout=30.0)
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
                (SCHEMA_VERSION, checksum),
            )
            conn.commit()
            return

        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        applied = int(row[0]) if row and row[0] is not None else 0
        if applied > SCHEMA_VERSION:
            raise RuntimeError(
                f"workflow database schema version {applied} is newer than supported"
                f" version {SCHEMA_VERSION}"
            )

        if applied < 2:
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
                (2, checksum),
            )

        if applied < 3:
            # v2 -> v3: The CHECK constraint on workflow_jobs.status cannot be
            # altered in place on SQLite, so we rebuild the table to include
            # waiting_count_review and waiting_token_review states.
            # Preserve all existing data during the rebuild.
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Create new table with updated CHECK constraint
                conn.execute("""
                    CREATE TABLE workflow_jobs_v3 (
                        job_id TEXT PRIMARY KEY,
                        config_version INTEGER NOT NULL CHECK(config_version = 1),
                        config_json TEXT NOT NULL,
                        config_hash TEXT NOT NULL,
                        profile TEXT NOT NULL CHECK(profile IN ('e621', 'danbooru')),
                        work_mode TEXT NOT NULL CHECK(work_mode IN ('in_place', 'full_copy')),
                        overwrite_mode TEXT NOT NULL CHECK(overwrite_mode IN ('incremental', 'rebuild')),
                        source_root_id TEXT NOT NULL,
                        output_root_id TEXT,
                        workspace_path TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'pending', 'running', 'paused', 
                            'waiting_count_review', 'waiting_token_review',
                            'completed', 'failed', 'cancelled'
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
                
                # Copy all existing data
                conn.execute("""
                    INSERT INTO workflow_jobs_v3 SELECT * FROM workflow_jobs
                """)
                
                # Drop old table and rename new one
                conn.execute("DROP TABLE workflow_jobs")
                conn.execute("ALTER TABLE workflow_jobs_v3 RENAME TO workflow_jobs")
                
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum, applied_at)"
                    " VALUES (?, ?, datetime('now'))",
                    (3, checksum),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        # Keep the remaining DDL idempotent so a partially created database heals.
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        # Windows keeps a file handle until the connection is closed, which would
        # break temporary-directory cleanup and workspace discard.
        conn.close()


__all__ = ["SCHEMA_VERSION", "SCHEMA_SQL", "apply_migrations"]

