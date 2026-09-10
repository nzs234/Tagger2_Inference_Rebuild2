"""Storage-level tests for the tag manager: WAL mode and chunk atomicity.

These tests deliberately open a real file-backed database (never ``:memory:``
or a mock connection) so they exercise the exact pragmas and transaction
handling a scan pays for in production.
"""

import contextlib
import sqlite3
from pathlib import Path

import pytest

from tagger2.tag_manager.storage import TagManagerStore


def _session_entry(session_id: str = "sess-1") -> dict:
    return {
        "id": session_id,
        "name": "demo",
        "root_id": "root-1",
        "relative_path": "dataset",
        "profile": "e621",
        "recursive": True,
    }


def _scan_row(relative: str, tags: list[tuple[str, str]]) -> dict:
    return {
        "relative_path": relative,
        "file_name": Path(relative).name,
        "image_format": "png",
        "sidecar_kind": "tag_txt",
        "sidecar_path": str(Path(relative).with_suffix(".txt")),
        "mtime": 100.0,
        "sidecar_mtime": 100.0,
        "width": 8,
        "height": 8,
        "tag_count": len(tags),
        "_tags": tags,
    }


def test_file_database_runs_in_wal_mode(tmp_path: Path) -> None:
    store = TagManagerStore(tmp_path / "tag_manager.sqlite3")

    with store.connection() as conn:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])

    assert mode.casefold() == "wal"


def test_upsert_rows_rolls_back_the_whole_chunk_on_constraint_error(tmp_path: Path) -> None:
    store = TagManagerStore(tmp_path / "tag_manager.sqlite3")
    store.create_session(_session_entry())

    good_before = _scan_row("a.png", [("solo", "general")])
    bad = _scan_row("b.png", [])
    # sidecar_kind violates the schema's CHECK constraint mid-chunk.
    bad["sidecar_kind"] = "not-a-kind"
    good_after = _scan_row("c.png", [("wolf", "general")])

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_rows("sess-1", [good_before, bad, good_after])

    # The whole chunk rolled back: not even the valid rows before and after
    # the offender reached the database.
    items, total = store.list_images("sess-1")
    assert total == 0
    assert items == []

    # No transaction residue: a following valid chunk commits normally.
    assert store.upsert_rows("sess-1", [good_before, good_after]) == 2
    items, total = store.list_images("sess-1")
    assert total == 2
    assert {item["relative_path"] for item in items} == {"a.png", "c.png"}
    tags = store.image_tags([int(item["id"]) for item in items])
    assert {entry["tag"] for entries in tags.values() for entry in entries} == {
        "solo",
        "wolf",
    }
def test_get_images_chunks_the_id_query_and_skips_missing(tmp_path: Path, monkeypatch) -> None:
    """Batch id resolution reads every id in chunked IN () queries -- never
    one connection and one query per id -- and simply skips missing ids."""

    store = TagManagerStore(tmp_path / "tag_manager.sqlite3")
    store.create_session(_session_entry())
    store.upsert_rows(
        "sess-1", [_scan_row(f"bulk/img_{i}.png", [("solo", "general")]) for i in range(1100)]
    )
    ids = [int(item["id"]) for item in store.list_images("sess-1", limit=2000)[0]]

    opened = {"n": 0}
    original = store.connection

    @contextlib.contextmanager
    def counting_connection():
        opened["n"] += 1
        with original() as conn:
            yield conn

    monkeypatch.setattr(store, "connection", counting_connection)

    # 1100 ids fit in three 500-row chunked queries.
    fetched = store.get_images("sess-1", ids)
    assert set(fetched) == set(ids)
    assert opened["n"] == 3

    # Duplicates collapse and missing ids are absent from the result.
    again = store.get_images("sess-1", [ids[0], ids[0], 987_654])
    assert set(again) == {ids[0]}
    assert again[ids[0]]["relative_path"] == "bulk/img_0.png"


def test_tag_filter_key_folds_non_ascii_like_python(tmp_path: Path) -> None:
    """SQLite's built-in LOWER() folds ASCII only; the registered CASEFOLD
    keeps the SQL side of the tag normalization in lockstep with
    ``canonical_tag_key`` for non-ASCII tags."""

    store = TagManagerStore(tmp_path / "tag_manager.sqlite3")
    store.create_session(_session_entry())
    store.upsert_rows(
        "sess-1",
        [
            _scan_row("a.png", [("W\u00d6LFE", "general")]),
            _scan_row("b.png", [("w\u00f6lfe", "general")]),
        ],
    )

    items, total = store.list_images("sess-1", include_tags=["W\u00d6LFE"])
    assert total == 2
    assert {item["file_name"] for item in items} == {"a.png", "b.png"}

    # Excluding one spelling excludes the image carrying the other too.
    items, total = store.list_images("sess-1", exclude_tags=["w\u00f6lfe"])
    assert total == 0

    # Stats group both spellings onto one casefolded key.
    stats = store.tag_stats("sess-1")
    assert len(stats) == 1
    assert stats[0]["count"] == 2
