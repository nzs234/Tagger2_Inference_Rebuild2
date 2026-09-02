"""Tag manager SQLite index: sessions, image index, tag rows, undo journal.

The store deliberately keeps denormalized convenience columns (``file_name``,
``tag_count``) so grid listing, sorting and tag filtering stay single-query.
Image ids are stable across refreshes: rows are upserted by relative path, so
an edited selection survives a rescan.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..workflow.contracts import utc_now

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    root_id TEXT NOT NULL,
    relative_path TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL CHECK (profile IN ('e621', 'danbooru')),
    recursive INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK (status IN ('indexing', 'ready', 'error')),
    error TEXT,
    image_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES dataset_sessions(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    image_format TEXT NOT NULL DEFAULT '',
    sidecar_kind TEXT NOT NULL DEFAULT 'none'
        CHECK (sidecar_kind IN ('none', 'tag_txt', 'tags_json', 'standard_json', 'raw_e621_json')),
    sidecar_path TEXT,
    mtime REAL NOT NULL DEFAULT 0,
    sidecar_mtime REAL,
    width INTEGER,
    height INTEGER,
    tag_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (session_id, relative_path)
);
CREATE INDEX IF NOT EXISTS idx_images_session_id ON dataset_images(session_id, id);
CREATE INDEX IF NOT EXISTS idx_images_session_name ON dataset_images(session_id, file_name);
CREATE INDEX IF NOT EXISTS idx_images_session_mtime ON dataset_images(session_id, mtime);

CREATE TABLE IF NOT EXISTS dataset_image_tags (
    image_id INTEGER NOT NULL REFERENCES dataset_images(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (image_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON dataset_image_tags(tag);

CREATE TABLE IF NOT EXISTS undo_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES dataset_sessions(id) ON DELETE CASCADE,
    op TEXT NOT NULL,
    spec TEXT NOT NULL,
    changes TEXT NOT NULL,
    undone INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_session ON undo_journal(session_id, id);
"""


class TagManagerStoreError(RuntimeError):
    """Raised for internal index invariant violations."""


def default_tag_manager_database_path() -> Path:
    """Return the module's isolated database path under the data directory."""

    from ..config import get_settings

    settings = get_settings()
    data_dir = settings.data_dir
    if data_dir is None:
        raise RuntimeError("application data_dir is not configured")
    return data_dir / "tag_manager" / "tag_manager.sqlite3"


class TagManagerStore:
    """SQLite index for tag manager sessions."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        self._write_lock = threading.RLock()
        if str(db_path) == ":memory:":
            self._memory_conn = sqlite3.connect(
                ":memory:", timeout=30.0, check_same_thread=False
            )
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.execute("PRAGMA foreign_keys=ON")
            self._memory_conn.execute("PRAGMA busy_timeout=30000")
            self._memory_conn.executescript(SCHEMA_SQL)
            self._memory_conn.execute(
                "INSERT INTO schema_migrations (version, checksum, applied_at)"
                " VALUES (?, ?, ?)",
                (SCHEMA_VERSION, "schema-v1", utc_now()),
            )
            self._memory_conn.commit()
        else:
            self._init_file_db()

    def _init_file_db(self) -> None:
        with self.connection() as conn:
            conn.executescript(SCHEMA_SQL)
            row = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_migrations (version, checksum, applied_at)"
                    " VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, "schema-v1", utc_now()),
                )
            elif int(row["version"]) > SCHEMA_VERSION:
                raise TagManagerStoreError(
                    f"tag manager database version {row['version']} is newer than supported"
                )

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager yielding a transaction-wrapped connection."""

        if self._memory_conn is not None:
            with self._write_lock:
                try:
                    yield self._memory_conn
                    self._memory_conn.commit()
                except Exception:
                    self._memory_conn.rollback()
                    raise
        else:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                with self._write_lock:
                    try:
                        yield conn
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
            finally:
                conn.close()

    # -- sessions ----------------------------------------------------------

    def create_session(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO dataset_sessions"
                " (id, name, root_id, relative_path, profile, recursive, status,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'indexing', ?, ?)",
                (
                    str(entry["id"]),
                    entry.get("name"),
                    str(entry["root_id"]),
                    str(entry.get("relative_path", "")),
                    str(entry["profile"]),
                    1 if entry.get("recursive", True) else 0,
                    utc_now(),
                    utc_now(),
                ),
            )
        return self.get_session(str(entry["id"])) or {}

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM dataset_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_dict(row) if row is not None else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM dataset_sessions ORDER BY created_at DESC, id"
            ).fetchall()
        return [_session_dict(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        error: str | None = None,
        image_count: int | None = None,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if error is not None or status == "ready":
            fields.append("error = ?")
            values.append(error)
        if image_count is not None:
            fields.append("image_count = ?")
            values.append(image_count)
        values.append(session_id)
        with self.connection() as conn:
            conn.execute(
                f"UPDATE dataset_sessions SET {', '.join(fields)} WHERE id = ?", values
            )

    def delete_session(self, session_id: str) -> bool:
        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM dataset_sessions WHERE id = ?", (session_id,)
            )
            return cursor.rowcount > 0

    # -- image index -------------------------------------------------------

    def upsert_images(
        self, session_id: str, images: list[Mapping[str, Any]]
    ) -> list[int]:
        """Upsert scanned rows keyed by relative path; returns image ids.

        Rows for paths that no longer exist must be pruned by the caller via
        :meth:`prune_images_missing`.
        """

        ids: list[int] = []
        with self.connection() as conn:
            for image in images:
                relative = str(image["relative_path"])
                conn.execute(
                    "INSERT INTO dataset_images"
                    " (session_id, relative_path, file_name, image_format,"
                    "  sidecar_kind, sidecar_path, mtime, sidecar_mtime,"
                    "  width, height, tag_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(session_id, relative_path) DO UPDATE SET"
                    "  file_name = excluded.file_name,"
                    "  image_format = excluded.image_format,"
                    "  sidecar_kind = excluded.sidecar_kind,"
                    "  sidecar_path = excluded.sidecar_path,"
                    "  mtime = excluded.mtime,"
                    "  sidecar_mtime = excluded.sidecar_mtime,"
                    "  width = excluded.width,"
                    "  height = excluded.height,"
                    "  tag_count = excluded.tag_count",
                    (
                        session_id,
                        relative,
                        str(image["file_name"]),
                        str(image.get("image_format", "")),
                        str(image.get("sidecar_kind", "none")),
                        image.get("sidecar_path"),
                        float(image.get("mtime", 0.0)),
                        image.get("sidecar_mtime"),
                        image.get("width"),
                        image.get("height"),
                        int(image.get("tag_count", 0)),
                    ),
                )
                row = conn.execute(
                    "SELECT id FROM dataset_images WHERE session_id = ? AND relative_path = ?",
                    (session_id, relative),
                ).fetchone()
                if row is None:
                    raise TagManagerStoreError("image row missing after upsert")
                ids.append(int(row["id"]))
        return ids

    def prune_images_missing(self, session_id: str, keep_paths: set[str]) -> int:
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT id, relative_path FROM dataset_images WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            removed = 0
            for row in existing:
                if str(row["relative_path"]) not in keep_paths:
                    conn.execute(
                        "DELETE FROM dataset_images WHERE id = ?", (row["id"],)
                    )
                    removed += 1
            return removed

    def set_image_tags(
        self, image_id: int, tags: list[tuple[str, str]], *, sidecar_kind: str, sidecar_mtime: float | None
    ) -> None:
        """Replace the tag rows of one image and refresh its denormalized columns."""

        with self.connection() as conn:
            conn.execute("DELETE FROM dataset_image_tags WHERE image_id = ?", (image_id,))
            for position, (tag, category) in enumerate(tags):
                conn.execute(
                    "INSERT OR IGNORE INTO dataset_image_tags (image_id, tag, category, position)"
                    " VALUES (?, ?, ?, ?)",
                    (image_id, tag, category, position),
                )
            conn.execute(
                "UPDATE dataset_images"
                " SET tag_count = (SELECT COUNT(*) FROM dataset_image_tags WHERE image_id = ?),"
                "     sidecar_kind = ?,"
                "     sidecar_mtime = ?"
                " WHERE id = ?",
                (image_id, sidecar_kind, sidecar_mtime, image_id),
            )

    def get_image(self, session_id: str, image_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM dataset_images WHERE session_id = ? AND id = ?",
                (session_id, image_id),
            ).fetchone()
        return _image_dict(row) if row is not None else None

    def list_images(
        self,
        session_id: str,
        *,
        include_tags: Sequence[str] = (),
        exclude_tags: Sequence[str] = (),
        include_mode: str = "all",
        kind: str = "any",
        sidecar: str = "any",
        sort: str = "name",
        offset: int = 0,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = ["d.session_id = ?"]
        values: list[Any] = [session_id]

        if include_tags:
            if include_mode == "any":
                placeholders = ", ".join("?" for _ in include_tags)
                clauses.append(
                    f"EXISTS (SELECT 1 FROM dataset_image_tags t"
                    f" WHERE t.image_id = d.id AND t.tag IN ({placeholders}))"
                )
                values.extend(include_tags)
            else:
                for tag in include_tags:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM dataset_image_tags t"
                        " WHERE t.image_id = d.id AND t.tag = ?)"
                    )
                    values.append(tag)
        for tag in exclude_tags:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM dataset_image_tags t"
                " WHERE t.image_id = d.id AND t.tag = ?)"
            )
            values.append(tag)
        if kind != "any":
            clauses.append("d.sidecar_kind = ?")
            values.append(kind)
        if sidecar == "present":
            clauses.append("d.sidecar_kind != 'none'")
        elif sidecar == "missing":
            clauses.append("d.sidecar_kind = 'none'")

        order = {
            "name": "d.file_name COLLATE NOCASE ASC, d.id ASC",
            "mtime": "d.mtime DESC, d.id ASC",
            "tags": "d.tag_count DESC, d.file_name COLLATE NOCASE ASC, d.id ASC",
        }.get(sort, "d.file_name COLLATE NOCASE ASC, d.id ASC")

        where = " AND ".join(clauses)
        with self.connection() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM dataset_images d WHERE {where}", values
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT d.* FROM dataset_images d WHERE {where}"
                f" ORDER BY {order} LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
        return [_image_dict(row) for row in rows], total

    def image_tags(self, image_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not image_ids:
            return {}
        placeholders = ", ".join("?" for _ in image_ids)
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT image_id, tag, category FROM dataset_image_tags"
                f" WHERE image_id IN ({placeholders}) ORDER BY image_id, position",
                image_ids,
            ).fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            result.setdefault(int(row["image_id"]), []).append(
                {"tag": str(row["tag"]), "category": str(row["category"])}
            )
        return result

    def tag_stats(
        self,
        session_id: str,
        *,
        limit: int = 200,
        min_count: int = 1,
    ) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT t.tag AS tag, MIN(t.category) AS category, COUNT(*) AS count"
                " FROM dataset_image_tags t"
                " JOIN dataset_images d ON t.image_id = d.id"
                " WHERE d.session_id = ?"
                " GROUP BY t.tag COLLATE NOCASE"
                " HAVING COUNT(*) >= ?"
                " ORDER BY count DESC, t.tag COLLATE NOCASE ASC"
                " LIMIT ?",
                (session_id, min_count, limit),
            ).fetchall()
        return [
            {"tag": str(row["tag"]), "category": str(row["category"]), "count": int(row["count"])}
            for row in rows
        ]

    # -- undo journal ------------------------------------------------------

    def append_journal(
        self, session_id: str, *, op: str, spec: Mapping[str, Any], changes: Sequence[Mapping[str, Any]]
    ) -> int:
        with self.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO undo_journal (session_id, op, spec, changes, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    op,
                    json.dumps(dict(spec), ensure_ascii=False),
                    json.dumps(changes, ensure_ascii=False),
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def journal_entries(self, session_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM undo_journal WHERE session_id = ? ORDER BY id DESC"
        )
        params: list[Any] = [session_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_journal_dict(row) for row in rows]

    def latest_journal_entry(self, session_id: str, *, undone: bool) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM undo_journal WHERE session_id = ? AND undone = ?"
                " ORDER BY id DESC LIMIT 1",
                (session_id, 1 if undone else 0),
            ).fetchone()
        return _journal_dict(row) if row is not None else None

    def set_journal_undone(self, entry_id: int, undone: bool) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE undo_journal SET undone = ? WHERE id = ?",
                (1 if undone else 0, entry_id),
            )

    def trim_journal(self, session_id: str, keep: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM undo_journal WHERE session_id = ? AND id NOT IN ("
                " SELECT id FROM undo_journal WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, keep),
            )


def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "root_id": str(row["root_id"]),
        "relative_path": str(row["relative_path"]),
        "profile": str(row["profile"]),
        "recursive": bool(row["recursive"]),
        "status": str(row["status"]),
        "error": row["error"],
        "image_count": int(row["image_count"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _image_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "relative_path": str(row["relative_path"]),
        "file_name": str(row["file_name"]),
        "image_format": str(row["image_format"]),
        "sidecar_kind": str(row["sidecar_kind"]),
        "sidecar_path": row["sidecar_path"],
        "mtime": float(row["mtime"]),
        "sidecar_mtime": row["sidecar_mtime"],
        "width": row["width"],
        "height": row["height"],
        "tag_count": int(row["tag_count"]),
    }


def _journal_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": str(row["session_id"]),
        "op": str(row["op"]),
        "spec": json.loads(str(row["spec"])),
        "changes": json.loads(str(row["changes"])),
        "undone": bool(row["undone"]),
        "created_at": str(row["created_at"]),
    }


__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "TagManagerStore",
    "TagManagerStoreError",
    "default_tag_manager_database_path",
]
