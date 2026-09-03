"""Tests for the danbooru wiki JSON-API fetcher and incremental importer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tagger2.tag_wiki import danbooru_importer as di
from tagger2.tag_wiki.wiki_store import WikiStore

# Bodies must survive the chunk filters (>= 16 chars, >= 3 word-like tokens).
BODY = "This page describes the tag with plenty of words to survive the chunk filters."


def _page(
    page_id: int,
    title: str,
    body: str = BODY,
    *,
    deleted: bool = False,
    updated: str = "2026-09-02T05:00:00-04:00",
) -> dict[str, Any]:
    return {
        "id": page_id,
        "title": title,
        "body": body,
        "updated_at": updated,
        "is_deleted": deleted,
    }


class _FakeClock:
    """Records sleep calls; monotonic stays constant so pacing is observable."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def monotonic(self) -> float:
        return 1000.0


def _make_client(
    handler,
    *,
    min_interval: float = 0.0,
    attempts: int = 4,
) -> tuple[di.DanbooruWikiClient, _FakeClock]:
    clock = _FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return (
        di.DanbooruWikiClient(
            http,
            min_interval=min_interval,
            attempts=attempts,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def _wiki_handler(corpus: list[dict[str, Any]], calls: list[dict[str, str]]):
    """Serve the wiki API the way danbooru behaves: id-cursor pages, date windows."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = {str(k): str(v) for k, v in request.url.params.items()}
        calls.append(params)
        limit = int(params.get("limit", "1000"))
        page_param = params.get("page", "")
        if page_param.startswith("b"):
            cursor = int(page_param[1:])
            selected = [
                page
                for page in corpus
                if page["id"] < cursor and not page.get("is_deleted")
            ]
            batch = sorted(selected, key=lambda page: page["id"], reverse=True)[:limit]
        else:
            start_text, _, end_text = params.get("search[updated_at]", "").partition("..")
            start = di.parse_bound(start_text) if start_text else datetime.min.replace(tzinfo=UTC)
            end = di.parse_bound(end_text) if end_text else datetime.max.replace(tzinfo=UTC)
            selected = [
                page
                for page in corpus
                if start <= di.parse_bound(page["updated_at"]) <= end
            ]
            batch = selected[:limit]
        fields = [field for field in params.get("only", "").split(",") if field]
        if fields:
            batch = [{key: page[key] for key in fields if key in page} for page in batch]
        return httpx.Response(200, json=batch)

    return handler


def test_full_walk_paginates_by_cursor_and_imports(tmp_path: Path):
    """The cursor walk covers every page below the initial boundary, once."""

    corpus = [_page(page_id, f"tag_{page_id}") for page_id in range(1, 206)]
    calls: list[dict[str, str]] = []
    client, _clock = _make_client(_wiki_handler(corpus, calls))

    pages = [
        page
        for batch in di.iter_full_batches(client, page_limit=100)
        for page in batch
    ]
    assert len(calls) == 4  # 205 pages / 100 per request, plus the empty terminating batch
    assert len(pages) == 205
    assert len({page["id"] for page in pages}) == 205

    store = WikiStore(tmp_path / "danbooru.sqlite3")
    stats = di.import_pages(store, pages)
    assert stats["added"] == 205
    assert stats["unchanged"] == 0
    assert stats["chunks"] >= 205
    assert store.page_count() == 205
    assert store.get_meta("source") == "danbooru-api"
    page = store.get_page("tag_12")
    assert page is not None
    assert page["url"] == "https://danbooru.donmai.us/wiki_pages/12"
    store.close()


def test_incremental_splits_saturated_windows(tmp_path: Path):
    """A window that fills a whole page is split until nothing is truncated."""

    corpus = [
        _page(1, "left_a", updated="2026-09-02T02:00:00-04:00"),
        _page(2, "left_b", updated="2026-09-02T05:00:00-04:00"),
        _page(3, "left_c", updated="2026-09-02T08:00:00-04:00"),
        _page(4, "right_a", updated="2026-09-07T03:00:00-04:00"),
        _page(5, "right_b", updated="2026-09-07T09:00:00-04:00"),
    ]
    calls: list[dict[str, str]] = []
    client, _clock = _make_client(_wiki_handler(corpus, calls))

    pages = [
        page
        for batch in di.iter_updated_batches(
            client, since="2026-09-01", until="2026-09-09", page_limit=2
        )
        for page in batch
    ]
    assert len({page["id"] for page in pages}) == 5
    windows = [call["search[updated_at]"] for call in calls]
    assert len(windows) >= 3  # the initial window was split at least twice
    assert all(window.count("..") == 1 for window in windows)


def test_client_paces_requests():
    """A second request waits for the configured minimum interval."""

    calls: list[dict[str, str]] = []
    client, clock = _make_client(
        _wiki_handler([_page(1, "tag")], calls), min_interval=2.0
    )
    client.fetch_page(di.full_walk_params(2_000_000))
    assert clock.sleeps == []
    client.fetch_page(di.full_walk_params(2_000_000))
    assert clock.sleeps == [2.0]
    assert client.requests == 2


def test_client_retries_429_with_retry_after():
    """HTTP 429 sleeps for Retry-After and the same request is retried."""

    state = {"calls": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"}, json={})
        return httpx.Response(200, json=[_page(1, "tag")])

    client, clock = _make_client(handler)
    batch = client.fetch_page(di.full_walk_params(2_000_000))
    assert [page["id"] for page in batch] == [1]
    assert clock.sleeps == [3.0]
    assert client.requests == 1


def test_client_fails_fast_on_permanent_client_error():
    """4xx (other than 429) raise immediately without retries."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    client, clock = _make_client(handler)
    with pytest.raises(di.DanbooruWikiFetchError, match="403"):
        client.fetch_page(di.full_walk_params(2_000_000))
    assert clock.sleeps == []


def test_client_retries_5xx_until_exhausted():
    """5xx responses back off exponentially, then raise."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    client, clock = _make_client(handler, attempts=3)
    with pytest.raises(di.DanbooruWikiFetchError, match="3 attempts"):
        client.fetch_page(di.full_walk_params(2_000_000))
    assert clock.sleeps == [1.0, 2.0, 4.0]
    assert client.requests == 0


def test_import_pages_is_incremental_and_purges_deleted(tmp_path: Path):
    """Unchanged pages skip, updated pages rewrite, deleted pages purge."""

    store = WikiStore(tmp_path / "danbooru.sqlite3")
    first = [
        _page(1, "hug", updated="2026-09-01T00:00:00-04:00"),
        _page(2, "kiss", updated="2026-09-01T00:00:00-04:00"),
        _page(3, "gone", updated="2026-09-01T00:00:00-04:00"),
    ]
    assert di.import_pages(store, first)["added"] == 3

    # Same updated_at and body: everything is skipped without rewriting chunks.
    again = di.import_pages(store, first)
    assert again["unchanged"] == 3
    assert again["chunks"] == 0

    # One body changed, one stayed; a deleted page purges; junk is skipped.
    third = [
        _page(1, "hug", body="Completely different prose that still passes the chunk filters.", updated="2026-09-02T00:00:00-04:00"),
        _page(2, "kiss", updated="2026-09-01T00:00:00-04:00"),
        _page(3, "gone", deleted=True),
        _page(4, "junk", body=""),
    ]
    stats = di.import_pages(store, third)
    assert stats["updated"] == 1
    assert stats["unchanged"] == 1
    assert stats["deleted"] == 1
    assert stats["skipped"] == 1
    assert store.page_count() == 2
    assert store.get_page("gone") is None
    page = store.get_page("hug")
    assert page is not None
    assert page["updated_at"] == "2026-09-02T00:00:00-04:00"
    store.close()


def test_jsonl_cache_roundtrip_and_state(tmp_path: Path):
    """The raw cache tolerates malformed lines; state files roundtrip."""

    jsonl = tmp_path / "wiki_pages.jsonl"
    assert di.append_pages_jsonl(jsonl, [_page(1, "tag"), _page(2, "other", deleted=True)]) == 2
    with jsonl.open("a", encoding="utf-8") as handle:
        handle.write("{not json}\n")
    pages = list(di.iter_pages_jsonl(jsonl))
    assert [page["id"] for page in pages] == [1, 2]

    state_path = tmp_path / "state.json"
    assert di.load_state(state_path) == {}
    di.save_state(state_path, {"full_walk_done": True, "full_cursor": None})
    assert di.load_state(state_path) == {"full_walk_done": True, "full_cursor": None}

    # A corrupt state file falls back to a fresh start instead of crashing.
    state_path.write_text("{broken", encoding="utf-8")
    assert di.load_state(state_path) == {}
