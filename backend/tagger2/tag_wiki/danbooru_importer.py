"""Fetcher and importer for the danbooru ``wiki_pages`` JSON API.

Danbooru does not publish bulk database exports like e621's ``db_export``;
the wiki corpus is pulled through the paginated JSON API instead
(``GET https://danbooru.donmai.us/wiki_pages.json``, up to 1000 pages per
request). The module is deliberately conservative towards the upstream
service:

- requests are paced (``DEFAULT_MIN_INTERVAL`` seconds apart), ``429``
  responses honor ``Retry-After``, and transient failures retry with backoff
  while permanent client errors fail immediately;
- paging never relies on result ordering (the API does not reliably honor
  ``search[order]`` once a ``search[updated_at]`` filter is present): the
  full walk advances a ``page=b<cursor>`` id boundary (pages with
  ``id < cursor``) and takes the smallest id of each batch;
- every fetched batch lands in a raw JSONL cache plus a resumable state file
  under ``data/tag_wiki/danbooru/``, so an interrupted walk continues where
  it stopped and re-imports never touch the network again.

Import reuses the e621 DText pipeline (section parsing, chunk filters, wiki
links) and writes into a dedicated :class:`WikiStore` database
(``tag_wiki_danbooru.sqlite3``). Pages marked ``is_deleted`` upstream are
skipped, and purged from the store when they had been imported before.
Embeddings are out of scope here; the vector index is built separately.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..workflow.contracts import utc_now
from .importer import extract_wiki_links, parse_dtext_sections
from .wiki_store import WikiStore, normalize_title

logger = logging.getLogger("tagger2.tag_wiki.danbooru_importer")

WIKI_PAGES_URL = "https://danbooru.donmai.us/wiki_pages.json"

# Danbooru also expects a descriptive User-Agent (application/purpose style).
USER_AGENT = "Tagger2-TagWiki/1.6 (danbooru wiki_pages API importer)"

# The API caps ``limit`` at 1000 records per request.
PAGE_LIMIT = 1000

# Payload trim via ``only=``: full records are markedly slower to serve.
WIKI_FIELDS = "id,title,body,updated_at,is_deleted"

# Polite pacing: at most one request per interval. Danbooru tolerates short
# bursts, but the corpus is large enough that sustained pacing costs little
# extra wall time and keeps us well clear of throttling.
DEFAULT_MIN_INTERVAL = 2.0

# Attempts (with backoff) for transient failures such as 5xx and bad JSON.
_HTTP_ATTEMPTS = 4

# Upper bound for a server-imposed ``Retry-After`` sleep.
_MAX_RETRY_AFTER = 120.0

# ``page=b<cursor>`` returns pages with ``id < cursor``; the wiki id space
# currently tops out far below this initial boundary.
INITIAL_CURSOR = 2_000_000

# An ``updated_at`` window narrower than this that still fills a whole page
# is refused instead of split further.
_MIN_WINDOW_SECONDS = 3600.0

# Pages buffered per store transaction during import.
_IMPORT_BATCH = 500


class DanbooruWikiFetchError(RuntimeError):
    """Raised when the danbooru wiki API cannot be queried."""


class DanbooruWikiClient:
    """Paced, retried HTTP client for the danbooru wiki API.

    ``sleep`` and ``monotonic`` are injectable so tests can verify pacing and
    backoff without real delays. ``requests`` counts successful fetches only.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        attempts: int = _HTTP_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_interval = max(0.0, float(min_interval))
        self._attempts = max(1, int(attempts))
        self._sleep = sleep
        self._monotonic = monotonic
        self._next_allowed = 0.0
        self.requests = 0

    def fetch_page(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Issue one paced, retried GET and return the JSON record list."""

        last_error: Exception | None = None
        for attempt in range(self._attempts):
            delay = self._next_allowed - self._monotonic()
            if delay > 0:
                self._sleep(delay)
            try:
                response = self._client.get(
                    WIKI_PAGES_URL,
                    params=dict(params),
                    headers={"User-Agent": USER_AGENT},
                )
            except httpx.HTTPError as exc:
                last_error = exc
                self._sleep(min(60.0, 2.0**attempt))
                continue
            if response.status_code == 429:
                raw = response.headers.get("Retry-After", "")
                try:
                    wait = float(raw) if raw else 2.0**attempt
                except ValueError:
                    wait = 2.0**attempt
                self._sleep(min(_MAX_RETRY_AFTER, max(1.0, wait)))
                last_error = DanbooruWikiFetchError("danbooru wiki API rate limited (HTTP 429)")
                continue
            if 400 <= response.status_code < 500:
                raise DanbooruWikiFetchError(
                    f"danbooru wiki API returned HTTP {response.status_code}"
                )
            try:
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                self._sleep(min(60.0, 2.0**attempt))
                continue
            self.requests += 1
            self._next_allowed = self._monotonic() + self._min_interval
            if not isinstance(payload, list):
                raise DanbooruWikiFetchError("danbooru wiki API returned a non-list payload")
            return [item for item in payload if isinstance(item, dict)]
        raise DanbooruWikiFetchError(
            f"danbooru wiki API did not answer after {self._attempts} attempts: {last_error}"
        ) from last_error


# -- fetch queries -------------------------------------------------------------


def full_walk_params(cursor: int, *, page_limit: int = PAGE_LIMIT) -> dict[str, str]:
    """Query params for one full-corpus batch strictly below ``cursor``."""

    return {
        "limit": str(page_limit),
        "page": f"b{cursor}",
        "search[is_deleted]": "false",
        "only": WIKI_FIELDS,
    }


def updated_window_params(start: str, end: str, *, page_limit: int = PAGE_LIMIT) -> dict[str, str]:
    """Query params for pages updated within ``[start, end]`` (inclusive, UTC)."""

    return {
        "limit": str(page_limit),
        "search[updated_at]": f"{start}..{end}",
        "only": WIKI_FIELDS,
    }


def iter_full_batches(
    client: DanbooruWikiClient,
    *,
    page_limit: int = PAGE_LIMIT,
    initial_cursor: int = INITIAL_CURSOR,
) -> Iterator[list[dict[str, Any]]]:
    """Yield every non-deleted wiki page in ``page_limit``-sized batches.

    Walks the id space downwards from ``initial_cursor``; the next cursor is
    the smallest id of the previous batch, so server-side result ordering is
    irrelevant. Stops on the first empty batch.
    """

    cursor = int(initial_cursor)
    while True:
        batch = client.fetch_page(full_walk_params(cursor, page_limit=page_limit))
        if not batch:
            return
        yield batch
        ids = [int(page["id"]) for page in batch if page.get("id") is not None]
        if not ids:
            raise DanbooruWikiFetchError("danbooru wiki batch came back without page ids")
        next_cursor = min(ids)
        if next_cursor >= cursor:
            raise DanbooruWikiFetchError("danbooru wiki cursor pagination did not advance")
        cursor = next_cursor


def iter_updated_batches(
    client: DanbooruWikiClient,
    *,
    since: str,
    until: str | None = None,
    page_limit: int = PAGE_LIMIT,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages whose ``updated_at`` falls in ``[since, until]`` (UTC).

    ``since``/``until`` accept ``YYYY-MM-DD`` or full timestamps. ``until``
    defaults to tomorrow so the window always covers "everything since".
    A window whose request fills the whole page limit is split at the datetime
    midpoint and both halves are fetched separately, so a busy window can
    never be silently truncated; windows narrower than one hour that still
    fill a page raise :class:`DanbooruWikiFetchError`. Pages updated exactly
    at a split boundary may arrive twice; import deduplicates them.
    """

    if until is None:
        until = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    windows: deque[tuple[datetime, datetime]] = deque([(parse_bound(since), parse_bound(until))])
    while windows:
        start, end = windows.popleft()
        if end <= start:
            continue
        params = updated_window_params(_format_bound(start), _format_bound(end), page_limit=page_limit)
        batch = client.fetch_page(params)
        if len(batch) == page_limit:
            if (end - start).total_seconds() <= _MIN_WINDOW_SECONDS:
                raise DanbooruWikiFetchError(
                    "danbooru updated_at window "
                    f"{_format_bound(start)}..{_format_bound(end)} still fills a full page;"
                    " narrow the window (pass --since) and retry"
                )
            middle = start + (end - start) / 2
            windows.append((start, middle))
            windows.append((middle, end))
            continue
        if batch:
            yield batch


def parse_bound(text: str) -> datetime:
    """Parse a UTC window bound; naive values are treated as UTC."""

    value = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _format_bound(value: datetime) -> str:
    """Render a window bound the way the API accepts it."""

    if value.hour == 0 and value.minute == 0 and value.second == 0:
        return value.date().isoformat()
    return value.strftime("%Y-%m-%dT%H:%M:%S")


# -- import --------------------------------------------------------------------


def import_pages(
    store: WikiStore,
    pages: Iterable[Mapping[str, Any]],
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Import fetched danbooru pages into ``store`` incrementally.

    Mirrors :func:`tagger2.tag_wiki.importer.import_dump`: a page whose stored
    ``updated_at`` and raw body are unchanged is skipped entirely, otherwise
    it is upserted together with its parsed DText sections and wiki links.
    Pages marked ``is_deleted`` never enter the store and are purged when they
    had been imported earlier; rows without a usable title or body are
    skipped. Pages are buffered in :data:`_IMPORT_BATCH`-sized store
    transactions. ``progress(done, -1)`` fires once per buffered batch (the
    total row count is unknown while streaming).
    """

    check_existing = store.has_data()
    seen = added = updated = unchanged = deleted = skipped = chunks_written = 0

    def process(raws: list[Mapping[str, Any]]) -> None:
        nonlocal added, updated, unchanged, deleted, skipped, chunks_written
        candidates: list[tuple[str, str, str, Mapping[str, Any]]] = []
        for raw in raws:
            title = str(raw.get("title") or "").strip()
            body = str(raw.get("body") or "")
            norm_title = normalize_title(title) if title else ""
            if bool(raw.get("is_deleted")):
                if check_existing and norm_title and store.get_page(norm_title) is not None:
                    store.delete_page(norm_title)
                    deleted += 1
                continue
            if not norm_title or not body.strip():
                skipped += 1
                continue
            candidates.append((norm_title, title, body, raw))
        if not candidates:
            return

        snapshot = (
            store.get_pages_snapshot([candidate[0] for candidate in candidates])
            if check_existing
            else {}
        )
        upserts: list[dict[str, Any]] = []
        for norm_title, title, body, raw in candidates:
            new_updated_at = (
                str(raw.get("updated_at")) if raw.get("updated_at") is not None else None
            )
            existing = snapshot.get(norm_title)
            if (
                existing is not None
                and existing.get("updated_at") == new_updated_at
                and existing.get("body_md") == body
            ):
                unchanged += 1
                continue
            sections = parse_dtext_sections(body)
            wiki_id: int | None
            try:
                wiki_id = int(raw["id"]) if raw.get("id") is not None else None
            except (TypeError, ValueError):
                wiki_id = None
            upserts.append(
                {
                    "title": title,
                    "display_title": title,
                    "body_md": body,
                    "wiki_id": wiki_id,
                    "updated_at": new_updated_at,
                    "url": (
                        f"https://danbooru.donmai.us/wiki_pages/{wiki_id}"
                        if wiki_id is not None
                        else None
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
        if upserts:
            store.upsert_pages(upserts)

    buffer: list[Mapping[str, Any]] = []
    for raw in pages:
        seen += 1
        buffer.append(raw)
        if len(buffer) >= _IMPORT_BATCH:
            process(buffer)
            buffer = []
            if progress is not None:
                progress(seen, -1)
    if buffer:
        process(buffer)
        if progress is not None:
            progress(seen, -1)

    store.set_meta("source", "danbooru-api")
    store.set_meta("imported_at", utc_now())
    return {
        "pages": seen,
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "deleted": deleted,
        "skipped": skipped,
        "chunks": chunks_written,
    }


# -- local cache & state -------------------------------------------------------


def default_danbooru_dir() -> Path:
    """Directory for the raw JSONL cache and fetch state under the data dir."""

    from ..config import get_settings

    settings = get_settings()
    if settings.data_dir is None:
        raise RuntimeError("application data_dir is not configured")
    return settings.data_dir / "tag_wiki" / "danbooru"


def default_danbooru_store_path() -> Path:
    """Dedicated :class:`WikiStore` database path for the danbooru mirror."""

    from ..config import get_settings

    settings = get_settings()
    if settings.data_dir is None:
        raise RuntimeError("application data_dir is not configured")
    return settings.data_dir / "tag_wiki" / "tag_wiki_danbooru.sqlite3"


def append_pages_jsonl(path: Path, pages: Iterable[Mapping[str, Any]]) -> int:
    """Append one JSON object per page to the raw cache; return line count."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for page in pages:
            handle.write(json.dumps(dict(page), ensure_ascii=False) + "\n")
            written += 1
    return written


def iter_pages_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield pages from the raw JSONL cache, skipping malformed lines."""

    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                page = json.loads(text)
            except ValueError:
                logger.warning("skipping malformed danbooru JSONL line %d", line_number)
                continue
            if isinstance(page, dict):
                yield page


def load_state(path: Path) -> dict[str, Any]:
    """Read the resumable fetch state; missing or corrupt files mean fresh."""

    path = Path(path)
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("could not read danbooru fetch state %s; starting fresh", path)
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    """Atomically persist the fetch state."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(dict(state), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


__all__ = [
    "DEFAULT_MIN_INTERVAL",
    "INITIAL_CURSOR",
    "PAGE_LIMIT",
    "USER_AGENT",
    "WIKI_FIELDS",
    "WIKI_PAGES_URL",
    "DanbooruWikiClient",
    "DanbooruWikiFetchError",
    "append_pages_jsonl",
    "default_danbooru_dir",
    "default_danbooru_store_path",
    "full_walk_params",
    "import_pages",
    "iter_full_batches",
    "iter_pages_jsonl",
    "iter_updated_batches",
    "load_state",
    "parse_bound",
    "save_state",
    "updated_window_params",
]
