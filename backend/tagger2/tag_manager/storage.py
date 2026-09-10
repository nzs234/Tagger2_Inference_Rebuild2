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

from ..tag_text import canonical_tag_key
from ..workflow.contracts import utc_now

SCHEMA_VERSION = 1

# Host parameters per chunked IN () query: a 2000-image batch must stay
# inside SQLite's variable budget regardless of the build's compile limits.
_SQL_PARAMETER_CHUNK = 500

# Sentinel for the optional ``sidecar_path`` write on :meth:`set_image_tags`.
# ``None`` is a meaningful value (clear the column) while *not passing* the
# argument must leave the stored path alone, so the two cases need distinct
# markers rather than ``None`` doubling as "unset".
_UNSET: Any = object()

# SQLite side of ``canonical_tag_key``: the same lowercase underscore form
# so a filter typed with spaces matches a sidecar written with underscores.
# CASEFOLD is a scalar function registered on every connection (see
# ``_register_sql_functions``): SQLite's built-in LOWER() folds ASCII only,
# while the Python side of the rule (``canonical_tag_key``) casefolds full
# Unicode, so filter and stats queries must fold through the same function
# to agree with the normalized values the client sends.
_TAG_KEY_SQL = "REPLACE(CASEFOLD(t.tag), ' ', '_')"


def normalize_tag_key(tag: str) -> str:
    """Return the comparison key used by tag filters.

    Delegates to the shared :func:`tagger2.tag_text.canonical_tag_key` rule;
    the alias is kept so callers and tests keep their long-standing name.
    """

    return canonical_tag_key(tag)


def _sql_casefold(value: Any) -> Any:
    """``str.casefold`` exposed to SQLite (built-in LOWER is ASCII-only)."""

    return value.casefold() if isinstance(value, str) else value


def _register_sql_functions(conn: sqlite3.Connection) -> None:
    """Register the Python-backed scalar functions every connection needs.

    ``_TAG_KEY_SQL`` folds tag spellings through CASEFOLD so the SQL side of
    the filter/stats normalization matches ``normalize_tag_key`` for
    non-ASCII tags too.  File-backed connections are opened per operation,
    so the registration rides along with the connection setup.
    """

    conn.create_function("CASEFOLD", 1, _sql_casefold, deterministic=True)


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
            _register_sql_functions(self._memory_conn)
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
            # WAL mode: every store operation (reads included) still takes the
            # shared write lock, so reads and writes inside this process stay
            # serialized.  WAL instead buys crash safety over a rollback
            # journal, readers in other processes that keep working while this
            # one writes, and cheap transactions — the scan's per-chunk commits
            # yield the lock between chunks instead of one commit per image.
            conn.execute("PRAGMA journal_mode=WAL")
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
            _register_sql_functions(conn)
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
                ids.append(_upsert_image_row(conn, session_id, image))
        return ids

    def upsert_rows(self, session_id: str, rows: Sequence[Mapping[str, Any]]) -> int:
        """Upsert scan rows together with their tags in one transaction.

        Each row carries the scan fields plus ``_tags`` — the categorized tag
        pairs produced by the indexer.  One transaction for the whole chunk
        keeps a large scan from paying a connection and a commit per image.
        """

        with self.connection() as conn:
            for row in rows:
                image = {key: value for key, value in row.items() if key != "_tags"}
                image_id = _upsert_image_row(conn, session_id, image)
                _replace_image_tags(
                    conn,
                    image_id,
                    list(row["_tags"]),
                    sidecar_kind=str(image.get("sidecar_kind", "none")),
                    sidecar_mtime=image.get("sidecar_mtime"),
                )
        return len(rows)

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

    def scan_state(self, session_id: str) -> dict[str, tuple[float, float | None, str]]:
        """Snapshot the indexed rows as the baseline for an incremental rescan.

        Maps relative_path -> (image mtime, sidecar mtime, sidecar kind).  The
        scan reuses rows whose image and sidecar files still carry exactly
        these ``st_mtime`` stamps and the same kind, so unchanged files are not
        re-parsed and keep their image ids and tag rows.
        """

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT relative_path, mtime, sidecar_mtime, sidecar_kind"
                " FROM dataset_images WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        return {
            str(row["relative_path"]): (
                float(row["mtime"]),
                None if row["sidecar_mtime"] is None else float(row["sidecar_mtime"]),
                str(row["sidecar_kind"]),
            )
            for row in rows
        }

    def set_image_tags(
        self,
        image_id: int,
        tags: list[tuple[str, str]],
        *,
        sidecar_kind: str,
        sidecar_mtime: float | None,
        sidecar_path: str | None = _UNSET,
    ) -> None:
        """Replace the tag rows of one image and refresh its denormalized columns.

        ``sidecar_path`` is optional: omitting it keeps the path recorded at
        scan/save time, while passing an explicit ``None`` clears the column
        (undo of a "from nothing" save must not leave the old extension behind
        and route a later different-format save into it).
        """

        with self.connection() as conn:
            _replace_image_tags(
                conn,
                image_id,
                tags,
                sidecar_kind=sidecar_kind,
                sidecar_mtime=sidecar_mtime,
                sidecar_path=sidecar_path,
            )

    def get_image(self, session_id: str, image_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM dataset_images WHERE session_id = ? AND id = ?",
                (session_id, image_id),
            ).fetchone()
        return _image_dict(row) if row is not None else None

    def get_images(
        self, session_id: str, image_ids: Sequence[int]
    ) -> dict[int, dict[str, Any]]:
        """Fetch several indexed images in one chunked query, keyed by id.

        Batch resolution must not pay a connection per id, so the ids are
        fetched in ``_SQL_PARAMETER_CHUNK``-sized IN () queries (a 2000-image
        batch would otherwise exceed the host-parameter budget one id at a
        time).  Missing ids are simply absent from the result.
        """

        result: dict[int, dict[str, Any]] = {}
        unique = list(dict.fromkeys(int(image_id) for image_id in image_ids))
        for start in range(0, len(unique), _SQL_PARAMETER_CHUNK):
            chunk = unique[start : start + _SQL_PARAMETER_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            with self.connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM dataset_images WHERE session_id = ?"
                    f" AND id IN ({placeholders})",
                    [session_id, *chunk],
                ).fetchall()
            for row in rows:
                image = _image_dict(row)
                result[int(image["id"])] = image
        return result

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

        # Sidecars spell the same tag with underscores or spaces depending on
        # the writer, so filters match on a normalized key rather than the
        # stored spelling.
        if include_tags:
            keys = [normalize_tag_key(tag) for tag in include_tags]
            if include_mode == "any":
                placeholders = ", ".join("?" for _ in keys)
                clauses.append(
                    f"EXISTS (SELECT 1 FROM dataset_image_tags t"
                    f" WHERE t.image_id = d.id AND {_TAG_KEY_SQL} IN ({placeholders}))"
                )
                values.extend(keys)
            else:
                for key in keys:
                    clauses.append(
                        "EXISTS (SELECT 1 FROM dataset_image_tags t"
                        f" WHERE t.image_id = d.id AND {_TAG_KEY_SQL} = ?)"
                    )
                    values.append(key)
        for tag in exclude_tags:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM dataset_image_tags t"
                f" WHERE t.image_id = d.id AND {_TAG_KEY_SQL} = ?)"
            )
            values.append(normalize_tag_key(tag))
        if kind != "any":
            clauses.append("d.sidecar_kind = ?")
            values.append(kind)
        if sidecar == "present":
            clauses.append("d.sidecar_kind != 'none'")
        elif sidecar == "missing":
            clauses.append("d.sidecar_kind = 'none'")

        # ``mtime``/``tags`` keep their long-standing descending semantics; the
        # ascending variants are separate values so adding them cannot reorder
        # an existing client's grid.
        order = {
            "name": "d.file_name COLLATE NOCASE ASC, d.id ASC",
            "mtime": "d.mtime DESC, d.id ASC",
            "mtime_asc": "d.mtime ASC, d.id ASC",
            "tags": "d.tag_count DESC, d.file_name COLLATE NOCASE ASC, d.id ASC",
            "tag_count_asc": "d.tag_count ASC, d.file_name COLLATE NOCASE ASC, d.id ASC",
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
        # Group on the same normalized key the tag filters match, so a tag
        # stored with spaces and one stored with underscores does not split
        # into two rows; MIN(t.tag) picks a representative spelling.  The
        # count is the number of images carrying the tag, not the number of
        # rows: one image holding both spellings must not count twice.
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT MIN(t.tag COLLATE NOCASE) AS tag, MIN(t.category) AS category,"
                " COUNT(DISTINCT t.image_id) AS count"
                " FROM dataset_image_tags t"
                " JOIN dataset_images d ON t.image_id = d.id"
                " WHERE d.session_id = ?"
                f" GROUP BY {_TAG_KEY_SQL}"
                " HAVING COUNT(DISTINCT t.image_id) >= ?"
                " ORDER BY count DESC, tag COLLATE NOCASE ASC"
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

    def next_redo_entry(self, session_id: str) -> dict[str, Any] | None:
        """Return the oldest still-undone entry: the next redo step.

        Undo walks the live history newest-first (``undone=0 ORDER BY id DESC``),
        so its mirror image is the *earliest* undone entry (``ORDER BY id ASC``):
        replaying the largest undone id first would apply the newest undone
        edit before the one that preceded it.
        """

        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM undo_journal WHERE session_id = ? AND undone = 1"
                " ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()
        return _journal_dict(row) if row is not None else None

    def has_journal_entry(self, session_id: str, *, undone: bool) -> bool:
        """Existence probe backing the session's ``can_undo``/``can_redo`` flags."""

        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM undo_journal WHERE session_id = ? AND undone = ? LIMIT 1",
                (session_id, 1 if undone else 0),
            ).fetchone()
        return row is not None

    def set_journal_undone(self, entry_id: int, undone: bool) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE undo_journal SET undone = ? WHERE id = ?",
                (1 if undone else 0, entry_id),
            )

    def discard_redo_stack(self, session_id: str) -> int:
        """Drop the undone entries of one session.

        A fresh edit or batch makes the previously undone history unreachable:
        replaying it would collide with the new state (and fail the text
        equality guard), so the entries are removed instead of lingering as a
        redo button that can only 409.
        """

        with self.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM undo_journal WHERE session_id = ? AND undone = 1",
                (session_id,),
            )
            return int(cursor.rowcount or 0)

    def trim_journal(self, session_id: str, keep: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM undo_journal WHERE session_id = ? AND id NOT IN ("
                " SELECT id FROM undo_journal WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, keep),
            )


def _upsert_image_row(
    conn: sqlite3.Connection, session_id: str, image: Mapping[str, Any]
) -> int:
    """Upsert one scanned row; returns the stable image id."""

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
    return int(row["id"])


def _replace_image_tags(
    conn: sqlite3.Connection,
    image_id: int,
    tags: Sequence[tuple[str, str]],
    *,
    sidecar_kind: str,
    sidecar_mtime: float | None,
    sidecar_path: str | None = _UNSET,
) -> None:
    conn.execute("DELETE FROM dataset_image_tags WHERE image_id = ?", (image_id,))
    for position, (tag, category) in enumerate(tags):
        conn.execute(
            "INSERT OR IGNORE INTO dataset_image_tags (image_id, tag, category, position)"
            " VALUES (?, ?, ?, ?)",
            (image_id, tag, category, position),
        )
    # ``sidecar_path`` is only rewritten when a caller passes it explicitly;
    # the default keeps whatever the scan/save recorded.  Passing ``None``
    # clears the column (the unlink branch of undo/redo).
    if sidecar_path is _UNSET:
        conn.execute(
            "UPDATE dataset_images"
            " SET tag_count = (SELECT COUNT(*) FROM dataset_image_tags WHERE image_id = ?),"
            "     sidecar_kind = ?,"
            "     sidecar_mtime = ?"
            " WHERE id = ?",
            (image_id, sidecar_kind, sidecar_mtime, image_id),
        )
    else:
        conn.execute(
            "UPDATE dataset_images"
            " SET tag_count = (SELECT COUNT(*) FROM dataset_image_tags WHERE image_id = ?),"
            "     sidecar_kind = ?,"
            "     sidecar_mtime = ?,"
            "     sidecar_path = ?"
            " WHERE id = ?",
            (image_id, sidecar_kind, sidecar_mtime, sidecar_path, image_id),
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
    "normalize_tag_key",
]
