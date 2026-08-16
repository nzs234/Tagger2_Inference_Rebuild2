"""Regression tests for workflow SQLite migrations."""

import sqlite3
from pathlib import Path


def _v2_database(path: Path) -> None:
    """Create a representative v2 image with every child table populated."""

    from tagger2.workflow.db_schema import SCHEMA_SQL

    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "UPDATE schema_migrations SET version = 2, checksum = ?",
        ("workflow-schema-v2-leases",),
    )
    connection.execute(
        """
        INSERT INTO workflow_jobs (
            job_id, config_version, config_json, config_hash, profile,
            work_mode, overwrite_mode, source_root_id, output_root_id,
            workspace_path, status, total_samples, created_at
        ) VALUES ('job', 1, '{}', 'hash', 'e621', 'full_copy', 'incremental',
                  'input', 'output', 'workspace', 'pending', 1, 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_samples (
            job_id, sample_id, relative_image_path, image_format, status,
            created_at, updated_at
        ) VALUES ('job', 1, 'one.png', 'png', 'pending', 'now', 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_issues (
            issue_id, job_id, sample_id, module_id, code, severity, blocking,
            message, created_at
        ) VALUES ('issue', 'job', 1, 'caption', 'warning', 'warning', 0,
                  'preserve me', 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_count_review (
            job_id, sample_id, count_value, status, decision_json,
            created_at, updated_at
        ) VALUES ('job', 1, 'solo', 'pending', '{}', 'now', 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_token_budget_review (
            job_id, sample_id, nl_text, token_count, token_limit, status,
            created_at, updated_at
        ) VALUES ('job', 1, 'caption', 1, 10, 'overflow', 'now', 'now')
        """
    )
    connection.commit()
    connection.close()


def test_v2_to_v3_preserves_parent_and_all_child_rows(tmp_path: Path):
    """Rebuilding workflow_jobs must not cascade-delete dependent records."""

    from tagger2.workflow.db_schema import apply_migrations

    db_path = tmp_path / "workflow.sqlite3"
    _v2_database(db_path)

    apply_migrations(db_path)

    connection = sqlite3.connect(db_path)
    try:
        for table in (
            "workflow_jobs",
            "workflow_samples",
            "workflow_issues",
            "workflow_count_review",
            "workflow_token_budget_review",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        versions = connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [
            (2, "workflow-schema-v2-leases"),
            (3, "workflow-schema-v3-job-states"),
            (4, "workflow-schema-v4-restore-discard-state"),
        ]
        job_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(workflow_jobs)")
        }
        assert {"restored_at", "discarded_at"} <= job_columns
    finally:
        connection.close()

    # A recoverable copy is retained for operator rollback/diagnostics.
    assert db_path.with_name("workflow.sqlite3.pre-migration.bak").exists()


def test_v3_repairs_legacy_global_operation_idempotency(tmp_path: Path):
    """Existing v3 operation rows keep raw keys while becoming job-scoped."""
    from tagger2.workflow.db_schema import SCHEMA_SQL, apply_migrations

    db_path = tmp_path / "workflow.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA_SQL)
    # Emulate the first v3 schema, where the column itself was UNIQUE.
    connection.execute("DROP INDEX idx_workflow_operations_idempotency")
    connection.execute("DROP TABLE workflow_operations")
    connection.execute(
        """
        CREATE TABLE workflow_operations (
            operation_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES workflow_jobs(job_id) ON DELETE CASCADE,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_jobs (
            job_id, config_version, config_json, config_hash, profile,
            work_mode, overwrite_mode, source_root_id, output_root_id,
            workspace_path, status, created_at
        ) VALUES ('job-a', 1, '{}', 'hash', 'e621', 'full_copy', 'incremental',
                  'input', 'output', 'workspace-a', 'pending', 'now')
        """
    )
    connection.execute(
        """
        INSERT INTO workflow_operations
            (operation_id, job_id, operation_type, status, idempotency_key,
             payload_json, created_at)
        VALUES ('op-a', 'job-a', 'restore', 'completed', 'retry', '{}', 'now')
        """
    )
    connection.commit()
    connection.close()

    apply_migrations(db_path)

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT idempotency_key FROM workflow_operations WHERE operation_id = 'op-a'"
        ).fetchone()[0] == "retry"
        indexes = connection.execute(
            "PRAGMA index_list(workflow_operations)"
        ).fetchall()
        unique_columns = []
        for row in indexes:
            if int(row[2]) == 1:
                unique_columns.append(
                    [
                        str(info[2])
                        for info in connection.execute(
                            f"PRAGMA index_info('{str(row[1]).replace(chr(39), chr(39) * 2)}')"
                        ).fetchall()
                    ]
                )
        assert ["job_id", "operation_type", "idempotency_key"] in unique_columns
        assert ["idempotency_key"] not in unique_columns
    finally:
        connection.close()
