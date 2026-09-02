"""Tests for the tag manager foundation: sidecar IO and the SQLite index."""

import json
from pathlib import Path

import pytest

from tagger2.tag_manager.sidecar_io import (
    SidecarError,
    dedup_tags,
    load_sidecar,
    render_standard_json,
    render_tag_txt,
    render_tags_json,
)
from tagger2.tag_manager.storage import TagManagerStore


RAW_E621_DOCUMENT = {
    "artist": [],
    "character": ["rex"],
    "contributor": [],
    "copyright": [],
    "general": ["solo", "wolf"],
    "invalid": [],
    "lore": [],
    "meta": ["rating_safe"],
    "species": ["mammal"],
}


def test_tag_txt_round_trip(tmp_path: Path) -> None:
    txt = tmp_path / "a.b.txt"
    txt.write_text("solo, 1girl, blue_eyes\n", encoding="utf-8")

    content = load_sidecar(txt, None)

    assert content.kind == "tag_txt"
    assert content.tags == ("solo", "1girl", "blue_eyes")
    assert render_tag_txt(list(content.tags)) == "solo, 1girl, blue_eyes\n"


def test_dotted_image_names_keep_sidecar_suffix_pairing(tmp_path: Path) -> None:
    txt = tmp_path / "43900,_(artist_tag),yellow.png.txt"
    txt.write_text("solo\n", encoding="utf-8")

    content = load_sidecar(txt, tmp_path / "43900,_(artist_tag),yellow.png.json")

    assert content.kind == "tag_txt"
    assert content.tags == ("solo",)


def test_tag_txt_blank_file_counts_as_none(tmp_path: Path) -> None:
    txt = tmp_path / "a.txt"
    txt.write_text("   \n", encoding="utf-8")

    assert load_sidecar(txt, None).kind == "none"
    assert load_sidecar(None, None).kind == "none"


def test_tags_json_round_trip_preserves_entries_and_extras(tmp_path: Path) -> None:
    json_path = tmp_path / "a.json"
    payload = {
        "schema": "local-tags-v2",
        "tags": [
            {"text": "solo", "category": "general", "score": 0.98},
            {"text": "rex", "category": "character"},
        ],
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    content = load_sidecar(None, json_path)

    assert content.kind == "tags_json"
    assert content.tags == ("solo", "rex")
    assert content.tag_entries[0]["score"] == 0.98
    rendered = render_tags_json(
        [dict(entry) for entry in content.tag_entries], document={"schema": "local-tags-v2"}
    )
    document = json.loads(rendered)
    assert document["schema"] == "local-tags-v2"
    assert document["tags"] == payload["tags"]
    assert rendered.endswith("\n")


def test_tags_json_accepts_legacy_string_tags(tmp_path: Path) -> None:
    json_path = tmp_path / "a.json"
    json_path.write_text(json.dumps({"tag": "solo, 1girl"}), encoding="utf-8")

    content = load_sidecar(None, json_path)

    assert content.kind == "tags_json"
    assert content.tags == ("solo", "1girl")


def test_standard_json_detected_by_nine_field_keys(tmp_path: Path) -> None:
    json_path = tmp_path / "a.json"
    payload = {
        "quality": ["safe"],
        "count": "solo",
        "character": "rex",
        "series": "",
        "artist": "",
        "appearance": ["blue_eyes"],
        "tags": ["wolf"],
        "environment": ["forest"],
        "nl": "A wolf in a forest.",
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")

    content = load_sidecar(None, json_path)

    assert content.kind == "standard_json"
    assert content.tags == ("wolf",)
    assert content.document is not None
    assert content.document["nl"] == "A wolf in a forest."


def test_standard_json_render_freezes_field_order_and_keeps_extras() -> None:
    document = {
        "tags": ["wolf"],
        "extra_key": "kept",
        "nl": "A wolf.",
        "count": "solo",
    }

    rendered = render_standard_json(document)

    assert rendered.endswith("\n")
    assert rendered.index('"quality"') < rendered.index('"count"') < rendered.index('"nl"')
    payload = json.loads(rendered)
    assert payload["extra_key"] == "kept"
    assert payload["count"] == "solo"


def test_raw_e621_json_is_recognized_read_only(tmp_path: Path) -> None:
    json_path = tmp_path / "a.json"
    json_path.write_text(json.dumps(RAW_E621_DOCUMENT), encoding="utf-8")

    content = load_sidecar(None, json_path)

    assert content.kind == "raw_e621_json"
    assert content.tags == ("solo", "wolf", "rating_safe", "mammal", "rex")


def test_invalid_sidecars_fail_closed(tmp_path: Path) -> None:
    bad_json = tmp_path / "a.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(SidecarError):
        load_sidecar(None, bad_json)

    bad_root = tmp_path / "b.json"
    bad_root.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(SidecarError):
        load_sidecar(None, bad_root)

    assert dedup_tags(["Solo", "solo", "SOLO", "wolf"]) == ["Solo", "wolf"]


def _session_entry(session_id: str = "sess-1") -> dict:
    return {
        "id": session_id,
        "name": "demo",
        "root_id": "root-1",
        "relative_path": "dataset",
        "profile": "e621",
        "recursive": True,
    }


def _image_row(relative: str, **overrides: object) -> dict:
    row = {
        "relative_path": relative,
        "file_name": Path(relative).name,
        "image_format": "png",
        "sidecar_kind": "tag_txt",
        "sidecar_path": str(Path(relative).with_suffix(".txt")),
        "mtime": 100.0,
        "sidecar_mtime": 100.0,
        "width": 8,
        "height": 8,
        "tag_count": 2,
    }
    row.update(overrides)
    return row


def test_store_sessions_and_stable_image_ids_across_upserts() -> None:
    store = TagManagerStore(":memory:")
    created = store.create_session(_session_entry())
    assert created["status"] == "indexing"

    first = store.upsert_images("sess-1", [_image_row("a.png")])
    again = store.upsert_images("sess-1", [_image_row("a.png")])
    assert first == again, "upserting the same path must keep the image id stable"

    store.upsert_images("sess-1", [_image_row("b.png")])
    removed = store.prune_images_missing("sess-1", {"a.png"})
    assert removed == 1

    assert store.get_session("sess-1")["image_count"] == 0
    store.update_session("sess-1", status="ready", image_count=1)
    session = store.get_session("sess-1")
    assert session["status"] == "ready"
    assert session["error"] is None


def test_store_filters_sort_and_stats() -> None:
    store = TagManagerStore(":memory:")
    store.create_session(_session_entry())
    rows = [
        _image_row("a.png", mtime=1.0, tag_count=1),
        _image_row("b.png", mtime=3.0, tag_count=3),
        _image_row("c.png", mtime=2.0, tag_count=0, sidecar_kind="none", sidecar_path=None),
    ]
    ids = store.upsert_images("sess-1", rows)
    store.set_image_tags(ids[0], [("solo", "general"), ("rex", "character")],
                         sidecar_kind="tag_txt", sidecar_mtime=1.0)
    store.set_image_tags(ids[1], [("solo", "general"), ("wolf", "general"), ("rex", "character")],
                         sidecar_kind="tag_txt", sidecar_mtime=3.0)

    both, total = store.list_images("sess-1", include_tags=["solo", "rex"])
    assert total == 2 and [item["file_name"] for item in both] == ["a.png", "b.png"]

    any_mode, _ = store.list_images(
        "sess-1", include_tags=["rex", "wolf"], include_mode="any"
    )
    assert len(any_mode) == 2

    excluded, _ = store.list_images("sess-1", exclude_tags=["rex"])
    assert [item["file_name"] for item in excluded] == ["c.png"]

    by_tags, _ = store.list_images("sess-1", sort="tags")
    assert by_tags[0]["file_name"] == "b.png"
    by_mtime, _ = store.list_images("sess-1", sort="mtime")
    assert by_mtime[0]["file_name"] == "b.png"

    stats = store.tag_stats("sess-1", min_count=1)
    counts = {entry["tag"]: entry["count"] for entry in stats}
    assert counts == {"rex": 2, "solo": 2, "wolf": 1}
    top = store.tag_stats("sess-1")[0]
    assert top["count"] == 2

    missing, _ = store.list_images("sess-1", sidecar="missing")
    assert [item["file_name"] for item in missing] == ["c.png"]


def test_store_journal_undone_flow_and_trim() -> None:
    store = TagManagerStore(":memory:")
    store.create_session(_session_entry())

    first = store.append_journal("sess-1", op="edit", spec={"k": 1}, changes=[{"image_id": 1}])
    second = store.append_journal("sess-1", op="batch_add", spec={"k": 2}, changes=[])

    assert store.latest_journal_entry("sess-1", undone=False)["id"] == second
    store.set_journal_undone(first, True)
    assert store.latest_journal_entry("sess-1", undone=True)["id"] == first

    store.trim_journal("sess-1", keep=1)
    remaining = store.journal_entries("sess-1")
    assert [entry["id"] for entry in remaining] == [second]
