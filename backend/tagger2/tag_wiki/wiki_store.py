"""SQLite storage layer for the tag wiki mirror, catalog, and summaries.

This module provides the local persistence for the booru tag wiki mirror,
storing pages, parsed chunks (section bodies), wiki-link relationships,
generated Chinese summaries, and the read-only high-frequency tag catalog.

Schema v2 additionally holds the read-only tag catalog (``catalog_tags`` /
``catalog_relations`` / ``catalog_meta``): a booru-style directory of
high-frequency tags maintained exclusively by
``scripts/build_tag_wiki_catalog.py`` and served by the ``/catalog`` API.
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

from ..workflow.contracts import utc_now

SCHEMA_VERSION = 3

# v2 additive migration: the read-only tag catalog (booru-style directory of
# high-frequency tags) written by scripts/build_tag_wiki_catalog.py. The
# tables live in the same per-profile database as pages/chunks/summaries but
# are fully independent: the catalog is rebuilt wholesale by the CLI while
# user-facing reads only ever see rows written by that CLI.
CATALOG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalog_tags (
    name TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    group_key TEXT NOT NULL DEFAULT '',
    post_count INTEGER NOT NULL DEFAULT 0,
    has_wiki INTEGER NOT NULL DEFAULT 0,
    alias_of TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_tags_group
    ON catalog_tags(category, group_key, post_count DESC);
CREATE INDEX IF NOT EXISTS idx_catalog_tags_post_count
    ON catalog_tags(post_count DESC, name);

CREATE TABLE IF NOT EXISTS catalog_relations (
    tag_name TEXT NOT NULL,
    related_name TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tag_name, related_name, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_catalog_relations_tag
    ON catalog_relations(tag_name, relation_type, score DESC);
CREATE INDEX IF NOT EXISTS idx_catalog_relations_related
    ON catalog_relations(related_name, relation_type, score DESC);

CREATE TABLE IF NOT EXISTS catalog_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

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
    content_hash TEXT NOT NULL
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
        self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Apply schema migrations up to ``SCHEMA_VERSION``.

        v1 -> v2 adds the tag-catalog tables (``catalog_tags``,
        ``catalog_relations``, ``catalog_meta``). v2 -> v3 removes the
        retired vector-search structures: the ``chunks.embedding`` column,
        the FTS5 virtual table with its sync triggers, and their meta keys.
        The catalog DDL is ``CREATE ... IF NOT EXISTS`` so re-running on a
        current database is a no-op and upgrading never touches user rows.
        """

        row = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current = int(row["version"]) if row is not None else None
        if current is not None and current > SCHEMA_VERSION:
            raise WikiStoreError(
                f"tag wiki database version {current} is newer than supported"
            )
        if current == SCHEMA_VERSION:
            # Still run the additive DDL: a version marker row could predate
            # its tables if a migration was interrupted mid-way.
            conn.executescript(CATALOG_SCHEMA_SQL)
            return
        conn.executescript(CATALOG_SCHEMA_SQL)
        if current is not None:
            # v1/v2 -> v3: the vector-search stack is gone (the browse UI is
            # the read-only tag catalog), so drop its storage.
            for trigger in ("chunks_ai", "chunks_ad", "chunks_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("DROP TABLE IF EXISTS chunks_fts")
            conn.execute("ALTER TABLE chunks DROP COLUMN embedding")
            conn.execute("DELETE FROM meta WHERE key IN ('embedding_dim', 'fts_available')")
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum, applied_at)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(version) DO NOTHING",
            (SCHEMA_VERSION, f"schema-v{SCHEMA_VERSION}", utc_now()),
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

        with self.connection() as conn:
            return self._upsert_page_on_conn(conn, page)

    def upsert_pages(self, pages: Sequence[Mapping[str, Any]]) -> list[str]:
        """Upsert many wiki pages in a single transaction; return their titles.

        Bulk path for corpus-sized imports: one connection and one commit per
        call instead of one connection per page. Every page goes through the
        same rewrite as :meth:`upsert_page` (chunks and links replaced
        wholesale). Returns one normalized title per input page, in order.
        """

        titles: list[str] = []
        with self.connection() as conn:
            for page in pages:
                titles.append(self._upsert_page_on_conn(conn, page))
        return titles

    def _upsert_page_on_conn(self, conn: sqlite3.Connection, page: Mapping[str, Any]) -> str:
        """Write one page (and replace its chunks/links) on an open connection."""

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
                "INSERT INTO chunks (page_title, heading, body, position, content_hash)"
                " VALUES (?, ?, ?, ?, ?)",
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

    def get_pages_snapshot(self, titles: Sequence[str]) -> dict[str, dict[str, Any]]:
        """Return ``{normalized_title: {updated_at, body_md}}`` for stored pages.

        Bulk variant of the incremental-import unchanged check: one query per
        500-title batch instead of one connection per page. Titles missing
        from the store are absent from the result.
        """

        normalized = [normalize_title(str(title)) for title in titles]
        normalized = [title for title in normalized if title]
        snapshot: dict[str, dict[str, Any]] = {}
        with self.connection() as conn:
            for start in range(0, len(normalized), 500):
                batch = normalized[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"SELECT title, updated_at, body_md FROM pages WHERE title IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    snapshot[str(row["title"])] = {
                        "updated_at": row["updated_at"],
                        "body_md": str(row["body_md"]),
                    }
        return snapshot

    def delete_page(self, title: str) -> bool:
        """Remove one page with its chunks, links and summary; return existence.

        Used when an upstream wiki page is deleted (danbooru marks pages
        ``is_deleted``).
        """

        norm_title = normalize_title(str(title))
        if not norm_title:
            return False
        with self.connection() as conn:
            row = conn.execute("SELECT 1 FROM pages WHERE title = ?", (norm_title,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM chunks WHERE page_title = ?", (norm_title,))
            conn.execute("DELETE FROM page_links WHERE page_title = ?", (norm_title,))
            conn.execute("DELETE FROM summaries WHERE page_title = ?", (norm_title,))
            conn.execute("DELETE FROM pages WHERE title = ?", (norm_title,))
        return True

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
            "translated_pages": self.summary_count(),
            "dump_date": self.get_meta("dump_date"),
        }

    # -- chunk pruning (build pipeline) --------------------------------------

    def delete_chunks_for_pages(self, page_titles: Sequence[str]) -> int:
        """Delete every chunk belonging to the given pages; return the count.

        Used by the build pipeline to drop chunks of pages that carry nothing
        but link lists (artist/character/contributor pages). The pages
        themselves stay.
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
        return affected

    def delete_link_soup_chunks(self) -> int:
        """Delete chunks whose body is nothing but links and placeholders.

        Contributor pages, uncategorized stub pages and reference sections
        keep nothing but ``"Site":https://...`` lines, bare URLs and
        ``thumb #id`` tokens. Pages stay. Idempotent.
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
        return affected

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

    # -- tag catalog (read-only directory; written only by the build CLI) ----

    def replace_catalog(
        self,
        tags: Sequence[Mapping[str, Any]],
        relations: Sequence[Mapping[str, Any]] = (),
        meta: Mapping[str, Any] | None = None,
    ) -> int:
        """Atomically rebuild the catalog: clear, bulk-write, store meta.

        Tags are mappings with ``name`` plus optional ``category``,
        ``group_key``, ``post_count``, ``has_wiki`` and ``alias_of``.
        Relations carry ``tag_name``/``related_name``/``relation_type`` and
        optional ``score``/``source``; duplicates collapse silently. The
        whole swap happens in one transaction so readers never observe a
        half-written catalog. Returns the number of tags written.
        """

        with self.connection() as conn:
            conn.execute("DELETE FROM catalog_relations")
            conn.execute("DELETE FROM catalog_tags")
            seen: set[str] = set()
            written = 0
            for tag in tags:
                name = normalize_title(str(tag.get("name", "")))
                if not name or name in seen:
                    continue
                seen.add(name)
                conn.execute(
                    "INSERT INTO catalog_tags"
                    " (name, category, group_key, post_count, has_wiki, alias_of)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        name,
                        str(tag.get("category", "") or ""),
                        str(tag.get("group_key", "") or ""),
                        max(0, int(tag.get("post_count") or 0)),
                        1 if tag.get("has_wiki") else 0,
                        str(tag["alias_of"]) if tag.get("alias_of") else None,
                    ),
                )
                written += 1
            seen_relations: set[tuple[str, str, str]] = set()
            for relation in relations:
                tag_name = normalize_title(str(relation.get("tag_name", "")))
                related_name = normalize_title(str(relation.get("related_name", "")))
                relation_type = str(relation.get("relation_type", "") or "")
                if not tag_name or not related_name or not relation_type:
                    continue
                if tag_name == related_name:
                    continue
                key = (tag_name, related_name, relation_type)
                if key in seen_relations:
                    continue
                seen_relations.add(key)
                conn.execute(
                    "INSERT OR IGNORE INTO catalog_relations"
                    " (tag_name, related_name, relation_type, score, source)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        tag_name,
                        related_name,
                        relation_type,
                        float(relation.get("score") or 0.0),
                        str(relation.get("source", "") or ""),
                    ),
                )
            for meta_key, meta_value in (meta or {}).items():
                conn.execute(
                    "INSERT INTO catalog_meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(meta_key), str(meta_value)),
                )
        return written

    def catalog_built(self) -> bool:
        """Whether the catalog CLI has written at least one generation."""

        return self.catalog_tag_count() > 0

    def catalog_meta(self) -> dict[str, Any]:
        """All ``catalog_meta`` rows, integer-coerced where possible."""

        result: dict[str, Any] = {}
        with self.connection() as conn:
            rows = conn.execute("SELECT key, value FROM catalog_meta").fetchall()
        for row in rows:
            value: Any = str(row["value"])
            try:
                value = int(value)
            except (TypeError, ValueError):
                pass
            result[str(row["key"])] = value
        return result

    def catalog_tag_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM catalog_tags").fetchone()
        return int(row[0]) if row else 0

    def catalog_relation_count(self) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM catalog_relations").fetchone()
        return int(row[0]) if row else 0

    def catalog_categories_stats(self) -> list[dict[str, Any]]:
        """Per (category, group_key) tag counts for the browse sidebar."""

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT category, group_key, COUNT(*) AS tag_count,"
                " SUM(has_wiki) AS wiki_count"
                " FROM catalog_tags"
                " GROUP BY category, group_key"
                " ORDER BY category ASC, tag_count DESC, group_key ASC"
            ).fetchall()
        return [
            {
                "category": str(row["category"]),
                "group_key": str(row["group_key"]),
                "tag_count": int(row["tag_count"]),
                "wiki_count": int(row["wiki_count"] or 0),
            }
            for row in rows
        ]

    @staticmethod
    def _catalog_filters(
        category: str | None, group: str | None, q: str | None
    ) -> tuple[list[str], list[Any]]:
        """Shared WHERE fragments for the catalog read queries."""

        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if group:
            clauses.append("group_key = ?")
            params.append(group)
        if q:
            # Tags contain underscores, so LIKE wildcards in the query must
            # be literal matches; escape and bind an ESCAPE character.
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")
        return clauses, params

    def catalog_browse(
        self,
        *,
        category: str | None = None,
        group: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 60,
    ) -> tuple[int, list[dict[str, Any]]]:
        """Page through catalog tags, post_count desc then name asc.

        Returns ``(total_matching, items)``; ``q`` is a case-insensitive
        substring filter over the canonical name (ranking refinement happens
        in the service layer, which needs alias information the store does
        not hold).
        """

        clauses, params = self._catalog_filters(category, group, q)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM catalog_tags{where}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT name, category, group_key, post_count, has_wiki, alias_of"
                f" FROM catalog_tags{where}"
                " ORDER BY post_count DESC, name ASC"
                " LIMIT ? OFFSET ?",
                [*params, max(0, limit), max(0, offset)],
            ).fetchall()
        return total, [_catalog_tag_row(row) for row in rows]

    def catalog_get_tag(self, name: str) -> dict[str, Any] | None:
        """One catalog tag by canonical (normalized) name."""

        key = normalize_title(str(name))
        if not key:
            return None
        with self.connection() as conn:
            row = conn.execute(
                "SELECT name, category, group_key, post_count, has_wiki, alias_of"
                " FROM catalog_tags WHERE name = ?",
                (key,),
            ).fetchone()
        return _catalog_tag_row(row) if row is not None else None

    def catalog_relations_for(
        self, name: str, *, relation_type: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Relations touching ``name`` in both directions.

        Forward rows have ``tag_name = name`` (direction ``forward``);
        reverse rows have ``related_name = name`` (direction ``reverse``).
        Ordered by score desc then related/tag name for stable output.
        """

        key = normalize_title(str(name))
        if not key:
            return []
        type_clause = " AND relation_type = ?" if relation_type else ""
        type_params = [relation_type] if relation_type else []
        sql = (
            "SELECT tag_name, related_name, relation_type, score, source,"
            " 'forward' AS direction FROM catalog_relations"
            f" WHERE tag_name = ?{type_clause}"
            " UNION ALL "
            "SELECT tag_name, related_name, relation_type, score, source,"
            " 'reverse' AS direction FROM catalog_relations"
            f" WHERE related_name = ?{type_clause}"
        )
        limit_clause = f" LIMIT {int(limit)}" if limit is not None else ""
        with self.connection() as conn:
            rows = conn.execute(
                sql + " ORDER BY score DESC, related_name ASC, tag_name ASC" + limit_clause,
                [key, *type_params, key, *type_params],
            ).fetchall()
        return [
            {
                "tag_name": str(row["tag_name"]),
                "related_name": str(row["related_name"]),
                "relation_type": str(row["relation_type"]),
                "score": float(row["score"] or 0.0),
                "source": str(row["source"]),
                "direction": str(row["direction"]),
            }
            for row in rows
        ]

    def iter_page_links(self) -> Iterator[tuple[str, str]]:
        """Yield every stored ``(page_title, link_title)`` wiki-link pair.

        Streaming source for the catalog CLI: the page-link table can hold
        hundreds of thousands of pairs, so the caller must be able to filter
        without materializing everything through a list first.
        """

        with self.connection() as conn:
            cursor = conn.execute(
                "SELECT page_title, link_title FROM page_links ORDER BY page_title ASC"
            )
            while True:
                batch = cursor.fetchmany(2048)
                if not batch:
                    break
                for row in batch:
                    yield str(row["page_title"]), str(row["link_title"])

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


def _catalog_tag_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape one ``catalog_tags`` row for the service/API layers."""

    return {
        "name": str(row["name"]),
        "category": str(row["category"]),
        "group_key": str(row["group_key"]),
        "post_count": int(row["post_count"] or 0),
        "has_wiki": bool(row["has_wiki"]),
        "alias_of": str(row["alias_of"]) if row["alias_of"] else None,
    }


__all__ = [
    "CATALOG_SCHEMA_SQL",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "WikiStore",
    "WikiStoreError",
    "default_tag_wiki_database_path",
    "normalize_title",
]
