"""Offline tests for the tag wiki dump importer (no real network calls)."""

from __future__ import annotations

import csv
import contextlib
import gzip
import io
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from tagger2.tag_wiki import importer
from tagger2.tag_wiki.contracts import ERROR_WIKI_BUILD_FAILED, MAX_CHUNK_CHARS
from tagger2.tag_wiki.importer import (
    ImporterError,
    extract_dump_entries,
    extract_wiki_links,
    latest_dump_url,
    parse_dtext_sections,
    strip_dtext,
)
from tagger2.tag_wiki.wiki_store import WikiStore

LISTING_HTML = """
<html><head><title>Index of /db_export/</title></head>
<body>
<a href="../">../</a>
<a href="tags-2026-09-01.csv.gz">tags-2026-09-01.csv.gz</a>
<a href="wiki_pages-2026-08-01.csv.gz">wiki_pages-2026-08-01.csv.gz</a>
<a href="wiki_pages-2026-09-01.csv.gz">wiki_pages-2026-09-01.csv.gz</a>
<a href="wiki_pages-2026-09-01.csv.gz">duplicate mention of the same file</a>
<a href="post_versions-2026-09-01.csv.gz">post_versions-2026-09-01.csv.gz</a>
</body></html>
"""


def _gzip_csv(path: Path, header: list[str], rows: list[list[str | None]]) -> Path:
    """Write a tiny gzipped CSV dump the way the official export looks."""

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        handle.write(buffer.getvalue())
    return path


def _write_dump(
    path: Path, rows: list[tuple[int | None, str, str, str | None]]
) -> Path:
    """Write a wiki_pages dump with (wiki_id, title, body, updated_at) rows."""

    return _gzip_csv(
        path,
        ["id", "title", "body", "updated_at"],
        [
            [
                None if wiki_id is None else str(wiki_id),
                title,
                body,
                None if updated_at is None else updated_at,
            ]
            for wiki_id, title, body, updated_at in rows
        ],
    )


# -- dump listing ----------------------------------------------------------------


# The listing format e621 serves today: absolute CDN hrefs, undated dump name.
CURRENT_LISTING_HTML = """
<html><body>
<div class="cell" data-label="Name"><strong>wiki_pages</strong></div>
<a class="export-download-link" href="https://static1.e621.net/data/db_export/wiki_pages.csv.gz">download</a>
<a class="export-download-link" href="https://static1.e621.net/data/db_export/tags.csv.gz">download</a>
</body></html>
"""


def test_extract_dump_entries_newest_first():
    """wiki_pages entries are extracted once each and sorted newest first."""

    entries = extract_dump_entries(LISTING_HTML)
    assert entries == [
        ("2026-09-01", "https://e621.net/db_export/wiki_pages-2026-09-01.csv.gz"),
        ("2026-08-01", "https://e621.net/db_export/wiki_pages-2026-08-01.csv.gz"),
    ]
    assert extract_dump_entries("<html>no dumps here</html>") == []


def test_extract_dump_entries_supports_current_undated_listing():
    """Absolute CDN hrefs without a date are resolved and sorted last."""

    entries = extract_dump_entries(CURRENT_LISTING_HTML)
    assert entries == [
        ("", "https://static1.e621.net/data/db_export/wiki_pages.csv.gz"),
    ]
    assert latest_dump_url(CURRENT_LISTING_HTML) == (
        "https://static1.e621.net/data/db_export/wiki_pages.csv.gz"
    )
    # The undated dump is cached under today's UTC date so the daily refresh
    # comparison in the build pipeline keeps working.
    assert importer.dump_filename_for_url(
        "https://static1.e621.net/data/db_export/wiki_pages.csv.gz"
    ) == importer.dump_filename_for_url("wiki_pages.csv.gz")
    assert importer.dump_filename_for_url("wiki_pages.csv.gz").startswith("wiki_pages-2")
    assert importer.dump_filename_for_url(
        "https://e621.net/db_export/wiki_pages-2026-09-01.csv.gz"
    ) == "wiki_pages-2026-09-01.csv.gz"


def test_latest_dump_url_builds_absolute_url():
    """The newest entry becomes the full db_export URL; an empty listing raises."""

    assert (
        latest_dump_url(LISTING_HTML)
        == "https://e621.net/db_export/wiki_pages-2026-09-01.csv.gz"
    )
    with pytest.raises(ImporterError) as excinfo:
        latest_dump_url("<html>empty</html>")
    assert excinfo.value.code == ERROR_WIKI_BUILD_FAILED


def test_latest_dump_html_returns_text(monkeypatch: pytest.MonkeyPatch):
    """The listing is fetched with a User-Agent header from the fixed URL."""

    seen: dict[str, object] = {}

    class FakeResponse:
        text = "<html>listing</html>"

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return FakeResponse()

    monkeypatch.setattr(importer.httpx, "get", fake_get)
    assert importer.latest_dump_html() == "<html>listing</html>"
    assert seen["url"] == importer.DUMP_LIST_URL
    assert isinstance(seen["headers"], dict) and "User-Agent" in seen["headers"]


def test_latest_dump_html_retries_then_raises(monkeypatch: pytest.MonkeyPatch):
    """HTTP failures are retried before surfacing as ImporterError."""

    attempts: list[str] = []

    def fake_get(url: str, **kwargs: object) -> str:
        attempts.append(url)
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(importer.httpx, "get", fake_get)
    monkeypatch.setattr(importer.time, "sleep", lambda _seconds: None)
    with pytest.raises(ImporterError) as excinfo:
        importer.latest_dump_html(timeout=1)
    assert len(attempts) == 3
    assert attempts[0] == importer.DUMP_LIST_URL
    assert excinfo.value.code == ERROR_WIKI_BUILD_FAILED


# -- download --------------------------------------------------------------------


class _FakeStreamResponse:
    """Minimal stand-in for the streamed httpx response of download_dump."""

    def __init__(self, payload: bytes, headers: dict[str, str]) -> None:
        self._payload = payload
        # Real httpx headers are case-insensitive; mirror that behavior.
        self.headers = httpx.Headers(headers)

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self) -> Iterator[bytes]:
        yield self._payload


def _install_fake_stream(
    monkeypatch: pytest.MonkeyPatch, payload: bytes, headers: dict[str, str]
) -> list[dict[str, object]]:
    """Serve ``payload`` through a fake httpx.stream and record the requests."""

    requests: list[dict[str, object]] = []

    @contextlib.contextmanager
    def fake_stream(method: str, url: str, **kwargs: object) -> Iterator[_FakeStreamResponse]:
        requests.append({"method": method, "url": url, **kwargs})
        yield _FakeStreamResponse(payload, headers)

    monkeypatch.setattr(importer.httpx, "stream", fake_stream)
    return requests


def test_download_dump_writes_atomically_and_prunes_older(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The dump lands under its final name, replacing older dumps only."""

    payload = gzip.compress(b"fake dump contents")
    url = f"{importer.DUMP_LIST_URL}wiki_pages-2026-09-01.csv.gz"
    requests = _install_fake_stream(
        monkeypatch, payload, {"Content-Length": str(len(payload))}
    )
    older = tmp_path / "wiki_pages-2020-01-01.csv.gz"
    older.write_bytes(b"old dump")
    keeper = tmp_path / "notes.txt"
    keeper.write_bytes(b"not a dump")

    calls: list[tuple[int, int]] = []

    def progress(done: int, total: int) -> None:
        calls.append((done, total))

    result = importer.download_dump(url, tmp_path, progress=progress)

    assert result == tmp_path / "wiki_pages-2026-09-01.csv.gz"
    assert result.read_bytes() == payload
    assert requests == [
        {
            "method": "GET",
            "url": url,
            "headers": {"User-Agent": importer.USER_AGENT},
            "timeout": 600,
            "follow_redirects": True,
        }
    ]
    assert not (tmp_path / "wiki_pages-2026-09-01.csv.gz.part").exists()
    assert older.exists() is False
    assert keeper.exists() is True
    # Content-Length was known, so the final callback reports completion.
    assert calls and calls[-1] == (len(payload), len(payload))


def test_download_dump_rejects_non_gzip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A payload without the gzip magic is rejected and leaves no artifacts."""

    url = f"{importer.DUMP_LIST_URL}wiki_pages-2026-09-01.csv.gz"
    _install_fake_stream(monkeypatch, b"plain text, not gzip", {})
    with pytest.raises(ImporterError) as excinfo:
        importer.download_dump(url, tmp_path)
    assert excinfo.value.code == ERROR_WIKI_BUILD_FAILED
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("wiki_pages-*.csv.gz"))


# -- DText processing ------------------------------------------------------------


def test_extract_wiki_links_mixed_and_dedup():
    """Wiki and search links are normalized, deduplicated, order-preserving."""

    body = (
        "See [[Long Ears]] then [[long ears|big ears]] plus {{Canid}} and "
        "{{canid}} before [[Wolf]]."
    )
    assert extract_wiki_links(body) == ["long_ears", "canid", "wolf"]


def test_extract_wiki_links_drops_empty_and_self_references():
    """Empty targets and links to the page itself are removed."""

    body = "[[|no target]] and {{}} plus [[Long Ears|self]] and [[wolf]]"
    assert extract_wiki_links(body, page_title="Long Ears") == ["wolf"]
    assert extract_wiki_links("[[Long Ears]] only self") == ["long_ears"]
    assert extract_wiki_links("no links at all") == []


def test_strip_dtext_removes_markup_keeps_text():
    """Markup tags are dropped (code content included), inner text survives."""

    body = (
        "[b]bold[/b] and [i]italic[/i] with [u]under[/u] and [s]strike[/s]\n"
        "[code]int x = 1;[/code]\n"
        "[section=collapsed]collapsible body[/section]\n"
        "[quote]quoted words[/quote]\n"
        "[spoiler]hidden hint[/spoiler]\n"
        "footnote [1] stays\n"
    )
    text = strip_dtext(body)
    for kept in ("bold", "italic", "under", "strike", "collapsible body",
                 "quoted words", "hidden hint", "footnote [1] stays"):
        assert kept in text
    for gone in ("int x = 1;", "[code]", "[section", "[quote]", "[spoiler]", "[b]"):
        assert gone not in text
    assert not text.startswith("\n")
    assert "\n\n\n" not in text
    assert strip_dtext("") == ""
    # Wiki and search links render as their readable target, label, or tag name.
    assert strip_dtext("See [[Long Ears|big ears]] and [[rabbit]] with {{canid}}.") == (
        "See big ears and rabbit with canid."
    )


def test_parse_dtext_sections_splits_headings():
    """Heading markers split sections; pre-heading text uses an empty heading."""

    body = (
        "Intro paragraph before any heading.\n\n"
        "h2. [b]Usage[/b]\n"
        "Use it for rabbits.\n\n"
        "h3. History\n"
        "Added in 2020 with [b]bold[/b] markup.\n"
    )
    sections = parse_dtext_sections(body)
    assert sections == [
        {"heading": "", "text": "Intro paragraph before any heading."},
        {"heading": "Usage", "text": "Use it for rabbits."},
        {"heading": "History", "text": "Added in 2020 with bold markup."},
    ]


def test_parse_dtext_sections_drops_empty_and_honors_default_cap():
    """Empty-text sections vanish and the default cap comes from contracts."""

    body = "h2. Empty\nh3. Real\nText under the third heading."
    sections = parse_dtext_sections(body)
    assert sections == [{"heading": "Real", "text": "Text under the third heading."}]
    assert parse_dtext_sections("plain text", max_chunk_chars=MAX_CHUNK_CHARS, min_chunk_chars=0) == [
        {"heading": "", "text": "plain text"}
    ]
    assert parse_dtext_sections("") == []


def test_parse_dtext_sections_drops_junk_fragments():
    """Tiny/no-content fragments are filtered by default; content survives."""

    body = (
        "h2. Junk one\n"
        "。\n"
        "h2. Junk two\n"
        "category:character\n"
        "h2. Junk three\n"
        "___\n/   \\\n"
        "h2. Real\n"
        "Wolves hunt in packs and raise their young together."
    )
    sections = parse_dtext_sections(body)
    assert sections == [
        {"heading": "Real", "text": "Wolves hunt in packs and raise their young together."}
    ]
    # The filter is opt-out for tests and unusual pipelines.
    assert len(parse_dtext_sections(body, min_chunk_chars=0)) == 4


def test_parse_dtext_sections_drops_link_soup():
    """URL lists, bare page URLs and thumb placeholders never become chunks;
    prose that merely mentions a link survives."""

    body = (
        "h2. Links\n"
        '* "FurAffinity":https://www.furaffinity.net/user/stub\n'
        '* "Twitter":https://x.com/stub\n'
        '* "Bluesky":https://bsky.app/profile/stub\n'
        "h2. Bare\n"
        "https://www.pixiv.net/member_illust.php\n"
        "h2. Thumbs\n"
        "thumb #5481584 thumb #5521572 thumb #5529707\n"
        "h2. Real\n"
        "References the official https://example.com/thread discussion."
    )
    sections = parse_dtext_sections(body)
    assert sections == [
        {"heading": "Real", "text": "References the official https://example.com/thread discussion."}
    ]


def test_parse_dtext_sections_splits_long_sections_by_paragraph():
    """Sections over the cap split at \\n\\n boundaries under the same heading."""

    paragraphs = "\n\n".join(f"para {index} {'x' * 30}" for index in range(5))
    body = f"h2. Details\n{paragraphs}"
    sections = parse_dtext_sections(body, max_chunk_chars=50, min_chunk_chars=0)
    assert len(sections) > 1
    assert all(section["heading"] == "Details" for section in sections)
    assert all(len(section["text"]) <= 50 for section in sections)
    assert "\n\n".join(section["text"] for section in sections) == strip_dtext(paragraphs)


# -- dump parsing ----------------------------------------------------------------


def test_parse_dump_tolerates_column_order_and_nulls(tmp_path: Path):
    """Any column order, extra columns, \\N and empty cells are all handled."""

    dump = _gzip_csv(
        tmp_path / "wiki_pages-2026-09-01.csv.gz",
        ["body", "title", "updated_at", "extra", "id"],
        [
            ["Body A", "Tag A", "2026-01-01T00:00:00Z", "extra1", "123"],
            ["Body B", "Tag B", "\\N", "extra2", ""],
            ["Body C", "Tag C", "\\N", "extra3", "not-an-int"],
            ["", "No body", "2026-01-02T00:00:00Z", "extra4", "7"],
            ["No title body", "", "2026-01-03T00:00:00Z", "extra5", "8"],
        ],
    )
    rows = list(importer.parse_dump(dump))
    assert rows == [
        {
            "title": "Tag A",
            "body": "Body A",
            "wiki_id": 123,
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {"title": "Tag B", "body": "Body B", "wiki_id": None, "updated_at": None},
        {"title": "Tag C", "body": "Body C", "wiki_id": None, "updated_at": None},
    ]


def test_parse_dump_reads_updated_on_fallback(tmp_path: Path):
    """The updated_on column is used when updated_at is NULL."""

    dump = _gzip_csv(
        tmp_path / "wiki_pages-2026-09-02.csv.gz",
        ["title", "body", "updated_at", "updated_on"],
        [
            ["Tag D", "Body D", "\\N", "2026-05-05T00:00:00Z"],
            ["Tag E", "Body E", "2026-06-06T00:00:00Z", "\\N"],
        ],
    )
    rows = list(importer.parse_dump(dump))
    assert rows[0]["updated_at"] == "2026-05-05T00:00:00Z"
    assert rows[1]["updated_at"] == "2026-06-06T00:00:00Z"


def test_parse_dump_requires_title_and_body_columns(tmp_path: Path):
    """A dump without the title/body columns is a loud ImporterError."""

    dump = _gzip_csv(tmp_path / "broken.csv.gz", ["title", "other"], [["A", "B"]])
    with pytest.raises(ImporterError) as excinfo:
        list(importer.parse_dump(dump))
    assert excinfo.value.code == ERROR_WIKI_BUILD_FAILED


# -- import ----------------------------------------------------------------------


BODY_LONG_EARS = (
    "Rabbits have long ears.\n\n"
    "h2. Usage\n"
    "Use for [[rabbit]] and {{canid}} artwork, see also [[Long Ears|self]]."
)
BODY_WOLF = "Wolves are [[canid]]s."


def test_import_dump_incremental(tmp_path: Path):
    """A second identical import is all unchanged; edited bodies are updated."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    dump = _write_dump(
        tmp_path / "wiki_pages-2026-09-01.csv.gz",
        [
            (101, "Long Ears", BODY_LONG_EARS, "2026-01-01T00:00:00Z"),
            (None, "Wolf", BODY_WOLF, None),
        ],
    )

    stats = importer.import_dump(store, dump)
    assert stats == {"pages": 2, "added": 2, "updated": 0, "unchanged": 0, "chunks": 3}
    assert store.chunk_count() == 3
    assert store.get_meta("dump_date") == "2026-09-01"
    assert store.get_meta("imported_at") is not None

    page = store.get_page("long_ears")
    assert page is not None
    assert page["title"] == "long_ears"
    assert page["display_title"] == "Long Ears"
    assert page["wiki_id"] == 101
    assert page["updated_at"] == "2026-01-01T00:00:00Z"
    assert page["url"] == "https://e621.net/wiki_pages/101"
    assert page["body_md"] == BODY_LONG_EARS
    assert page["sections"] == [
        {"heading": "", "text": "Rabbits have long ears."},
        {"heading": "Usage", "text": "Use for rabbit and canid artwork, see also self."},
    ]
    assert page["related_tags"] == ["canid", "rabbit"]

    wolf = store.get_page("wolf")
    assert wolf is not None
    assert wolf["wiki_id"] is None
    assert wolf["updated_at"] is None
    assert wolf["url"] is None
    assert wolf["related_tags"] == ["canid"]

    # Second run over the same dump: everything unchanged, nothing rewritten.
    stats = importer.import_dump(store, dump)
    assert stats == {"pages": 2, "added": 0, "updated": 0, "unchanged": 2, "chunks": 0}
    assert store.chunk_count() == 3

    # Same titles, one edited body: that row is updated, the other unchanged.
    dump_modified = _write_dump(
        tmp_path / "wiki_pages-2026-09-02.csv.gz",
        [
            (101, "Long Ears", BODY_LONG_EARS, "2026-01-01T00:00:00Z"),
            (102, "Wolf", "Wolves hunt in packs.", "2026-02-02T00:00:00Z"),
        ],
    )
    stats = importer.import_dump(store, dump_modified)
    assert stats == {"pages": 2, "added": 0, "updated": 1, "unchanged": 1, "chunks": 1}
    assert store.chunk_count() == 3
    wolf = store.get_page("wolf")
    assert wolf is not None
    assert wolf["body_md"] == "Wolves hunt in packs."
    assert wolf["url"] == "https://e621.net/wiki_pages/102"
    assert store.get_meta("dump_date") == "2026-09-02"


def test_import_dump_reports_progress(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Progress fires periodically with (done, -1) because the total is unknown."""

    monkeypatch.setattr(importer, "PROGRESS_INTERVAL", 1)
    store = WikiStore(tmp_path / "wiki.sqlite3")
    dump = _write_dump(
        tmp_path / "wiki_pages-2026-09-01.csv.gz",
        [
            (1, "Long Ears", BODY_LONG_EARS, None),
            (2, "Wolf", BODY_WOLF, None),
        ],
    )
    calls: list[tuple[int, int]] = []

    def progress(done: int, total: int) -> None:
        calls.append((done, total))

    importer.import_dump(store, dump, progress=progress)
    assert calls == [(1, -1), (2, -1)]


def test_import_dump_falls_back_to_today_for_dump_date(tmp_path: Path):
    """A dump file without a date in its name records today's UTC date."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    dump = _write_dump(tmp_path / "unnamed-dump.gz", [(1, "Wolf", BODY_WOLF, None)])
    importer.import_dump(store, dump)
    assert store.get_meta("dump_date") == datetime.now(UTC).date().isoformat()


def test_progress_callable_is_optional_everywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """download_dump and import_dump both work without a progress callback."""

    payload = gzip.compress(b"unused")
    url = f"{importer.DUMP_LIST_URL}wiki_pages-2026-09-01.csv.gz"
    _install_fake_stream(monkeypatch, payload, {})
    downloaded = importer.download_dump(url, tmp_path)
    assert downloaded.exists()

    store = WikiStore(tmp_path / "wiki.sqlite3")
    dump = _write_dump(downloaded, [(3, "Wolf", BODY_WOLF, None)])
    assert importer.import_dump(store, dump)["added"] == 1
