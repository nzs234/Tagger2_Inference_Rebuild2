"""SQLite storage layer for the tag wiki mirror, embeddings, and summaries.

This module provides the local persistence for the e621 tag wiki mirror,
storing pages, parsed chunks, content hashes, embeddings (as float32 little-endian
vectors), wiki-link relationships, and generated Chinese summaries.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ..workflow.contracts import utc_now

SCHEMA_VERSION = 1

_URL_PATTERN = re.compile(r"https?://\S+")
_THUMB_PATTERN = re.compile(r"\bthumb\s*#\d+\b")
_WORD_PATTERN = re.compile(r"[A-Za-z0-9]{2,}")


def is_link_soup(text: str, *, max_urls: int = 2, min_words: int = 3) -> bool:
    """Whether a chunk carries nothing but external links and placeholders.

    Wiki dumps are full of ``"Site":https://...`` link lists, bare page URLs
    and e621 ``thumb #id`` reference lines. e5 embeds that soup into vectors
    that sit closer to every query than real prose does, so such chunks are
    excluded from the index. Text without any links or placeholders is never
    soup; when links are present, the residual text left after stripping
    them must still read like prose (``min_words`` word tokens) for the
    chunk to be kept, and anything with more than ``max_urls`` URLs is soup
    outright.
    """

    urls = _URL_PATTERN.findall(text)
    thumbs = _THUMB_PATTERN.findall(text)
    if len(urls) > max_urls:
        return True
    if not urls and not thumbs:
        return False
    residual = _THUMB_PATTERN.sub(" ", _URL_PATTERN.sub(" ", text))
    return len(_WORD_PATTERN.findall(residual)) < min_words


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    title TEXT PRIMARY KEY,
    display_title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    wiki_id INTEGER,
    updated_at TEXT,
    url TEXT,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_title TEXT NOT NULL REFERENCES pages(title) ON DELETE CASCADE,
    heading TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_page_title ON chunks(page_title);
CREATE INDEX IF NOT EXISTS idx_chunks_content_hash ON chunks(content_hash);

CREATE TABLE IF NOT EXISTS page_links (
    page_title TEXT NOT NULL,
    link_title TEXT NOT NULL,
    PRIMARY KEY(page_title, link_title)
);
CREATE INDEX IF NOT EXISTS idx_page_links_link_title ON page_links(link_title);

CREATE TABLE IF NOT EXISTS summaries (
    page_title TEXT PRIMARY KEY REFERENCES pages(title) ON DELETE CASCADE,
    meaning TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '',
    pairing TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

CHUNKS_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    body,
    heading,
    page_title UNINDEXED,
    content='chunks',
    content_rowid='id'
);
"""

# Standard triggers for FTS5 content table synchronization. Kept as a tuple of
# individual statements: `Connection.executescript()` issues an implicit COMMIT
# which would destroy the surrounding SAVEPOINT used by the FTS5 probe below.
CHUNKS_FTS_TRIGGER_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO chunks_fts(rowid, body, heading, page_title)
        VALUES (new.id, new.body, new.heading, new.page_title);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, body, heading, page_title)
        VALUES('delete', old.id, old.body, old.heading, old.page_title);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
        INSERT INTO chunks_fts(chunks_fts, rowid, body, heading, page_title)
        VALUES('delete', old.id, old.body, old.heading, old.page_title);
        INSERT INTO chunks_fts(rowid, body, heading, page_title)
        VALUES (new.id, new.body, new.heading, new.page_title);
    END;
    """,
)


class WikiStoreError(RuntimeError):
    """Raised for internal wiki store errors."""


def default_tag_wiki_database_path() -> Path:
    """Return the module's isolated database path under the data directory."""

    from ..config import get_settings

    settings = get_settings()
    data_dir = settings.data_dir
    if data_dir is None:
        raise RuntimeError("application data_dir is not configured")
    return data_dir / "tag_wiki" / "tag_wiki.sqlite3"


def normalize_title(title: str) -> str:
    """Normalize a wiki page title into the primary lookup key."""

    return "_".join(title.strip().casefold().split())


def _content_hash(text: str) -> str:
    """Compute sha256 of normalized text."""

    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class WikiStore:
    """SQLite persistence for the wiki mirror."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._is_memory = str(db_path) == ":memory:"
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._fts_enabled: bool | None = None
        self._embedding_matrix_cache: tuple[list[int], np.ndarray] | None = None

        if self._is_memory:
            self._memory_conn = sqlite3.connect(
                ":memory:", timeout=30.0, check_same_thread=False
            )
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.execute("PRAGMA foreign_keys=ON")
            self._memory_conn.execute("PRAGMA busy_timeout=30000")
            self._init_schema(self._memory_conn)
        else:
            self._init_file_db()

    def _init_schema(self, conn: sqlite3.Connection) -> None:
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
            raise WikiStoreError(
                f"tag wiki database version {row['version']} is newer than supported"
            )

        # Probe FTS5 support
        self._setup_fts(conn)

    def _setup_fts(self, conn: sqlite3.Connection) -> None:
        if self._fts_enabled is not None:
            return
        try:
            conn.execute("SAVEPOINT test_fts")
            # Plain execute() only: executescript() would implicitly COMMIT and
            # destroy the savepoint, so a perfectly healthy FTS5 build looked
            # like "unsupported" (the failure surfaced at RELEASE SAVEPOINT).
            conn.execute(CHUNKS_FTS_SQL)
            for statement in CHUNKS_FTS_TRIGGER_STATEMENTS:
                conn.execute(statement)
            conn.execute("RELEASE SAVEPOINT test_fts")
            self._fts_enabled = True
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('fts_available', '1')"
                " ON CONFLICT(key) DO UPDATE SET value = '1'"
            )
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT test_fts")
            except Exception:
                pass
            self._fts_enabled = False
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('fts_available', '0')"
                " ON CONFLICT(key) DO UPDATE SET value = '0'"
            )

    def _init_file_db(self) -> None:
        with self.connection() as conn:
            self._init_schema(conn)

    @contextlib.contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Context manager yielding a transaction-wrapped connection."""

        if self._memory_conn is not None:
            with self._lock:
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
                with self._lock:
                    try:
                        yield conn
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
            finally:
                conn.close()

    def fts_available(self) -> bool:
        """Return whether FTS5 virtual tables are available."""

        if self._fts_enabled is None:
            with self.connection() as conn:
                self._setup_fts(conn)
        return bool(self._fts_enabled)

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        """Set a metadata key/value pair."""

        with self.connection() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )

    def get_meta(self, key: str) -> str | None:
        """Retrieve a metadata value by key."""

        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (str(key),)
            ).fetchone()
        return str(row["value"]) if row is not None else None

    # -- pages & chunks -----------------------------------------------------

    def upsert_page(self, page: Mapping[str, Any]) -> str:
        """Atomically upsert a wiki page, its chunks, and wiki links.

        Keys in `page`:
          - `title`: raw display title or string (normalized to PK)
          - `display_title`: display title string
          - `body_md`: raw DText markdown
          - `wiki_id`: int or None
          - `updated_at`: ISO str or None
          - `url`: str or None
          - `sections`: list of {"heading": str, "text": str}
          - `links`: list[str] normalized target names

        Returns the normalized page title.
        """

        raw_title = str(page.get("title", ""))
        norm_title = normalize_title(raw_title)
        display_title = str(page.get("display_title", raw_title))
        body_md = str(page.get("body_md", ""))
        wiki_id = page.get("wiki_id")
        if wiki_id is not None:
            try:
                wiki_id = int(wiki_id)
            except (ValueError, TypeError):
                wiki_id = None
        updated_at = str(page["updated_at"]) if page.get("updated_at") is not None else None
        url = str(page["url"]) if page.get("url") is not None else None
        sections = page.get("sections", ())
        links = page.get("links", ())

        with self.connection() as conn:
            conn.execute(
                "INSERT INTO pages (title, display_title, body_md, wiki_id, updated_at, url, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(title) DO UPDATE SET"
                "  display_title = excluded.display_title,"
                "  body_md = excluded.body_md,"
                "  wiki_id = excluded.wiki_id,"
                "  updated_at = excluded.updated_at,"
                "  url = excluded.url,"
                "  imported_at = excluded.imported_at",
                (
                    norm_title,
                    display_title,
                    body_md,
                    wiki_id,
                    updated_at,
                    url,
                    utc_now(),
                ),
            )

            # Delete existing chunks and page links
            conn.execute("DELETE FROM chunks WHERE page_title = ?", (norm_title,))
            conn.execute("DELETE FROM page_links WHERE page_title = ?", (norm_title,))

            # Insert chunks (skip empty text)
            pos = 0
            for sec in sections:
                if not isinstance(sec, Mapping):
                    continue
                heading = str(sec.get("heading", "")).strip()
                text = str(sec.get("text", "")).strip()
                if not text:
                    continue
                chash = _content_hash(text)
                conn.execute(
                    "INSERT INTO chunks (page_title, heading, body, position, content_hash, embedding)"
                    " VALUES (?, ?, ?, ?, ?, NULL)",
                    (norm_title, heading, text, pos, chash),
                )
                pos += 1

            # Insert links
            seen_links: set[str] = set()
            for link in links:
                target = normalize_title(str(link))
                if target and target != norm_title and target not in seen_links:
                    seen_links.add(target)
                    conn.execute(
                        "INSERT OR IGNORE INTO page_links (page_title, link_title)"
                        " VALUES (?, ?)",
                        (norm_title, target),
                    )

        # Invalidate embedding matrix cache
        self._embedding_matrix_cache = None
        return norm_title

    def get_page(self, title: str) -> dict[str, Any] | None:
        """Retrieve full wiki page info, summary, chunks as sections, and related tags."""

        norm_title = normalize_title(title)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM pages WHERE title = ?", (norm_title,)
            ).fetchone()
            if row is None:
                return None

            # Summary
            sum_row = conn.execute(
                "SELECT * FROM summaries WHERE page_title = ?", (norm_title,)
            ).fetchone()
            summary = _summary_dict(sum_row) if sum_row is not None else None

            # Sections (from chunks)
            chunk_rows = conn.execute(
                "SELECT heading, body FROM chunks WHERE page_title = ? ORDER BY position ASC, id ASC",
                (norm_title,),
            ).fetchall()
            sections = [{"heading": str(r["heading"]), "text": str(r["body"])} for r in chunk_rows]

            # Related tags (from page_links)
            link_rows = conn.execute(
                "SELECT link_title FROM page_links WHERE page_title = ? ORDER BY link_title ASC",
                (norm_title,),
            ).fetchall()
            related_tags = [str(r["link_title"]) for r in link_rows]

        return {
            "title": str(row["title"]),
            "display_title": str(row["display_title"]),
            "body_md": str(row["body_md"]),
            "wiki_id": row["wiki_id"],
            "updated_at": row["updated_at"],
            "url": row["url"],
            "imported_at": str(row["imported_at"]),
            "summary": summary,
            "sections": sections,
            "related_tags": related_tags,
        }

    # -- stats & counts -----------------------------------------------------

    def has_data(self) -> bool:
        """Return True if at least one page exists in the store."""

        return self.page_count() > 0

    def page_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM pages").fetchone()
            return int(row[0]) if row else 0

    def chunk_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            return int(row[0]) if row else 0

    def embedded_chunk_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()
            return int(row[0]) if row else 0

    def summary_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM summaries").fetchone()
            return int(row[0]) if row else 0

    def iter_page_titles(self) -> list[str]:
        """Return all normalized page titles."""

        with self.connection() as conn:
            rows = conn.execute("SELECT title FROM pages ORDER BY title ASC").fetchall()
            return [str(r["title"]) for r in rows]

    def page_meta(self) -> dict[str, Any]:
        """Return aggregate statistics dictionary."""

        pages = self.page_count()
        return {
            "exists": pages > 0,
            "pages": pages,
            "chunks": self.chunk_count(),
            "embedded_chunks": self.embedded_chunk_count(),
            "translated_pages": self.summary_count(),
            "dump_date": self.get_meta("dump_date"),
        }

    # -- embeddings ---------------------------------------------------------

    def pending_embedding_chunks(self, limit: int = 256) -> list[dict[str, Any]]:
        """Return chunks where embedding is NULL, ordered stably by id."""

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, page_title, heading, body FROM chunks"
                " WHERE embedding IS NULL"
                " ORDER BY id ASC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "page_title": str(r["page_title"]),
                "heading": str(r["heading"]),
                "text": str(r["body"]),
            }
            for r in rows
        ]

    def chunks_by_ids(self, chunk_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Return ``{"id", "page_title", "heading", "text"}`` rows for the ids.

        Used by the searcher to render vector-only hits, whose ids come from
        the in-memory embedding matrix. Missing ids are silently dropped.
        """

        ids = [int(value) for value in chunk_ids]
        if not ids:
            return []
        with self.connection() as conn:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                "SELECT id, page_title, heading, body FROM chunks"
                f" WHERE id IN ({placeholders})",
                ids,
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "page_title": str(r["page_title"]),
                "heading": str(r["heading"]),
                "text": str(r["body"]),
            }
            for r in rows
        ]

    def clear_embeddings(self) -> int:
        """Set every chunk's embedding back to NULL and return the row count.

        Backs the build pipeline's ``force_reembed`` option; the in-memory
        matrix cache is invalidated so the next search rebuilds it lazily.
        """

        with self.connection() as conn:
            cursor = conn.execute("UPDATE chunks SET embedding = NULL WHERE embedding IS NOT NULL")
            affected = int(cursor.rowcount or 0)
        self._embedding_matrix_cache = None
        return affected

    def delete_chunks_for_pages(self, page_titles: Sequence[str]) -> int:
        """Delete every chunk belonging to the given pages; return the count.

        Used by the build pipeline to drop chunks of pages that are useless
        for semantic search (artist/character/contributor pages whose bodies
        are link lists). The pages themselves stay; the FTS rows follow via
        trigger and the embedding matrix cache is invalidated.
        """

        titles = [normalize_title(str(title)) for title in page_titles]
        titles = [title for title in titles if title]
        if not titles:
            return 0
        affected = 0
        with self.connection() as conn:
            # SQLite limits bound variables per statement (~32k); 44k+ artist
            # pages need batching.
            for start in range(0, len(titles), 500):
                batch = titles[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor = conn.execute(
                    f"DELETE FROM chunks WHERE page_title IN ({placeholders})",
                    batch,
                )
                affected += int(cursor.rowcount or 0)
        self._embedding_matrix_cache = None
        return affected

    def delete_link_soup_chunks(self) -> int:
        """Delete chunks whose body is nothing but links and placeholders.

        Contributor pages, uncategorized stub pages and reference sections
        keep nothing but ``"Site":https://...`` lines, bare URLs and
        ``thumb #id`` tokens; e5 embeds that soup into vectors that crowd
        real prose out of semantic search. Pages stay and FTS rows follow
        via trigger. Idempotent; invalidates the embedding matrix cache.
        """

        with self.connection() as conn:
            doomed = [
                int(row["id"])
                for row in conn.execute("SELECT id, body FROM chunks")
                if is_link_soup(str(row["body"]))
            ]
            affected = 0
            for start in range(0, len(doomed), 500):
                batch = doomed[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                cursor = conn.execute(
                    f"DELETE FROM chunks WHERE id IN ({placeholders})", batch
                )
                affected += int(cursor.rowcount or 0)
        self._embedding_matrix_cache = None
        return affected

    def mark_embedded(self, chunk_ids: Sequence[int], vectors: np.ndarray) -> None:
        """Store float32 little-endian vector bytes for the given chunks.

        Also updates meta "embedding_dim" and invalidates the cached matrix.
        """

        if not chunk_ids:
            return
        arr = np.asarray(vectors, dtype="<f4")
        if len(chunk_ids) != arr.shape[0]:
            raise WikiStoreError(
                f"chunk_ids length ({len(chunk_ids)}) does not match vectors rows ({arr.shape[0]})"
            )
        dim = int(arr.shape[1]) if arr.ndim > 1 else int(arr.shape[0])

        with self.connection() as conn:
            for chunk_id, vec in zip(chunk_ids, arr):
                blob = np.asarray(vec, dtype="<f4").tobytes()
                conn.execute(
                    "UPDATE chunks SET embedding = ? WHERE id = ?",
                    (sqlite3.Binary(blob), int(chunk_id)),
                )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('embedding_dim', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(dim),),
            )

        self._embedding_matrix_cache = None

    def load_embedding_matrix(self) -> tuple[list[int], np.ndarray]:
        """Load all embedded chunk IDs and the float32 [N, dim] matrix.

        Caches the matrix per instance until invalidated.
        """

        if self._embedding_matrix_cache is not None:
            return self._embedding_matrix_cache

        dim_str = self.get_meta("embedding_dim")
        dim = int(dim_str) if dim_str is not None else 384

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL ORDER BY id ASC"
            ).fetchall()

        if not rows:
            empty_matrix = np.empty((0, dim), dtype="<f4")
            result: tuple[list[int], np.ndarray] = ([], empty_matrix)
            self._embedding_matrix_cache = result
            return result

        ids: list[int] = []
        vectors: list[np.ndarray] = []
        for r in rows:
            ids.append(int(r["id"]))
            raw_blob: bytes = r["embedding"]
            vec = np.frombuffer(raw_blob, dtype="<f4")
            vectors.append(vec)

        matrix = np.stack(vectors, axis=0) if vectors else np.empty((0, dim), dtype="<f4")
        result = (ids, matrix)
        self._embedding_matrix_cache = result
        return result

    # -- text search --------------------------------------------------------

    def search_text(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search chunks using FTS5 (if available) or LIKE fallback."""

        cleaned = query.strip()
        if not cleaned or limit <= 0:
            return []

        if self.fts_available():
            try:
                return self._search_fts(cleaned, limit)
            except Exception:
                return self._search_like(cleaned, limit)
        return self._search_like(cleaned, limit)

    def _search_fts(self, query: str, limit: int) -> list[dict[str, Any]]:
        # Sanitize query for FTS5: wrap words in double quotes to avoid syntax errors
        tokens = [t.replace('"', '""') for t in query.split() if t.strip()]
        if not tokens:
            return []
        match_expr = " ".join(f'"{t}"*' for t in tokens)

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT c.id, c.page_title, c.heading, c.body"
                " FROM chunks_fts f"
                " JOIN chunks c ON f.rowid = c.id"
                " WHERE chunks_fts MATCH ?"
                " ORDER BY bm25(chunks_fts) ASC"
                " LIMIT ?",
                (match_expr, limit),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "page_title": str(r["page_title"]),
                "heading": str(r["heading"]),
                "text": str(r["body"]),
            }
            for r in rows
        ]

    def _search_like(self, query: str, limit: int) -> list[dict[str, Any]]:
        pattern = f"%{query}%"
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, page_title, heading, body FROM chunks"
                " WHERE body LIKE ? OR heading LIKE ? OR page_title LIKE ?"
                " ORDER BY id ASC"
                " LIMIT ?",
                (pattern, pattern, pattern, limit),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "page_title": str(r["page_title"]),
                "heading": str(r["heading"]),
                "text": str(r["body"]),
            }
            for r in rows
        ]

    # -- summaries ----------------------------------------------------------

    def upsert_summary(self, title: str, summary: Mapping[str, Any]) -> None:
        """Upsert a Chinese structured summary for a wiki page."""

        norm_title = normalize_title(title)
        meaning = str(summary.get("meaning", ""))
        usage = str(summary.get("usage", ""))
        pairing = str(summary.get("pairing", ""))
        notes = str(summary.get("notes", ""))
        raw_tags = summary.get("tags", [])
        tags_json = json.dumps(
            [str(t) for t in raw_tags] if isinstance(raw_tags, (list, tuple, set)) else [],
            ensure_ascii=False,
        )
        provider_id = str(summary.get("provider_id", ""))
        model = str(summary.get("model", ""))
        updated_at = str(summary.get("updated_at", utc_now()))

        with self.connection() as conn:
            conn.execute(
                "INSERT INTO summaries"
                " (page_title, meaning, usage, pairing, notes, tags, provider_id, model, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(page_title) DO UPDATE SET"
                "  meaning = excluded.meaning,"
                "  usage = excluded.usage,"
                "  pairing = excluded.pairing,"
                "  notes = excluded.notes,"
                "  tags = excluded.tags,"
                "  provider_id = excluded.provider_id,"
                "  model = excluded.model,"
                "  updated_at = excluded.updated_at",
                (
                    norm_title,
                    meaning,
                    usage,
                    pairing,
                    notes,
                    tags_json,
                    provider_id,
                    model,
                    updated_at,
                ),
            )

    def get_summary(self, title: str) -> dict[str, Any] | None:
        """Get the parsed summary dictionary for a title."""

        norm_title = normalize_title(title)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM summaries WHERE page_title = ?", (norm_title,)
            ).fetchone()
        return _summary_dict(row) if row is not None else None

    def get_summaries_by_titles(self, titles: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Batch variant of :meth:`get_summary` keyed by normalized title.

        Used by search-hit enrichment so one query round-trip replaces one
        connection per hit.
        """

        norm_titles = [normalize_title(str(title)) for title in titles]
        norm_titles = [title for title in norm_titles if title]
        if not norm_titles:
            return {}
        unique_titles = list(dict.fromkeys(norm_titles))
        result: dict[str, dict[str, Any]] = {}
        with self.connection() as conn:
            for start in range(0, len(unique_titles), 500):
                batch = unique_titles[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT * FROM summaries WHERE page_title IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    result[str(row["page_title"])] = _summary_dict(row)
        return result

    def missing_summary_titles(
        self, titles: Sequence[str], limit: int | None = None
    ) -> list[str]:
        """Filter the input list of titles to those that do NOT have a summary.

        With ``limit`` set, stops as soon as that many missing titles are
        collected instead of scanning the whole input.
        """

        if not titles:
            return []
        if limit is not None and limit <= 0:
            return []
        norm_map = {normalize_title(t): t for t in titles}
        norm_keys = list(norm_map.keys())

        # Check existing in chunks of 500
        missing: list[str] = []
        batch_size = 500
        with self.connection() as conn:
            for i in range(0, len(norm_keys), batch_size):
                batch = norm_keys[i : i + batch_size]
                placeholders = ", ".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT page_title FROM summaries WHERE page_title IN ({placeholders})",
                    batch,
                ).fetchall()
                found = {str(r["page_title"]) for r in rows}
                for k in batch:
                    if k not in found:
                        missing.append(norm_map[k])
                        if limit is not None and len(missing) >= limit:
                            return missing
        return missing

    def close(self) -> None:
        """Close memory connection if open."""

        if self._memory_conn is not None:
            with self._lock:
                try:
                    self._memory_conn.close()
                except Exception:
                    pass
                self._memory_conn = None


def _summary_dict(row: sqlite3.Row) -> dict[str, Any]:
    try:
        tags = json.loads(str(row["tags"]))
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []
    return {
        "meaning": str(row["meaning"]),
        "usage": str(row["usage"]),
        "pairing": str(row["pairing"]),
        "notes": str(row["notes"]),
        "tags": tags,
        "provider_id": str(row["provider_id"]),
        "model": str(row["model"]),
        "updated_at": str(row["updated_at"]),
    }


__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "WikiStore",
    "WikiStoreError",
    "default_tag_wiki_database_path",
    "normalize_title",
]
