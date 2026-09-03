"""Importer for the official e621 ``wiki_pages`` db_export CSV dump.

The importer mirrors the download conventions of ``scripts/`` against
``https://e621.net/db_export/`` (a descriptive User-Agent header is required by
e621), parses the gzipped CSV lazily, and feeds normalized pages into
:class:`~tagger2.tag_wiki.wiki_store.WikiStore`. Because the store rewrites all
chunks (dropping their embeddings) on every ``upsert_page``, the importer
implements the incremental logic itself: a dump row whose stored ``updated_at``
and raw body are unchanged is skipped entirely.

Progress reporting: the dump size is unknown while streaming, so
:func:`import_dump` calls ``progress(done, -1)`` every
``PROGRESS_INTERVAL`` rows (``-1`` total means "unknown"), and
:func:`download_dump` calls ``progress(bytes_done, bytes_total)`` per chunk
only when the response carries a ``Content-Length`` header.
"""

from __future__ import annotations

import csv
import gzip
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from ..workflow.contracts import utc_now
from .contracts import MAX_CHUNK_CHARS, MIN_CHUNK_CHARS, ERROR_WIKI_BUILD_FAILED
from .wiki_store import WikiStore, is_link_soup, normalize_title

logger = logging.getLogger("tagger2.tag_wiki.importer")

DUMP_LIST_URL = "https://e621.net/db_export/"

# e621 rejects requests without a descriptive User-Agent; keep the same
# "application/purpose" style used by the repo's other e621 fetchers.
USER_AGENT = "Tagger2-TagWiki/1.6 (e621 db_export wiki_pages importer)"

# How many dump rows between import_dump progress callbacks.
PROGRESS_INTERVAL = 500

# Number of attempts (with short backoff) for each HTTP request.
_HTTP_ATTEMPTS = 3

# Matches one ``wiki_pages-YYYY-MM-DD.csv.gz`` entry anywhere in a listing
# page or path; group 1 is the dump date.
_DUMP_ENTRY_RE = re.compile(r"wiki_pages-(\d{4}-\d{2}-\d{2})\.csv\.gz")

# e621 now lists dumps as anchors whose href may be absolute (static1.e621.net)
# or site-relative, and the file name may carry no date at all. Match any
# anchor pointing at a wiki_pages CSV; group 1 is the raw href.
_DUMP_HREF_RE = re.compile(r"""href=["']([^"']*wiki_pages[^"']*\.csv\.gz)["']""", re.IGNORECASE)

# DText heading lines: h2./h3./h4./h5. at the start of a line.
_HEADING_RE = re.compile(r"^h[2-5]\.\s*(.*)$")

# Bracket markup tags such as [b], [/b], [section=expanded]; the leading name
# must be alphabetic so numbered footnote brackets like [1] survive.
_DTEXT_TAG_RE = re.compile(r"\[/?[A-Za-z][A-Za-z0-9_]*(?:=[^\]\n]*)?\]")

# Wiki links [[target]] / [[target|label]] and search links {{tag}} in one
# scanning pattern so the original body order is preserved; group 1/2 holds the
# full bracket interior (target, optionally "|label").
_DTEXT_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]|\{\{([^{}]+)\}\}")

# Columns of the official dump that carry the wiki page id.
_WIKI_ID_COLUMNS = ("id", "wiki_page_id")

# Columns of the official dump that carry the last update timestamp.
_UPDATED_AT_COLUMNS = ("updated_at", "updated_on")

# CSV spellings of a SQL NULL in the official dumps.
_NULL_TOKENS = {"", "\\N", "NULL"}

GZIP_MAGIC = b"\x1f\x8b"


class ImporterError(RuntimeError):
    """Raised when the wiki dump listing, download, parse, or import fails."""

    def __init__(self, message: str, *, code: str = ERROR_WIKI_BUILD_FAILED) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# -- dump listing --------------------------------------------------------------


def extract_dump_entries(html: str) -> list[tuple[str, str]]:
    """Extract ``(date, absolute_url)`` for every wiki_pages dump in a listing.

    Both listing generations are supported: dated file names
    (``wiki_pages-YYYY-MM-DD.csv.gz``) and the current undated
    ``wiki_pages.csv.gz`` served from a CDN host. Undated dumps get an empty
    date and sort after dated ones; duplicates collapse into one entry.
    """

    entries: set[tuple[str, str]] = set()
    for match in _DUMP_HREF_RE.finditer(html):
        href = match.group(1).strip()
        if not href:
            continue
        url = urljoin(DUMP_LIST_URL, href)
        dated = _DUMP_ENTRY_RE.search(url)
        entries.add((dated.group(1) if dated else "", url))
    if not entries:
        # Older listings mention the bare file name without an anchor href.
        for match in _DUMP_ENTRY_RE.finditer(html):
            entries.add((match.group(1), urljoin(DUMP_LIST_URL, match.group(0))))
    return sorted(entries, key=lambda entry: entry[0], reverse=True)


def latest_dump_url(html: str) -> str:
    """Return the absolute URL of the newest wiki_pages dump in a listing."""

    entries = extract_dump_entries(html)
    if not entries:
        raise ImporterError(
            "no wiki_pages dumps found on the e621 db_export listing",
            code=ERROR_WIKI_BUILD_FAILED,
        )
    return entries[0][1]


def dump_filename_for_url(url: str) -> str:
    """Return the local cache file name for one dump URL.

    Dated dumps keep their name; the undated CDN dump is stored under today's
    UTC date so the daily refresh logic in the build pipeline can compare
    names and re-download when a newer dump appears.
    """

    dated = _DUMP_ENTRY_RE.search(url)
    if dated:
        return f"wiki_pages-{dated.group(1)}.csv.gz"
    return f"wiki_pages-{datetime.now(UTC):%Y-%m-%d}.csv.gz"


def latest_dump_html(timeout: float = 60) -> str:
    """Fetch the e621 db_export listing page (retried, User-Agent required)."""

    last_error: Exception | None = None
    for attempt in range(_HTTP_ATTEMPTS):
        try:
            response = httpx.get(
                DUMP_LIST_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
                follow_redirects=True,
            )
            response.raise_for_status()
            return response.text
        except (httpx.HTTPError, OSError) as exc:
            last_error = exc
            if attempt < _HTTP_ATTEMPTS - 1:
                time.sleep(2**attempt)
    raise ImporterError(
        f"failed to fetch the e621 db_export listing: {last_error}",
        code=ERROR_WIKI_BUILD_FAILED,
    ) from last_error


# -- download ------------------------------------------------------------------


def download_dump(
    url: str,
    target_dir: Path,
    *,
    timeout: float = 600,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Stream one wiki_pages dump into ``target_dir`` atomically.

    The response is written to a ``.part`` file which is verified to start with
    the gzip magic bytes and then atomically renamed; older
    ``wiki_pages-*.csv.gz`` files in the directory are removed afterwards.
    ``progress(bytes_done, bytes_total)`` fires per streamed chunk, but only
    when the server declared a ``Content-Length``.
    """

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = dump_filename_for_url(url)
    final_path = target_dir / filename
    part_path = target_dir / f"{filename}.part"

    try:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            total = int(declared) if declared is not None and declared.isdigit() else None
            done = 0
            with part_path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None and total is not None:
                        progress(done, total)
    except (httpx.HTTPError, OSError) as exc:
        part_path.unlink(missing_ok=True)
        raise ImporterError(
            f"failed to download the e621 wiki dump from {url}: {exc}",
            code=ERROR_WIKI_BUILD_FAILED,
        ) from exc

    with part_path.open("rb") as handle:
        magic = handle.read(2)
    if magic != GZIP_MAGIC:
        part_path.unlink(missing_ok=True)
        raise ImporterError(
            f"downloaded e621 wiki dump {filename} is not a gzip file",
            code=ERROR_WIKI_BUILD_FAILED,
        )

    os.replace(part_path, final_path)

    # Best-effort cleanup of superseded dumps; a file held open by another
    # process (Windows locks) must not fail the download that just finished.
    for old_path in target_dir.glob("wiki_pages*.csv.gz"):
        if old_path != final_path and old_path.is_file():
            try:
                old_path.unlink()
            except OSError:
                logger.warning("could not remove superseded wiki dump %s", old_path)
    return final_path


# -- dump parsing --------------------------------------------------------------


def parse_dump(path: Path) -> Iterator[dict[str, Any]]:
    """Lazily yield usable rows from a gzipped ``wiki_pages`` CSV dump.

    Columns may appear in any order and extra columns are ignored. Rows whose
    title or body is empty are skipped. Each row is shaped as ``{"title",
    "body", "wiki_id", "updated_at"}`` where ``wiki_id`` is an ``int | None``
    (from the ``id`` or ``wiki_page_id`` column) and ``updated_at`` is the raw
    timestamp string or ``None`` (``updated_at``/``updated_on`` with ``\\N``,
    ``NULL`` or empty treated as missing).
    """

    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [name for name in ("title", "body") if name not in fieldnames]
        if missing:
            raise ImporterError(
                f"e621 wiki dump {Path(path).name} is missing required columns: {missing}",
                code=ERROR_WIKI_BUILD_FAILED,
            )
        for row in reader:
            title = str(row.get("title") or "").strip()
            body = str(row.get("body") or "")
            if not title or not body.strip():
                continue
            yield {
                "title": title,
                "body": body,
                "wiki_id": _first_int(row, _WIKI_ID_COLUMNS),
                "updated_at": _first_text(row, _UPDATED_AT_COLUMNS),
            }


def _first_int(row: dict[str, Any], columns: tuple[str, ...]) -> int | None:
    """Return the first parseable integer among the given columns, else None."""

    for column in columns:
        raw = row.get(column)
        if raw is None:
            continue
        text = str(raw).strip()
        if text in _NULL_TOKENS:
            continue
        try:
            return int(text)
        except ValueError:
            continue
    return None


def _first_text(row: dict[str, Any], columns: tuple[str, ...]) -> str | None:
    """Return the first non-null text among the given columns, else None."""

    for column in columns:
        raw = row.get(column)
        if raw is None:
            continue
        text = str(raw).strip()
        if text in _NULL_TOKENS:
            continue
        return text
    return None


# -- DText processing ----------------------------------------------------------


def extract_wiki_links(body: str, *, page_title: str | None = None) -> list[str]:
    """Return normalized targets of ``[[wiki]]``/``[[wiki|label]]``/``{{tag}}`` links.

    Targets are normalized via
    :func:`~tagger2.tag_wiki.wiki_store.normalize_title`, deduplicated in body
    order, and empty targets plus self references (target equals the optional
    ``page_title``) are dropped.
    """

    self_title = normalize_title(page_title) if page_title else ""
    links: list[str] = []
    seen: set[str] = set()
    for match in _DTEXT_LINK_RE.finditer(body):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        target = normalize_title((raw or "").partition("|")[0])
        if not target or target == self_title or target in seen:
            continue
        seen.add(target)
        links.append(target)
    return links


def strip_dtext(body: str) -> str:
    """Reduce DText markup to readable plain text.

    ``[code]...[/code]`` blocks are removed with their content. ``[[wiki]]``/
    ``[[wiki|label]]`` links become their readable target or label and
    ``{{tag}}`` search links become the tag name. Remaining bracket tags
    (``[section]``/``[section=...]``, ``[b]``, ``[i]``, ``[u]``, ``[s]``,
    ``[quote]``, and any other ``[unknown]`` marker) are dropped while the text
    they wrap is kept. Runs of 3+ newlines collapse to two and the result is
    trimmed.
    """

    text = body.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\[code\].*?\[/code\]", "", text, flags=re.DOTALL)
    text = _DTEXT_LINK_RE.sub(_readable_link, text)
    text = _DTEXT_TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _readable_link(match: re.Match[str]) -> str:
    """Render one ``[[wiki|label]]``/``{{tag}}`` match as readable text."""

    raw = match.group(1) if match.group(1) is not None else match.group(2)
    target, separator, label = (raw or "").partition("|")
    readable = label if separator and label.strip() else target
    return readable.strip()


# Word-like tokens: runs of at least two letters/digits. Used by the junk
# chunk filter below ("category:character" is one, "see also kiss" is three).
_WORD_RE = re.compile(r"[A-Za-z0-9]{2,}")


def _is_substantive(text: str, min_chars: int) -> bool:
    """Whether one chunk carries enough content to be worth embedding.

    A chunk must reach ``min_chars`` characters AND contain at least three
    word-like tokens. This drops the degenerate fragments that made up ~8% of
    a real wiki dump (lone punctuation, ``category:character`` stubs, ASCII
    art, bare link lines) whose mean-pooled vectors otherwise dominate the
    top of every semantic query.
    """

    if len(text) < min_chars:
        return False
    return len(_WORD_RE.findall(text)) >= 3


def parse_dtext_sections(
    body: str,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
    min_chunk_chars: int = MIN_CHUNK_CHARS,
) -> list[dict[str, str]]:
    """Split DText into ``{"heading", "text"}`` sections for chunking.

    Lines starting with the DText heading markers ``h2.``/``h3.``/``h4.``/
    ``h5.`` begin a new section whose heading is the markup-stripped remainder;
    text before the first heading belongs to a section with heading ``""``.
    Every section is markup-stripped, sections longer than ``max_chunk_chars``
    are split at paragraph boundaries (``\\n\\n``; an oversized single paragraph
    is hard-split) keeping the same heading, and empty-text sections are
    dropped. Chunks shorter than ``min_chunk_chars`` are dropped as well: tiny
    fragments embed into degenerate vectors that pollute semantic search
    (pass ``min_chunk_chars=0`` to keep everything).
    """

    raw_sections: list[tuple[str, str]] = []
    heading = ""
    lines: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match is not None:
            if lines:
                raw_sections.append((heading, "\n".join(lines)))
                lines = []
            heading = strip_dtext(match.group(1)).strip()
        else:
            lines.append(line)
    if lines:
        raw_sections.append((heading, "\n".join(lines)))

    sections: list[dict[str, str]] = []
    for section_heading, raw_text in raw_sections:
        text = strip_dtext(raw_text)
        for chunk in _split_paragraph_chunks(text, max_chunk_chars):
            stripped = chunk.strip()
            if min_chunk_chars > 0:
                if not _is_substantive(stripped, min_chunk_chars) or is_link_soup(stripped):
                    continue
            sections.append({"heading": section_heading, "text": stripped})
    return sections


def _split_paragraph_chunks(text: str, max_chunk_chars: int) -> list[str]:
    """Split text at ``\\n\\n`` boundaries so every chunk fits the cap."""

    text = text.strip()
    if len(text) <= max_chunk_chars:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chunk_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) > max_chunk_chars:
            for start in range(0, len(paragraph), max_chunk_chars):
                piece = paragraph[start : start + max_chunk_chars]
                if piece.strip():
                    chunks.append(piece)
            current = ""
        else:
            current = paragraph
    if current:
        chunks.append(current)
    return chunks


# -- import --------------------------------------------------------------------


def import_dump(
    store: WikiStore,
    dump_path: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Import a ``wiki_pages`` dump into the store incrementally.

    A row is skipped (``unchanged``) when the store already holds that page
    with the same ``updated_at`` and the same raw body; otherwise the page is
    upserted together with its parsed sections and wiki links. ``pages`` counts
    every usable dump row, ``chunks`` the sections written by this run, and
    ``progress(done, -1)`` fires every :data:`PROGRESS_INTERVAL` rows (the
    streaming parser cannot know the total row count upfront). Afterwards the
    ``dump_date`` (parsed from the dump filename, falling back to today) and
    ``imported_at`` (UTC now) meta keys are recorded.
    """

    dump_path = Path(dump_path)
    dump_date = _dump_date_from_path(dump_path)
    check_existing = store.has_data()
    seen = 0
    added = 0
    updated = 0
    unchanged = 0
    chunks_written = 0

    for row in parse_dump(dump_path):
        seen += 1
        title = str(row["title"])
        body = str(row["body"])
        new_updated_at = row["updated_at"]
        norm_title = normalize_title(title)
        existing = store.get_page(norm_title) if check_existing else None
        if (
            existing is not None
            and existing.get("updated_at") == new_updated_at
            and existing.get("body_md") == body
        ):
            unchanged += 1
            if progress is not None and seen % PROGRESS_INTERVAL == 0:
                progress(seen, -1)
            continue

        wiki_id = row["wiki_id"]
        sections = parse_dtext_sections(body)
        store.upsert_page(
            {
                "title": title,
                "display_title": title,
                "body_md": body,
                "wiki_id": wiki_id,
                "updated_at": new_updated_at,
                "url": (
                    f"https://e621.net/wiki_pages/{wiki_id}" if wiki_id is not None else None
                ),
                "sections": sections,
                "links": extract_wiki_links(body, page_title=title),
            }
        )
        chunks_written += len(sections)
        if existing is not None:
            updated += 1
        else:
            added += 1
        if progress is not None and seen % PROGRESS_INTERVAL == 0:
            progress(seen, -1)

    store.set_meta("dump_date", dump_date)
    store.set_meta("imported_at", utc_now())
    return {
        "pages": seen,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "chunks": chunks_written,
    }


def _dump_date_from_path(path: Path) -> str:
    """Extract the YYYY-MM-DD dump date from a filename, falling back to today."""

    match = _DUMP_ENTRY_RE.search(path.name)
    if match is not None:
        return match.group(1)
    return datetime.now(UTC).date().isoformat()


__all__ = [
    "DUMP_LIST_URL",
    "GZIP_MAGIC",
    "ImporterError",
    "PROGRESS_INTERVAL",
    "USER_AGENT",
    "download_dump",
    "extract_dump_entries",
    "extract_wiki_links",
    "import_dump",
    "latest_dump_html",
    "latest_dump_url",
    "parse_dtext_sections",
    "parse_dump",
    "strip_dtext",
]
