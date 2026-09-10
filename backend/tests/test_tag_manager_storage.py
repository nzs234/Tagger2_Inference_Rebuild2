"""Storage-level tests for the tag manager: WAL mode and chunk atomicity.

These tests deliberately open a real file-backed database (never ``:memory:``
or a mock connection) so they exercise the exact pragmas and transaction
handling a scan pays for in production.
"""

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
