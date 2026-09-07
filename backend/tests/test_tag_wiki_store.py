"""Tests for the tag wiki SQLite store (pages, chunks, embeddings, search)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tagger2.tag_wiki.wiki_store import WikiStore, is_link_soup, normalize_title


def _page(title: str, **overrides: object) -> dict[str, object]:
    """A minimal valid page mapping with overridable fields."""

    page: dict[str, object] = {
        "title": title,
        "display_title": title,
        "body_md": f"body of {title}",
        "wiki_id": None,
        "updated_at": None,
        "url": None,
        "sections": [{"heading": "", "text": f"text of {title}"}],
        "links": [],
    }
    page.update(overrides)
    return page


def test_delete_chunks_for_pages_removes_and_invalidates(tmp_path: Path):
    """Deleting a page's chunks drops its rows and keeps the page itself."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.upsert_page(_page("hug", sections=[{"heading": "", "text": "a hug between two characters"}]))
    store.upsert_page(_page("some_artist", sections=[{"heading": "", "text": "link list for the artist page"}]))
    assert store.chunk_count() == 2

    removed = store.delete_chunks_for_pages(["some_artist"])
    assert removed == 1
    assert store.chunk_count() == 1
    assert store.get_page("some_artist") is not None  # the page itself stays
    assert store.get_page("some_artist")["sections"] == []
    assert store.get_page("hug")["sections"] != []
    store.close()


def test_delete_link_soup_chunks_drops_link_lists(tmp_path: Path):
    """Chunks that are nothing but links/placeholders go; prose chunks and pages stay."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    url_list = (
        '* "FurAffinity":https://www.furaffinity.net/user/stub\n'
        '* "Twitter":https://x.com/stub\n'
        '* "Bluesky":https://bsky.app/profile/stub'
    )
    bare_url = "https://www.pixiv.net/member_illust.php"
    thumbs = "thumb #5481584 thumb #5521572 thumb #5529707"
    two_links = "Links: https://one.example/x and https://two.example/y only."
    store.upsert_page(_page("stub_page", body_md=url_list, sections=[{"heading": "", "text": url_list}]))
    store.upsert_page(_page("bare_url_page", body_md=bare_url, sections=[{"heading": "", "text": bare_url}]))
    store.upsert_page(_page("thumbs_page", body_md=thumbs, sections=[{"heading": "", "text": thumbs}]))
    store.upsert_page(_page("prose_page", sections=[{"heading": "", "text": "a hug between two characters"}]))
    store.upsert_page(_page("few_links_page", sections=[{"heading": "", "text": two_links}]))

    removed = store.delete_link_soup_chunks()
    assert removed == 3
    assert store.get_page("stub_page") is not None  # the page itself stays
    assert store.get_page("stub_page")["sections"] == []
    assert store.get_page("bare_url_page")["sections"] == []
    assert store.get_page("thumbs_page")["sections"] == []
    assert store.get_page("prose_page")["sections"] != []
    assert store.get_page("few_links_page")["sections"] != []  # residual prose keeps it
    # Idempotent.
    assert store.delete_link_soup_chunks() == 0
    store.close()


def test_is_link_soup_classifies_shapes():
    """Unit coverage for the shared link-soup predicate."""

    assert is_link_soup("https://www.pixiv.net/member_illust.php")
    assert is_link_soup('* "FA":https://a.example/1\n* "TW":https://b.example/2\n* "BS":https://c.example/3')
    assert is_link_soup("thumb #5481584 thumb #5521572")
    assert is_link_soup('"Deviantart":http://dementra369.deviantart.com/ "Tumblr":https://rezident369.tumblr.com/')
    # Prose that merely mentions links stays.
    assert not is_link_soup("A character hugs another character from behind them, torsos aligned.")
    assert not is_link_soup("See the official thread at https://example.com/thread for details.")
    assert not is_link_soup("Links: https://one.example/x and https://two.example/y only.")


def test_batch_summaries_and_missing_limit(tmp_path: Path):
    """get_summaries_by_titles batches and normalizes; missing_summary_titles
    honors the early-exit limit."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.upsert_page(_page("hug"))
    store.upsert_page(_page("kiss"))
    store.upsert_page(_page("rare"))
    store.upsert_summary("hug", {"meaning": "拥抱", "tags": ["couple"]})
    store.upsert_summary("KISS", {"meaning": "亲吻", "tags": []})

    got = store.get_summaries_by_titles(["Hug", "KISS", "rare", "missing_page"])
    assert set(got) == {"hug", "kiss"}
    assert got["hug"]["meaning"] == "拥抱"
    assert got["kiss"]["meaning"] == "亲吻"
    assert store.get_summaries_by_titles([]) == {}

    missing = store.missing_summary_titles(["Hug", "KISS", "rare"], limit=1)
    assert missing == ["rare"]
    assert store.missing_summary_titles(["hug"], limit=2) == []
    assert store.missing_summary_titles(["hug", "rare"], limit=0) == []
    store.close()


def test_normalize_title_casefolds_and_underscores():
    """Whitespace collapses to single underscores and case is folded."""

    assert normalize_title("  Long   EARS ") == "long_ears"
    assert normalize_title("Hatsune Miku") == "hatsune_miku"


def test_upsert_and_get_page_roundtrip(tmp_path: Path):
    """A page round-trips with normalized title, display title, chunks and links."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    assert store.has_data() is False

    returned = store.upsert_page(
        _page(
            "Long Ears",
            display_title="Long ears",
            wiki_id=4242,
            updated_at="2026-01-02T03:04:05Z",
            url="https://e621.net/wiki_pages/4242",
            sections=[
                {"heading": "Meaning", "text": "Ears that are long."},
                {"heading": "Usage", "text": "Tag for rabbits."},
            ],
            links=["Long Ears", "canid", "Canid", "  "],
        )
    )
    assert returned == "long_ears"
    assert store.has_data() is True

    page = store.get_page("Long Ears")
    assert page is not None
    assert page["title"] == "long_ears"
    assert page["display_title"] == "Long ears"
    assert page["body_md"] == "body of Long Ears"
    assert page["wiki_id"] == 4242
    assert page["updated_at"] == "2026-01-02T03:04:05Z"
    assert page["url"] == "https://e621.net/wiki_pages/4242"
    # Sections are stored as chunks in position order, empty links dropped and
    # self references plus duplicates removed.
    assert page["sections"] == [
        {"heading": "Meaning", "text": "Ears that are long."},
        {"heading": "Usage", "text": "Tag for rabbits."},
    ]
    assert page["related_tags"] == ["canid"]

    # Unknown pages return None instead of raising.
    assert store.get_page("totally_missing_page") is None
    store.close()


def test_upsert_page_rewrites_chunks(tmp_path: Path):
    """Re-upserting a page replaces its chunks wholesale."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.upsert_page(_page("wolf", sections=[{"heading": "", "text": "old text"}]))
    store.upsert_page(_page("wolf", sections=[{"heading": "", "text": "new text"}]))
    assert store.chunk_count() == 1
    assert store.get_page("wolf")["sections"] == [{"heading": "", "text": "new text"}]
    store.close()


def test_summary_lifecycle(tmp_path: Path):
    """get_page reports summary=None until upsert_summary fills it in."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.upsert_page(_page("long ears"))

    assert store.get_summary("long ears") is None
    page = store.get_page("long ears")
    assert page is not None
    assert page["summary"] is None

    store.upsert_summary(
        "Long Ears",
        {
            "meaning": "长耳朵",
            "usage": "用于兔子类标签",
            "pairing": "常与 rabbit 一起使用",
            "notes": "",
            "tags": ["rabbit", "canid"],
            "provider_id": "openai",
            "model": "gpt-test",
            "updated_at": "2026-02-03T04:05:06Z",
        },
    )

    summary = store.get_summary("long_ears")
    assert summary == {
        "meaning": "长耳朵",
        "usage": "用于兔子类标签",
        "pairing": "常与 rabbit 一起使用",
        "notes": "",
        "tags": ["rabbit", "canid"],
        "provider_id": "openai",
        "model": "gpt-test",
        "updated_at": "2026-02-03T04:05:06Z",
    }
    page = store.get_page("long ears")
    assert page is not None
    assert page["summary"] == summary
    store.close()


def test_page_meta_counts(tmp_path: Path):
    """page_meta aggregates page/chunk/embedding/summary counts and dump_date."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    assert store.page_meta() == {
        "exists": False,
        "pages": 0,
        "chunks": 0,
        "translated_pages": 0,
        "dump_date": None,
    }

    store.upsert_page(
        _page("wolf", sections=[{"heading": "a", "text": "one"}, {"heading": "b", "text": "two"}])
    )
    store.upsert_page(_page("fox", updated_at=None))
    store.upsert_summary("fox", {"meaning": "狐"})
    store.set_meta("dump_date", "2026-08-01")

    assert store.page_meta() == {
        "exists": True,
        "pages": 2,
        "chunks": 3,
        "translated_pages": 1,
        "dump_date": "2026-08-01",
    }
    assert store.page_count() == 2
    assert store.chunk_count() == 3
    assert store.iter_page_titles() == ["fox", "wolf"]
    store.close()


def test_missing_summary_titles(tmp_path: Path):
    """Only titles without a summary are returned (original spelling kept)."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.upsert_page(_page("long ears"))
    store.upsert_page(_page("wolf"))
    store.upsert_summary("Long Ears", {"meaning": "长耳朵"})

    missing = store.missing_summary_titles(["long ears", "Wolf", "fox"])
    assert missing == ["Wolf", "fox"]
    assert store.missing_summary_titles([]) == []
    store.close()


def test_get_and_set_meta(tmp_path: Path):
    """Meta values upsert by key; missing keys read back as None."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    assert store.get_meta("dump_date") is None
    store.set_meta("dump_date", "2026-08-01")
    store.set_meta("dump_date", "2026-09-01")
    assert store.get_meta("dump_date") == "2026-09-01"
    store.close()


def test_close_memory_store_then_operations_raise():
    """Closing a memory store drops its connection; later operations fail loudly.

    This asserts the store's real behavior: after close() the memory connection
    is gone, so the next connection() opens a fresh empty ``:memory:`` database
    and any SQL touching the schema raises sqlite3.OperationalError.
    """

    store = WikiStore(":memory:")
    store.upsert_page(_page("wolf"))
    assert store.page_count() == 1

    store.close()
    with pytest.raises(sqlite3.OperationalError):
        store.page_count()
    with pytest.raises(sqlite3.OperationalError):
        store.get_meta("dump_date")

    # Closing twice is safe (the connection handle is already cleared).
    store.close()


def test_upsert_pages_bulk_matches_single_upsert(tmp_path: Path):
    """The bulk path writes exactly the same rows as the per-page API."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    written = store.upsert_pages(
        [
            _page("hug", sections=[{"heading": "", "text": "two characters hugging"}], links=["kiss"]),
            _page("kiss", sections=[{"heading": "", "text": "one character kissing"}]),
        ]
    )
    assert written == ["hug", "kiss"]
    assert store.page_count() == 2
    assert store.chunk_count() == 2
    assert store.get_page("hug")["related_tags"] == ["kiss"]  # type: ignore[index]

    snapshot = store.get_pages_snapshot(["hug", "kiss", "missing_page"])
    assert set(snapshot) == {"hug", "kiss"}
    assert snapshot["hug"]["body_md"] == "body of hug"
    assert store.get_pages_snapshot([]) == {}
    store.close()


def test_delete_page_removes_page_chunks_links_and_summary(tmp_path: Path):
    """Deleting a page drops every dependent row and reports existence."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.upsert_page(_page("hug", sections=[{"heading": "", "text": "a hugging pose"}], links=["kiss"]))
    store.upsert_summary("hug", {"meaning": "拥抱"})
    assert store.page_count() == 1
    assert store.chunk_count() == 1

    assert store.delete_page("hug") is True
    assert store.get_page("hug") is None
    assert store.page_count() == 0
    assert store.chunk_count() == 0
    assert store.summary_count() == 0
    assert store.delete_page("hug") is False
    assert store.delete_page("") is False
    store.close()
