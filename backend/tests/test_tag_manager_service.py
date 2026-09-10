"""Service-level tests for the tag manager: index, edit, batch, undo/redo, routes."""

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tagger2.security import PathAllowlist
from tagger2.tag_manager import editing, indexing
from tagger2.tag_manager.api import create_tag_manager_router
from tagger2.tag_manager.editing import MAX_BATCH_IMAGES
from tagger2.tag_manager.contracts import (
    BatchOperationRequest,
    CreateDatasetRequest,
    ImageEditRequest,
    ImageFilter,
    StandardJsonContent,
    TagEdit,
    TagsJsonContent,
    TagTxtContent,
)
from tagger2.tag_manager.service import TagManagerError, TagManagerService
from tagger2.tag_manager.sidecar_io import render_tags_json
from tagger2.tag_manager.storage import TagManagerStore


class FakeTagDatabase:
    def __init__(self, categories: dict[str, str] | None = None):
        self.categories = categories or {"rex": "character", "wolf": "general"}
        self.loaded = True

    def is_loaded(self, profile: str) -> bool:
        return self.loaded

    def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
        return None

    def lookup(self, profile: str, tag: str, *, resolve_alias: bool = True):
        category = self.categories.get(tag.casefold())
        if category is None:
            return None
        return {"name": tag, "category": category, "post_count": 10, "alias_of": None}

    def autocomplete(self, profile: str, query: str, *, limit: int = 20):
        return []

    def available_profiles(self) -> dict[str, list[str]]:
        return {"e621": ["classify-e621-test-v1"], "danbooru": []}


class FakeThumbnails:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def ensure_thumbnail(self, source: Path, *, size: int, mtime: float) -> Path:
        self.calls.append(source)
        return source.with_suffix(".thumb.jpg")


STANDARD_JSON = {
    "quality": ["safe"],
    "count": "solo",
    "character": "rex",
    "series": "",
    "artist": "",
    "appearance": ["blue_eyes"],
    "tags": ["wolf"],
    "environment": ["forest"],
    "nl": "A wolf stands in a forest.",
}

TAGS_JSON = {
    "schema": "local-tags-v2",
    "tags": [{"text": "solo", "category": "general", "score": 0.5}],
}


def _make_image(directory: Path, name: str) -> None:
    Image.new("RGB", (8, 8)).save(directory / name)


@pytest.fixture()
def workspace(tmp_path: Path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")
    (dataset / "a.txt").write_text("solo, wolf\n", encoding="utf-8")
    _make_image(dataset, "b.png")
    (dataset / "b.json").write_text(json.dumps(STANDARD_JSON), encoding="utf-8")
    _make_image(dataset, "c.png")

    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="test-root", kind="input", writable=True)
    store = TagManagerStore(":memory:")
    service = TagManagerService(
        store=store,
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
    )
    session = service.create_session(
        CreateDatasetRequest(root_id="test-root", relative_path="", profile="e621")
    )
    service.index_session(str(session["id"]))
    session = service.get_session(str(session["id"]))
    return service, store, session, dataset


def test_create_and_index_dataset(workspace):
    service, _store, session, _dataset = workspace

    assert session["status"] == "ready"
    assert session["image_count"] == 3

    payload = service.list_images(str(session["id"]))
    items = payload["items"]
    assert payload["total"] == 3
    kinds = {item["file_name"]: item["sidecar_kind"] for item in items}
    assert kinds == {"a.png": "tag_txt", "b.png": "standard_json", "c.png": "none"}
    a = next(item for item in items if item["file_name"] == "a.png")
    assert {t["tag"]: t["category"] for t in a["tags"]} == {
        "solo": "general",
        "wolf": "general",
    }

    filtered = service.list_images(
        str(session["id"]), image_filter=ImageFilter(include_tags=["wolf"], kind="tag_txt")
    )
    assert [item["file_name"] for item in filtered["items"]] == ["a.png"]


def test_get_image_returns_format_native_content(workspace):
    service, _store, session, _dataset = workspace
    items = service.list_images(str(session["id"]))["items"]
    by_name = {item["file_name"]: item for item in items}

    detail = service.get_image(str(session["id"]), int(by_name["b.png"]["id"]))
    assert detail["content"]["kind"] == "standard_json"
    assert detail["content"]["fields"]["nl"] == "A wolf stands in a forest."
    assert detail["content"]["fields"]["character"] == "rex"
    assert detail["sidecar_mtime"] is not None

    raw_none = service.get_image(str(session["id"]), int(by_name["c.png"]["id"]))
    assert raw_none["content"]["kind"] == "none"


def test_save_image_updates_file_index_and_journal(workspace):
    service, store, session, dataset = workspace
    items = service.list_images(str(session["id"]))["items"]
    a = next(item for item in items if item["file_name"] == "a.png")
    detail = service.get_image(str(session["id"]), int(a["id"]))

    result = service.save_image(
        str(session["id"]),
        int(a["id"]),
        ImageEditRequest(
            content=TagTxtContent(tags=["solo", "wolf", "rex"]),
            expected_sidecar_mtime=detail["sidecar_mtime"],
        ),
    )

    assert result["sidecar_kind"] == "tag_txt"
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf, rex\n"
    updated = service.get_image(str(session["id"]), int(a["id"]))
    assert {tag["tag"] for tag in updated["tags"]} == {"solo", "wolf", "rex"}
    assert updated["tags"][2]["category"] == "character"  # enriched via the tag db
    journal = store.journal_entries(str(session["id"]))
    assert len(journal) == 1 and journal[0]["op"] == "edit"


def test_save_image_rejects_stale_mtime(workspace):
    service, _store, session, dataset = workspace
    items = service.list_images(str(session["id"]))["items"]
    a = next(item for item in items if item["file_name"] == "a.png")

    # The file changes after the editor loaded it.
    (dataset / "a.txt").write_text("changed externally\n", encoding="utf-8")

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            str(session["id"]),
            int(a["id"]),
            ImageEditRequest(
                content=TagTxtContent(tags=["solo"]),
                expected_sidecar_mtime=1.0,
            ),
        )
    assert excinfo.value.code == "sidecar_conflict"


def test_save_image_rejects_kind_mismatch(workspace):
    service, _store, session, _dataset = workspace
    items = service.list_images(str(session["id"]))["items"]
    a = next(item for item in items if item["file_name"] == "a.png")

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            str(session["id"]),
            int(a["id"]),
            ImageEditRequest(content=StandardJsonContent(fields={"tags": ["x"]})),
        )
    assert excinfo.value.code == "sidecar_kind_mismatch"


def test_save_image_rejects_raw_e621_sidecars(workspace):
    service, store, session, dataset = workspace
    _make_image(dataset, "raw.png")
    (dataset / "raw.json").write_text(
        json.dumps(
            {
                "artist": [], "character": [], "contributor": [], "copyright": [],
                "general": ["solo"], "invalid": [], "lore": [], "meta": [], "species": [],
            }
        ),
        encoding="utf-8",
    )
    service.index_session(str(session["id"]))
    items = service.list_images(str(session["id"]), image_filter=ImageFilter(kind="raw_e621_json"))["items"]
    assert len(items) == 1
    raw = items[0]

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            str(session["id"]),
            int(raw["id"]),
            ImageEditRequest(content=TagTxtContent(tags=["solo"])),
        )
    assert excinfo.value.code == "sidecar_read_only"


def test_batch_operations_across_formats_keep_nl_untouched(workspace):
    service, _store, session, dataset = workspace
    request = BatchOperationRequest(op="add", tags=["forest_night"], image_ids=None,
                                    filter=ImageFilter(sidecar="present"))
    result = service.batch_operation(str(session["id"]), request)

    assert result["affected"] == 2
    assert "forest_night" in (dataset / "a.txt").read_text(encoding="utf-8")
    b_document = json.loads((dataset / "b.json").read_text(encoding="utf-8"))
    assert "forest_night" in b_document["tags"]
    assert "forest_night" in b_document["appearance"]
    assert b_document["nl"] == "A wolf stands in a forest."
    assert b_document["count"] == "solo"

    remove = BatchOperationRequest(op="remove", tags=["wolf"], image_ids=None,
                                   filter=ImageFilter(sidecar="present"))
    service.batch_operation(str(session["id"]), remove)
    assert "wolf" not in (dataset / "a.txt").read_text(encoding="utf-8")

    replace = BatchOperationRequest(op="replace", tags=["solo"], replacement="duo",
                                    image_ids=None, filter=ImageFilter(sidecar="present"))
    service.batch_operation(str(session["id"]), replace)
    assert "duo" in (dataset / "a.txt").read_text(encoding="utf-8")
    assert "solo" not in (dataset / "a.txt").read_text(encoding="utf-8")


def test_undo_redo_restores_sidecars(workspace):
    service, store, session, dataset = workspace
    request = BatchOperationRequest(op="add", tags=["night"], image_ids=None,
                                    filter=ImageFilter(sidecar="present"))
    service.batch_operation(str(session["id"]), request)
    assert "night" in (dataset / "a.txt").read_text(encoding="utf-8")

    undone = service.undo(str(session["id"]))
    assert undone["reverted"] == 2
    assert "night" not in (dataset / "a.txt").read_text(encoding="utf-8")
    assert "night" not in (dataset / "b.json").read_text(encoding="utf-8")

    redone = service.redo(str(session["id"]))
    assert redone["reapplied"] == 2
    assert "night" in (dataset / "a.txt").read_text(encoding="utf-8")

    with pytest.raises(TagManagerError) as excinfo:
        service.redo(str(session["id"]))
    assert excinfo.value.code == "redo_empty"


def test_tag_stats_and_thumbnail_endpoint_data(workspace):
    service, _store, session, _dataset = workspace
    stats = service.tag_stats(str(session["id"]))
    counts = {entry["tag"]: entry["count"] for entry in stats}
    assert counts["wolf"] == 2  # a.png tags + b.png standard_json tags
    assert counts["solo"] == 1

    items = service.list_images(str(session["id"]))["items"]
    image_id = int(items[0]["id"])
    thumbnail = service.thumbnail(str(session["id"]), image_id, size=256)
    assert str(thumbnail).endswith(".thumb.jpg")


def test_router_error_envelope_and_happy_paths(workspace):
    service, _store, session, _dataset = workspace
    app = FastAPI()
    app.include_router(create_tag_manager_router(service))
    client = TestClient(app)

    listed = client.get("/api/v1/tag-manager/datasets")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    missing = client.get("/api/v1/tag-manager/datasets/nope")
    assert missing.status_code == 404
    body = missing.json()["detail"]
    assert body["code"] == "dataset_not_found"

    images = client.get(
        f"/api/v1/tag-manager/datasets/{session['id']}/images",
        params={"include_tags": "wolf", "kind": "tag_txt"},
    )
    assert images.status_code == 200
    payload = images.json()
    assert payload["total"] == 1 and payload["items"][0]["file_name"] == "a.png"

    stats = client.get(f"/api/v1/tag-manager/datasets/{session['id']}/tags/stats")
    assert stats.status_code == 200
    assert {item["tag"] for item in stats.json()["items"]} >= {"solo", "wolf"}

    info = client.get("/api/v1/tag-manager/tag-db/info")
    assert info.status_code == 200
    assert info.json()["available"]["e621"] == ["classify-e621-test-v1"]

    autocomplete = client.get(
        "/api/v1/tag-manager/tag-db", params={"profile": "e621", "query": "wol"}
    )
    assert autocomplete.status_code == 200

    bad_filter = client.get(
        f"/api/v1/tag-manager/datasets/{session['id']}/images", params={"kind": "weird"}
    )
    assert bad_filter.status_code == 422


def test_edit_rejects_read_only_root_with_actionable_error(tmp_path: Path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")

    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="ro-root", kind="input", writable=False)
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
    )
    session = service.create_session(
        CreateDatasetRequest(root_id="ro-root", relative_path="", profile="e621")
    )
    service.index_session(str(session["id"]))
    items = service.list_images(str(session["id"]))["items"]

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            str(session["id"]),
            int(items[0]["id"]),
            ImageEditRequest(content=TagTxtContent(tags=["x"])),
        )
    assert excinfo.value.code == "root_not_writable"
    assert excinfo.value.status_code == 403

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            str(session["id"]),
            BatchOperationRequest(op="add", tags=["x"], image_ids=[int(items[0]["id"])]),
        )
    assert excinfo.value.code == "root_not_writable"


def test_autocomplete_maps_missing_snapshot_to_clean_error(tmp_path: Path):
    class MissingSnapshotDb(FakeTagDatabase):
        def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
            from tagger2.tag_manager.tag_db import TagDatabaseError

            raise TagDatabaseError("no classify resource for profile 'danbooru'")

    allowlist = PathAllowlist()
    allowlist.register(tmp_path, root_id="any-root", kind="input", writable=False)
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=MissingSnapshotDb(),
    )

    with pytest.raises(TagManagerError) as excinfo:
        service.autocomplete("danbooru", "wolf")
    assert excinfo.value.code == "tag_db_unavailable"
    assert excinfo.value.status_code == 409


# -- journal kind / version persistence and replay safety ---------------------


def _image_ids_by_name(service: TagManagerService, session_id: str) -> dict[str, int]:
    return {
        item["file_name"]: int(item["id"])
        for item in service.list_images(session_id)["items"]
    }


def _add_tags_json_sidecar(service: TagManagerService, session: dict, dataset: Path) -> int:
    _make_image(dataset, "d.png")
    (dataset / "d.json").write_text(json.dumps(TAGS_JSON), encoding="utf-8")
    service.index_session(str(session["id"]))
    return _image_ids_by_name(service, str(session["id"]))["d.png"]


def test_tags_json_save_undo_redo_roundtrip(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    d_id = _add_tags_json_sidecar(service, session, dataset)
    detail = service.get_image(session_id, d_id)
    assert detail["content"]["kind"] == "tags_json"

    result = service.save_image(
        session_id,
        d_id,
        ImageEditRequest(
            content=TagsJsonContent(tags=[
                TagEdit(text="solo", score=0.5),
                TagEdit(text="rex", category="character"),
            ]),
            expected_sidecar_mtime=detail["sidecar_mtime"],
        ),
    )
    assert result["sidecar_kind"] == "tags_json"
    change = store.journal_entries(session_id)[0]["changes"][0]
    assert change["kind"] == "tags_json"
    assert change["existed"] is True
    assert change["before_version"]["size"] > 0
    assert change["after_version"]["mtime_ns"] >= change["before_version"]["mtime_ns"]

    undone = service.undo(session_id)
    assert undone["reverted"] == 1
    assert json.loads((dataset / "d.json").read_text(encoding="utf-8")) == TAGS_JSON
    # The tags_json kind must survive the replay, or the next save would be
    # rejected as a kind mismatch.
    assert store.get_image(session_id, d_id)["sidecar_kind"] == "tags_json"

    redone = service.redo(session_id)
    assert redone["reapplied"] == 1
    document = json.loads((dataset / "d.json").read_text(encoding="utf-8"))
    assert [entry["text"] for entry in document["tags"]] == ["solo", "rex"]
    assert store.get_image(session_id, d_id)["sidecar_kind"] == "tags_json"

    # A follow-up tags_json save after the undo/redo cycle must not be
    # rejected as a kind mismatch either.
    detail = service.get_image(session_id, d_id)
    service.save_image(
        session_id,
        d_id,
        ImageEditRequest(
            content=TagsJsonContent(tags=[TagEdit(text="solo", score=0.5)]),
            expected_sidecar_mtime=detail["sidecar_mtime"],
        ),
    )
    assert store.get_image(session_id, d_id)["sidecar_kind"] == "tags_json"


def test_batch_undo_redo_preserves_tags_json_kind(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    d_id = _add_tags_json_sidecar(service, session, dataset)

    result = service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[d_id])
    )
    assert result["affected"] == 1
    change = store.journal_entries(session_id)[0]["changes"][0]
    assert change["kind"] == "tags_json"
    assert change["existed"] is True
    assert change["before_version"] and change["after_version"]

    assert service.undo(session_id)["reverted"] == 1
    assert json.loads((dataset / "d.json").read_text(encoding="utf-8")) == TAGS_JSON
    assert store.get_image(session_id, d_id)["sidecar_kind"] == "tags_json"

    assert service.redo(session_id)["reapplied"] == 1
    document = json.loads((dataset / "d.json").read_text(encoding="utf-8"))
    assert "night" in [entry["text"] for entry in document["tags"]]
    assert store.get_image(session_id, d_id)["sidecar_kind"] == "tags_json"


def test_standard_json_journal_kind_survives_undo_redo(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    b_id = _image_ids_by_name(service, session_id)["b.png"]
    before_text = (dataset / "b.json").read_text(encoding="utf-8")

    result = service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[b_id])
    )
    assert result["affected"] == 1
    change = store.journal_entries(session_id)[0]["changes"][0]
    assert change["kind"] == "standard_json"
    assert change["after_version"]["size"] > len(before_text)

    assert service.undo(session_id)["reverted"] == 1
    assert (dataset / "b.json").read_text(encoding="utf-8") == before_text
    assert store.get_image(session_id, b_id)["sidecar_kind"] == "standard_json"

    assert service.redo(session_id)["reapplied"] == 1
    assert store.get_image(session_id, b_id)["sidecar_kind"] == "standard_json"


def test_undo_restores_missing_sidecar_and_redo_recreates_it(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    c_id = _image_ids_by_name(service, session_id)["c.png"]

    result = service.save_image(
        session_id, c_id, ImageEditRequest(content=TagTxtContent(tags=["fresh", "solo"]))
    )
    assert result["sidecar_kind"] == "tag_txt"
    assert (dataset / "c.txt").is_file()
    change = store.journal_entries(session_id)[0]["changes"][0]
    assert change["existed"] is False
    assert change["kind"] == "tag_txt"
    assert change["before_version"] is None
    assert change["after_version"] is not None

    assert service.undo(session_id)["reverted"] == 1
    assert not (dataset / "c.txt").exists()
    restored = store.get_image(session_id, c_id)
    assert restored["sidecar_kind"] == "none"
    assert restored["sidecar_mtime"] is None

    assert service.redo(session_id)["reapplied"] == 1
    assert (dataset / "c.txt").read_text(encoding="utf-8") == "fresh, solo\n"
    assert store.get_image(session_id, c_id)["sidecar_kind"] == "tag_txt"


def test_legacy_journal_change_warns_and_falls_back(workspace, caplog):
    """Pre-kind journal entries keep working, loudly, and never misread a
    tags_json file as standard_json."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    d_id = _add_tags_json_sidecar(service, session, dataset)
    ids = _image_ids_by_name(service, session_id)
    a_id = ids["a.png"]
    tags_json_text = json.dumps(TAGS_JSON)

    after_txt = "solo, wolf, night\n"
    after_json = render_tags_json(
        [{"text": "solo", "category": "general"}, {"text": "night", "category": "general"}],
        document={"schema": "local-tags-v2"},
    )
    (dataset / "a.txt").write_text(after_txt, encoding="utf-8")
    (dataset / "d.json").write_text(after_json, encoding="utf-8")
    store.append_journal(
        session_id,
        op="batch_add",
        spec={"tags": ["night"], "count": 2},
        changes=[
            {  # legacy change: no kind, no version stamps
                "image_id": a_id, "sidecar": "a.txt", "existed": True,
                "before": "solo, wolf\n", "after": after_txt,
            },
            {
                "image_id": d_id, "sidecar": "d.json", "existed": True,
                "before": tags_json_text, "after": after_json,
            },
        ],
    )

    with caplog.at_level(logging.WARNING, logger="tagger2.tag_manager"):
        undone = service.undo(session_id)
    assert undone["reverted"] == 2
    assert "no kind recorded" in caplog.text
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"
    assert json.loads((dataset / "d.json").read_text(encoding="utf-8")) == TAGS_JSON
    kinds = {
        item["file_name"]: store.get_image(session_id, int(item["id"]))["sidecar_kind"]
        for item in service.list_images(session_id)["items"]
    }
    assert kinds["a.png"] == "tag_txt"
    # The suffix fallback says standard_json for .json files; the restored
    # content itself must decide.
    assert kinds["d.png"] == "tags_json"


def test_undo_refuses_externally_changed_sidecar(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]
    service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[a_id])
    )
    change = store.journal_entries(session_id)[0]["changes"][0]
    assert change["after"] == "solo, wolf, night\n"

    (dataset / "a.txt").write_text("external edit\n", encoding="utf-8")
    with pytest.raises(TagManagerError) as excinfo:
        service.undo(session_id)
    assert excinfo.value.code == "sidecar_conflict"
    # The external file was not overwritten and the entry stays undoable.
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "external edit\n"
    assert store.latest_journal_entry(session_id, undone=False) is not None

    (dataset / "a.txt").write_text(change["after"], encoding="utf-8")
    assert service.undo(session_id)["reverted"] == 1
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"


def test_redo_refuses_externally_changed_sidecar(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]
    service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[a_id])
    )
    change = store.journal_entries(session_id)[0]["changes"][0]
    service.undo(session_id)
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"

    (dataset / "a.txt").write_text("external edit\n", encoding="utf-8")
    with pytest.raises(TagManagerError) as excinfo:
        service.redo(session_id)
    assert excinfo.value.code == "sidecar_conflict"
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "external edit\n"

    (dataset / "a.txt").write_text(change["before"], encoding="utf-8")
    assert service.redo(session_id)["reapplied"] == 1
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf, night\n"


def test_replay_refuses_format_mismatch_without_overwrite(workspace):
    """A journalled kind that no longer matches the live format must not be
    replayed over the current file."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    tags_body = json.dumps({"tags": [{"text": "solo"}]})
    (dataset / "a.json").write_text(tags_body, encoding="utf-8")
    store.append_journal(
        session_id,
        op="edit",
        spec={"image_ids": [a_id], "kind": "standard_json"},
        changes=[{
            "image_id": a_id, "sidecar": "a.json", "existed": True,
            "kind": "standard_json",
            "before": json.dumps({"tags": []}), "after": tags_body,
            "before_version": {"mtime_ns": 1, "size": 14},
            "after_version": {"mtime_ns": 2, "size": len(tags_body)},
        }],
    )

    with pytest.raises(TagManagerError) as excinfo:
        service.undo(session_id)
    assert excinfo.value.code == "sidecar_kind_mismatch"
    assert (dataset / "a.json").read_text(encoding="utf-8") == tags_body


def test_replay_rejects_corrupt_journalled_text(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    b_id = _image_ids_by_name(service, session_id)["b.png"]
    live_text = (dataset / "b.json").read_text(encoding="utf-8")
    store.append_journal(
        session_id,
        op="edit",
        spec={"kind": "standard_json"},
        changes=[{
            "image_id": b_id, "sidecar": "b.json", "existed": True,
            "kind": "standard_json",
            "before": "{corrupt json", "after": live_text,
            "before_version": None, "after_version": None,
        }],
    )

    with pytest.raises(TagManagerError) as excinfo:
        service.undo(session_id)
    assert excinfo.value.code == "journal_invalid"
    # validated before anything was written
    assert (dataset / "b.json").read_text(encoding="utf-8") == live_text


# -- batch image_ids dedup -----------------------------------------------------


def test_batch_image_ids_deduplicate_preserving_order():
    request = BatchOperationRequest(op="add", tags=["x"], image_ids=[7, 3, 7, 3, 9])
    assert request.image_ids == [7, 3, 9]


def test_batch_duplicate_image_ids_apply_once(workspace):
    """A repeated id must not apply the op twice (visible for non-idempotent
    regex replaces)."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    result = service.batch_operation(
        session_id,
        BatchOperationRequest(
            op="replace", tags=["o"], replacement="oo", use_regex=True,
            image_ids=[a_id, a_id, a_id],
        ),
    )
    assert result["affected"] == 1
    # Single application: solo -> sooloo, wolf -> woolf. A second pass over
    # the same image would have grown every run of o's again.
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "sooloo, woolf\n"


# -- batch failure recovery ----------------------------------------------------


def test_batch_midway_failure_journals_partial_changes(workspace):
    service, store, session, dataset = workspace
    session_id = str(session["id"])
    ids = _image_ids_by_name(service, session_id)
    a_id, b_id = ids["a.png"], ids["b.png"]
    # b's sidecar becomes unparsable after the scan: the batch fails on it but
    # the first image's already-written change must stay recoverable.
    (dataset / "b.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[a_id, b_id]),
        )
    assert excinfo.value.code == "sidecar_invalid"
    assert "fresh" in (dataset / "a.txt").read_text(encoding="utf-8")

    entry = store.latest_journal_entry(session_id, undone=False)
    assert entry["spec"]["partial"] is True
    assert entry["spec"]["count"] == 1
    assert [change["image_id"] for change in entry["changes"]] == [a_id]

    assert service.undo(session_id)["reverted"] == 1
    assert "fresh" not in (dataset / "a.txt").read_text(encoding="utf-8")
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"


# -- concurrency guards and version checks ------------------------------------


def test_write_operations_reject_while_session_locked(workspace):
    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    lock = service._session_lock(session_id)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(TagManagerError) as excinfo:
            service.save_image(
                session_id, a_id, ImageEditRequest(content=TagTxtContent(tags=["x"]))
            )
        assert excinfo.value.code == "session_busy"
        assert excinfo.value.retryable

        with pytest.raises(TagManagerError) as excinfo:
            service.batch_operation(
                session_id, BatchOperationRequest(op="add", tags=["x"], image_ids=[a_id])
            )
        assert excinfo.value.code == "session_busy"

        with pytest.raises(TagManagerError) as excinfo:
            service.undo(session_id)
        assert excinfo.value.code == "session_busy"

        with pytest.raises(TagManagerError) as excinfo:
            service.redo(session_id)
        assert excinfo.value.code == "session_busy"
    finally:
        lock.release()

    # once the lock is free, the same operations work again
    service.save_image(session_id, a_id, ImageEditRequest(content=TagTxtContent(tags=["x"])))


def test_save_rejects_sidecar_created_after_no_sidecar_load(workspace):
    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]
    (dataset / "a.txt").unlink()
    service.index_session(session_id)
    detail = service.get_image(session_id, a_id)
    assert detail["content"]["kind"] == "none"

    (dataset / "a.txt").write_text("external\n", encoding="utf-8")
    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            session_id,
            a_id,
            ImageEditRequest(
                content=TagTxtContent(tags=["solo"]),
                expected_sidecar_mtime=detail["sidecar_mtime"],
            ),
        )
    assert excinfo.value.code == "sidecar_conflict"
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "external\n"


def test_save_falls_back_to_indexed_mtime_on_external_change(workspace):
    """Without a client-supplied mtime, the indexed mtime guards against
    silently overwriting an externally modified sidecar."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]
    # Pin the indexed mtime so the external rewrite below can never collide.
    os.utime(dataset / "a.txt", (1_000_000_000, 1_000_000_000))
    service.index_session(session_id)

    (dataset / "a.txt").write_text("changed externally\n", encoding="utf-8")
    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            session_id, a_id, ImageEditRequest(content=TagTxtContent(tags=["solo"]))
        )
    assert excinfo.value.code == "sidecar_conflict"
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "changed externally\n"

    # After a rescan the index is current again and the save goes through.
    service.index_session(session_id)
    service.save_image(
        session_id, a_id, ImageEditRequest(content=TagTxtContent(tags=["solo"]))
    )
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo\n"


# -- P0 regression coverage: extras round-trip, redo stack, refresh, stats -----


def test_save_preserves_tags_json_container_and_entry_extras(workspace):
    """Container-level and entry-level extras survive a save untouched."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    _make_image(dataset, "e.png")
    sidecar = {
        "schema": "local-tags-v2",
        "source": {"tool": "test"},  # nested extra: not editable, but present
        "tags": [
            {"text": "solo", "category": "general", "score": 0.5, "origin": "importer"},
            {"text": "wolf", "locked": True, "aliases": ["canis"]},
        ],
    }
    (dataset / "e.json").write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    service.index_session(session_id)
    e_id = _image_ids_by_name(service, session_id)["e.png"]

    detail = service.get_image(session_id, e_id)
    # The read payload ships flat extras to the editor; nested ones (source)
    # are stripped from the client contract and re-merged from disk on save.
    payload = detail["content"]
    assert payload["schema"] == "local-tags-v2"
    assert "source" not in payload
    assert payload["tags"][0].get("origin") == "importer"
    assert payload["tags"][1].get("locked") is True
    assert payload["tags"][1].get("aliases") == ["canis"]

    # Save exactly what the editor received: flat extras round-trip and the
    # nested ones are merged back server-side.
    content = TagsJsonContent(
        **{key: value for key, value in payload.items() if key not in {"kind", "tags"}},
        tags=[TagEdit(**entry) for entry in payload["tags"]],
    )
    service.save_image(session_id, e_id, ImageEditRequest(
        content=content, expected_sidecar_mtime=detail["sidecar_mtime"]
    ))
    saved = json.loads((dataset / "e.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "local-tags-v2"
    assert saved["source"] == {"tool": "test"}
    assert saved["tags"][0]["origin"] == "importer"
    assert saved["tags"][1]["locked"] is True
    assert saved["tags"][1]["aliases"] == ["canis"]


def test_save_preserves_standard_json_top_level_extras(workspace):
    """Top-level keys outside the nine frozen fields survive a save."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    b_id = _image_ids_by_name(service, session_id)["b.png"]
    document = {**STANDARD_JSON, "meta_version": 3, "reviewed": True, "labels": ["curated"]}
    (dataset / "b.json").write_text(json.dumps(document), encoding="utf-8")
    service.index_session(session_id)

    detail = service.get_image(session_id, b_id)
    payload = detail["content"]
    assert payload["meta_version"] == 3
    assert payload["reviewed"] is True
    assert payload["labels"] == ["curated"]

    fields = payload["fields"]
    fields["tags"] = ["wolf", "night"]
    content = StandardJsonContent(
        **{key: value for key, value in payload.items() if key not in {"kind", "fields"}},
        fields=fields,
    )
    service.save_image(session_id, b_id, ImageEditRequest(
        content=content, expected_sidecar_mtime=detail["sidecar_mtime"]
    ))
    saved = json.loads((dataset / "b.json").read_text(encoding="utf-8"))
    assert saved["meta_version"] == 3
    assert saved["reviewed"] is True
    assert saved["labels"] == ["curated"]
    assert saved["tags"] == ["wolf", "night"]


def test_save_rejects_nested_extra_values(workspace):
    """A client cannot smuggle nested structures through extras."""

    from tagger2.tag_manager.contracts import TagEdit

    with pytest.raises(Exception):
        TagEdit(text="solo", deep={"a": {"b": 1}})


def test_fresh_edit_after_undo_clears_redo_stack(workspace):
    """A new save after an undo drops the unreachable redo entries."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    detail = service.get_image(session_id, a_id)
    service.save_image(session_id, a_id, ImageEditRequest(
        content=TagTxtContent(tags=["solo", "wolf", "rex"]),
        expected_sidecar_mtime=detail["sidecar_mtime"],
    ))
    service.undo(session_id)
    assert store.latest_journal_entry(session_id, undone=True) is not None

    # A new edit branches the history; the redo entry must be gone.
    detail = service.get_image(session_id, a_id)
    service.save_image(session_id, a_id, ImageEditRequest(
        content=TagTxtContent(tags=["night"]),
        expected_sidecar_mtime=detail["sidecar_mtime"],
    ))
    assert store.latest_journal_entry(session_id, undone=True) is None
    with pytest.raises(TagManagerError) as excinfo:
        service.redo(session_id)
    assert excinfo.value.code == "redo_empty"


def test_fresh_batch_after_undo_clears_redo_stack(workspace):
    service, store, session, _dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[a_id])
    )
    service.undo(session_id)
    assert store.latest_journal_entry(session_id, undone=True) is not None

    service.batch_operation(
        session_id, BatchOperationRequest(op="remove", tags=["wolf"], image_ids=[a_id])
    )
    assert store.latest_journal_entry(session_id, undone=True) is None


def test_refresh_reports_session_busy_under_write(workspace):
    """A refresh during an in-flight write returns 409 instead of a fake 202."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])

    lock = service._session_lock(session_id)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(TagManagerError) as excinfo:
            service.refresh_session(session_id)
        assert excinfo.value.code == "session_busy"
        assert excinfo.value.retryable
    finally:
        lock.release()

    # Once free, the refresh schedules the rescan normally.
    refreshed = service.refresh_session(session_id)
    assert refreshed["status"] in {"indexing", "ready", "error"}


def test_async_index_scan_future_is_tracked_then_cleared(workspace):
    """The async scan path keeps its future until the scan has finished."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    lock = service._session_lock(session_id)

    async def drive():
        # Hold the session lock so the scheduled scan cannot complete while
        # the tracking assertion runs.
        with lock:
            service.schedule_index(session_id)
            future = service._index_futures.get(session_id)
            assert future is not None, "the async path must track its scan future"
        deadline = time.monotonic() + 10.0
        while session_id in service._index_futures:
            if time.monotonic() > deadline:
                raise AssertionError("index future was never cleared")
            await asyncio.sleep(0.01)

    asyncio.run(drive())
    assert service.get_session(session_id)["status"] == "ready"


def test_async_index_scan_logs_errors_that_escape_the_scan(workspace, caplog):
    """A failed scan future is logged instead of vanishing silently."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])

    def explode(_session_id):
        raise RuntimeError("scan exploded")

    service.index_session = explode

    async def drive():
        service.schedule_index(session_id)
        deadline = time.monotonic() + 10.0
        while session_id in service._index_futures:
            if time.monotonic() > deadline:
                raise AssertionError("failed index future was never cleared")
            await asyncio.sleep(0.01)

    with caplog.at_level(logging.WARNING, logger="tagger2.tag_manager"):
        asyncio.run(drive())
    assert "scan exploded" in caplog.text


def test_delete_session_cancels_a_queued_index_scan(workspace):
    """A queued (not yet started) scan is cancelled when its session dies."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    block = threading.Event()

    async def drive():
        loop = asyncio.get_running_loop()
        # Saturate the default executor with blocked workers so the scheduled
        # scan stays queued while delete_session runs.
        occupied = [loop.run_in_executor(None, block.wait) for _ in range(64)]
        service.schedule_index(session_id)
        future = service._index_futures[session_id]
        service.delete_session(session_id)
        assert future.cancelled(), "a queued scan must be cancelled on delete"
        assert session_id not in service._index_futures
        block.set()
        await asyncio.gather(*occupied)

    asyncio.run(drive())


def test_tag_stats_merges_underscore_and_space_spellings(workspace):
    """`long hair` and `long_hair` count as one tag in the stats panel."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    _make_image(dataset, "f.png")
    _make_image(dataset, "g.png")
    (dataset / "f.txt").write_text("long hair, solo\n", encoding="utf-8")
    (dataset / "g.txt").write_text("long_hair, duo\n", encoding="utf-8")
    service.index_session(session_id)

    stats = service.tag_stats(session_id)
    counts = {row["tag"]: row["count"] for row in stats}
    assert counts.get("long_hair", counts.get("long hair")) == 2
    assert not ("long_hair" in counts and "long hair" in counts)


def test_get_image_strips_nested_entry_extras_and_save_keeps_them(workspace):
    """Nested per-entry extras never enter the client contract (the strict
    TagEdit model would reject them with a 422), yet saving the payload the
    editor received keeps them on disk via the server-side merge."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    _make_image(dataset, "h.png")
    (dataset / "h.json").write_text(
        json.dumps({
            "tags": [
                {"text": "solo", "category": "general"},
                {"text": "wolf", "meta": {"a": 1}},
            ],
        }),
        encoding="utf-8",
    )
    service.index_session(session_id)
    h_id = _image_ids_by_name(service, session_id)["h.png"]

    detail = service.get_image(session_id, h_id)
    payload = detail["content"]
    wolf_entry = next(entry for entry in payload["tags"] if entry["text"] == "wolf")
    assert "meta" not in wolf_entry

    # Save exactly what the client received: without the strip this strict
    # construction (and therefore the PATCH request) would fail validation.
    content = TagsJsonContent(
        **{key: value for key, value in payload.items() if key not in {"kind", "tags"}},
        tags=[TagEdit(**entry) for entry in payload["tags"]],
    )
    result = service.save_image(session_id, h_id, ImageEditRequest(
        content=content, expected_sidecar_mtime=detail["sidecar_mtime"],
    ))
    assert result["sidecar_kind"] == "tags_json"

    # The nested extra is merged back from the disk original, not dropped.
    saved = json.loads((dataset / "h.json").read_text(encoding="utf-8"))
    saved_wolf = next(entry for entry in saved["tags"] if entry["text"] == "wolf")
    assert saved_wolf["meta"] == {"a": 1}


def test_failed_save_keeps_redo_stack_replayable(workspace):
    """A save that fails (sidecar conflict here) must not drop the redo
    history: the stack is discarded only after a journal entry lands."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[a_id])
    )
    service.undo(session_id)
    assert store.latest_journal_entry(session_id, undone=True) is not None
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"

    detail = service.get_image(session_id, a_id)
    # An external rewrite pins a mtime the editor cannot know about.
    (dataset / "a.txt").write_text("changed externally\n", encoding="utf-8")
    os.utime(dataset / "a.txt", (1_000_000_000, 1_000_000_000))
    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            session_id, a_id, ImageEditRequest(
                content=TagTxtContent(tags=["solo", "wolf"]),
                expected_sidecar_mtime=detail["sidecar_mtime"],
            )
        )
    assert excinfo.value.code == "sidecar_conflict"
    # The failed save appended nothing and kept the redo entry.
    assert len(store.journal_entries(session_id)) == 1
    assert store.latest_journal_entry(session_id, undone=True) is not None

    # Once the sidecar matches the journalled before-state, redo still works.
    (dataset / "a.txt").write_text("solo, wolf\n", encoding="utf-8")
    assert service.redo(session_id)["reapplied"] == 1
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf, night\n"


def test_noop_batch_keeps_redo_stack_and_skips_journal(workspace):
    """A batch with nothing to do branches no history: no journal entry, redo
    stack untouched, and the response reports affected 0 without a journal."""

    service, store, session, _dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["night"], image_ids=[a_id])
    )
    service.undo(session_id)
    assert store.latest_journal_entry(session_id, undone=True) is not None

    # Zero matching targets.
    result = service.batch_operation(
        session_id,
        BatchOperationRequest(op="add", tags=["night"], image_ids=None,
                              filter=ImageFilter(include_tags=["no-such-tag"])),
    )
    assert result["affected"] == 0
    assert result["journal_id"] is None
    assert store.latest_journal_entry(session_id, undone=True) is not None

    # Targets exist but every one is a no-op (the tag is already present).
    result = service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["wolf"], image_ids=[a_id])
    )
    assert result["affected"] == 0
    assert result["journal_id"] is None
    assert store.latest_journal_entry(session_id, undone=True) is not None
    # Neither run appended an (empty) journal entry.
    assert len(store.journal_entries(session_id)) == 1


def test_refresh_session_rescans_in_sync_context(workspace):
    """A synchronous refresh (no running event loop) actually rescans: the
    busy probe releases the lock before scheduling, so the inline index run
    can acquire it instead of silently skipping."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    assert service.get_session(session_id)["status"] == "ready"

    _make_image(dataset, "added_later.png")
    service.refresh_session(session_id)

    names = {item["file_name"] for item in service.list_images(session_id)["items"]}
    assert "added_later.png" in names
    assert service.get_session(session_id)["status"] == "ready"


def test_tag_stats_counts_image_once_for_double_spelling(workspace):
    """One image carrying both `long hair` and `long_hair` counts once in the
    stats panel, not once per spelling."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    _make_image(dataset, "i.png")
    (dataset / "i.txt").write_text("long hair, long_hair, solo\n", encoding="utf-8")
    service.index_session(session_id)

    stats = service.tag_stats(session_id)
    counts = {row["tag"]: row["count"] for row in stats}
    assert counts.get("long_hair", counts.get("long hair")) == 1


# -- incremental rescans -------------------------------------------------------


def test_incremental_rescan_skips_unchanged_files(workspace, monkeypatch):
    """A rescan over an unchanged dataset reuses the indexed rows: no sidecar
    is re-parsed and no image header is probed again."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])

    real_open = Image.open
    opened: list[str] = []

    def counting_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Image, "open", counting_open)
    real_load = indexing.load_sidecar
    loaded: list[str | None] = []

    def counting_load(txt_path, json_path):
        source = txt_path or json_path
        loaded.append(source.name if source is not None else None)
        return real_load(txt_path, json_path)

    monkeypatch.setattr(indexing, "load_sidecar", counting_load)

    service.index_session(session_id)  # second scan: nothing changed

    assert opened == []
    assert loaded == []
    refreshed = service.get_session(session_id)
    assert refreshed["status"] == "ready"
    assert refreshed["image_count"] == 3  # skipped images still count


def test_incremental_rescan_reindexes_only_changed_sidecars(workspace, monkeypatch):
    """Sidecars that changed, appeared or disappeared are re-parsed; the image
    files themselves are untouched, so nothing else is re-indexed."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    items = {item["file_name"]: item for item in service.list_images(session_id)["items"]}
    b_id = int(items["b.png"]["id"])

    real_load = indexing.load_sidecar
    loaded: list[str | None] = []

    def counting_load(txt_path, json_path):
        source = txt_path or json_path
        loaded.append(source.name if source is not None else None)
        return real_load(txt_path, json_path)

    monkeypatch.setattr(indexing, "load_sidecar", counting_load)

    # a's sidecar is modified, c gains a sidecar (none -> tag_txt), b's
    # sidecar is deleted; a far-away mtime rules out float-rounding luck.
    (dataset / "a.txt").write_text("solo, wolf, night\n", encoding="utf-8")
    os.utime(dataset / "a.txt", (1_100_000_000, 1_100_000_000))
    (dataset / "c.txt").write_text("fresh\n", encoding="utf-8")
    os.remove(dataset / "b.json")

    service.index_session(session_id)

    # Scan order is a.png, b.png, c.png; b's deleted sidecar parses as none.
    assert loaded == ["a.txt", None, "c.txt"]

    updated = {item["file_name"]: item for item in service.list_images(session_id)["items"]}
    assert "night" in {tag["tag"] for tag in updated["a.png"]["tags"]}
    assert updated["a.png"]["sidecar_mtime"] == 1_100_000_000.0
    assert updated["c.png"]["sidecar_kind"] == "tag_txt"
    assert store.get_image(session_id, b_id)["sidecar_kind"] == "none"


def test_incremental_rescan_picks_up_added_and_removed_images(workspace):
    """New images are indexed and deleted images (plus their tags) pruned."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    items = {item["file_name"]: item for item in service.list_images(session_id)["items"]}
    b_id = int(items["b.png"]["id"])

    _make_image(dataset, "added.png")
    (dataset / "added.txt").write_text("fresh\n", encoding="utf-8")
    os.remove(dataset / "b.png")
    os.remove(dataset / "b.json")

    service.index_session(session_id)

    names = {item["file_name"] for item in service.list_images(session_id)["items"]}
    assert names == {"a.png", "c.png", "added.png"}
    refreshed = service.get_session(session_id)
    assert refreshed["status"] == "ready"
    assert refreshed["image_count"] == 3
    assert store.get_image(session_id, b_id) is None
    added_tags = {
        item["file_name"]: {tag["tag"] for tag in item["tags"]}
        for item in service.list_images(session_id)["items"]
    }
    assert added_tags["added.png"] == {"fresh"}
# -- 1.10.5 review regressions: lock-window races, mtime guards, limits -------


def test_save_after_concurrent_delete_rechecks_session_under_lock(workspace, monkeypatch):
    """delete_session serializes through the same lock and can win the race
    between save_image's pre-lock session fetch and the lock acquisition: the
    write span must re-check and 404, never write or journal into a deleted
    session."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]
    real_require = editing.require_session
    deleted = {"done": False}

    def require_and_delete(store_arg, sid):
        fetched = real_require(store_arg, sid)
        if not deleted["done"]:
            deleted["done"] = True
            service.delete_session(sid)  # wins the race before the lock
        return fetched

    monkeypatch.setattr(editing, "require_session", require_and_delete)

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            session_id, a_id, ImageEditRequest(content=TagTxtContent(tags=["solo"]))
        )
    assert excinfo.value.code == "dataset_not_found"
    assert excinfo.value.status_code == 404
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"
    assert store.journal_entries(session_id) == []


def test_batch_after_concurrent_delete_rechecks_session_under_lock(workspace, monkeypatch):
    """Same lock-window race for batch_operation: no sidecar is touched after
    the session disappeared."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]
    real_require = editing.require_session
    deleted = {"done": False}

    def require_and_delete(store_arg, sid):
        fetched = real_require(store_arg, sid)
        if not deleted["done"]:
            deleted["done"] = True
            service.delete_session(sid)
        return fetched

    monkeypatch.setattr(editing, "require_session", require_and_delete)

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[a_id]),
        )
    assert excinfo.value.code == "dataset_not_found"
    assert "fresh" not in (dataset / "a.txt").read_text(encoding="utf-8")
    assert store.journal_entries(session_id) == []


def test_delete_session_rejects_while_write_in_flight(workspace):
    """A delete during an in-flight write is refused with session_busy instead
    of yanking the dataset out from under the writer."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    lock = service._session_lock(session_id)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(TagManagerError) as excinfo:
            service.delete_session(session_id)
        assert excinfo.value.code == "session_busy"
        assert excinfo.value.retryable
        assert service.get_session(session_id)["id"] == session_id
    finally:
        lock.release()


def test_index_timeout_is_observable_on_the_session_row(workspace, monkeypatch, caplog):
    """A rescan that loses the lock race times out, logs loudly and records
    error/session_busy on the session row instead of silently skipping."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    monkeypatch.setattr(indexing, "INDEX_LOCK_TIMEOUT_SECONDS", 0.05)
    lock = service._session_lock(session_id)
    assert lock.acquire(blocking=False)
    try:
        with caplog.at_level(logging.WARNING, logger="tagger2.tag_manager"):
            service.index_session(session_id)  # must return, never raise
        assert "index busy" in caplog.text
        row = service.get_session(session_id)
        assert row["status"] == "error"
        assert row["error"] == "session_busy"
    finally:
        lock.release()

    # Once the lock is free, a normal rescan recovers the session.
    service.index_session(session_id)
    recovered = service.get_session(session_id)
    assert recovered["status"] == "ready"
    assert recovered["error"] is None


def test_batch_external_sidecar_change_conflicts_and_keeps_partial_journal(workspace):
    """A sidecar modified externally after the scan aborts the batch with a
    conflict; targets applied before it stay recoverable via the partial
    journal entry, and the external content is never clobbered."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    ids = _image_ids_by_name(service, session_id)
    a_id, b_id = ids["a.png"], ids["b.png"]

    (dataset / "b.json").write_text(
        json.dumps({**STANDARD_JSON, "tags": ["wolf", "night"]}), encoding="utf-8"
    )
    os.utime(dataset / "b.json", (1_000_000_000, 1_000_000_000))

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[a_id, b_id]),
        )
    assert excinfo.value.code == "sidecar_conflict"

    # a was applied before b aborted and is journalled as a partial entry.
    assert "fresh" in (dataset / "a.txt").read_text(encoding="utf-8")
    entry = store.latest_journal_entry(session_id, undone=False)
    assert entry["spec"]["partial"] is True
    assert [change["image_id"] for change in entry["changes"]] == [a_id]

    # b's externally changed content survived untouched.
    assert "night" in json.loads((dataset / "b.json").read_text(encoding="utf-8"))["tags"]

    assert service.undo(session_id)["reverted"] == 1
    assert "fresh" not in (dataset / "a.txt").read_text(encoding="utf-8")


def test_batch_externally_deleted_standard_json_is_never_degraded(workspace):
    """A standard JSON deleted after the scan must not be resurrected as a
    degraded batch-only document -- including when the index row carries no
    mtime stamp to compare."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    b_id = _image_ids_by_name(service, session_id)["b.png"]
    (dataset / "b.json").unlink()

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[b_id]),
        )
    assert excinfo.value.code == "sidecar_conflict"
    assert not (dataset / "b.json").exists()

    # The degradation hole: a standard_json row without a sidecar_mtime must
    # not pass the mtime guard and write a fresh batch-only document either.
    store.set_image_tags(
        b_id, [("wolf", "general")], sidecar_kind="standard_json", sidecar_mtime=None
    )
    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[b_id]),
        )
    assert excinfo.value.code == "sidecar_conflict"
    assert not (dataset / "b.json").exists()


def test_batch_reverifies_sidecar_mtime_immediately_before_write(workspace, monkeypatch):
    """An external change landing between the indexed-stamp check and the
    write is caught by the final re-verification, not clobbered."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    real_stat = editing.stat_mtime
    calls = {"n": 0}

    def counting_stat(path):
        calls["n"] += 1
        value = real_stat(Path(path))
        if calls["n"] == 3:
            # The pre-write re-verification observes a changed file.
            return None if value is None else value + 1.0
        return value

    monkeypatch.setattr(editing, "stat_mtime", counting_stat)

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[a_id]),
        )
    assert excinfo.value.code == "sidecar_conflict"
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"
    assert store.journal_entries(session_id) == []


def test_save_reverifies_sidecar_mtime_immediately_before_write(workspace, monkeypatch):
    """The single save carries the same final guard: a change that lands after
    the optimistic-concurrency validation but before the write still 409s."""

    service, store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    real_stat = editing.stat_mtime
    calls = {"n": 0}

    def counting_stat(path):
        calls["n"] += 1
        value = real_stat(Path(path))
        if calls["n"] == 2:
            # The pre-write re-verification observes a changed file.
            return None if value is None else value + 1.0
        return value

    monkeypatch.setattr(editing, "stat_mtime", counting_stat)

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            session_id, a_id, ImageEditRequest(content=TagTxtContent(tags=["solo"]))
        )
    assert excinfo.value.code == "sidecar_conflict"
    assert (dataset / "a.txt").read_text(encoding="utf-8") == "solo, wolf\n"
    assert store.journal_entries(session_id) == []


def test_batch_limit_rejects_above_max_for_both_target_shapes(workspace):
    """Batches above MAX_BATCH_IMAGES are rejected with 413 before any file is
    touched, whether the targets come from a filter or an id list."""

    service, store, session, _dataset = workspace
    session_id = str(session["id"])
    store.upsert_images(
        session_id,
        [
            {
                "relative_path": f"bulk/img_{i}.png",
                "file_name": f"img_{i}.png",
                "image_format": "png",
                "sidecar_kind": "none",
                "sidecar_path": None,
                "mtime": 100.0 + i,
                "sidecar_mtime": None,
                "width": 8,
                "height": 8,
                "tag_count": 0,
            }
            for i in range(MAX_BATCH_IMAGES + 50)
        ],
    )

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(
                op="add", tags=["fresh"], image_ids=None, filter=ImageFilter(sidecar="any")
            ),
        )
    assert excinfo.value.code == "batch_too_large"
    assert excinfo.value.status_code == 413

    ids = [int(item["id"]) for item in store.list_images(session_id, limit=MAX_BATCH_IMAGES + 50)[0]]
    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=ids),
        )
    assert excinfo.value.code == "batch_too_large"
    assert excinfo.value.status_code == 413
    assert store.journal_entries(session_id) == []


def test_save_result_reports_disk_sidecar_mtime(workspace):
    """The save response carries the fresh sidecar mtime the client must echo
    back for its next optimistic-concurrency check."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    result = service.save_image(
        session_id,
        a_id,
        ImageEditRequest(content=TagTxtContent(tags=["solo", "wolf", "rex"])),
    )
    assert result["sidecar_mtime"] == (dataset / "a.txt").stat().st_mtime

    # The reported value round-trips: saving with it succeeds, a stale one
    # conflicts.
    result2 = service.save_image(
        session_id,
        a_id,
        ImageEditRequest(
            content=TagTxtContent(tags=["solo"]),
            expected_sidecar_mtime=result["sidecar_mtime"],
        ),
    )
    assert result2["sidecar_mtime"] == (dataset / "a.txt").stat().st_mtime

    with pytest.raises(TagManagerError) as excinfo:
        service.save_image(
            session_id,
            a_id,
            ImageEditRequest(
                content=TagTxtContent(tags=["solo"]),
                expected_sidecar_mtime=result2["sidecar_mtime"] + 1.0,
            ),
        )
    assert excinfo.value.code == "sidecar_conflict"


def test_batch_resolves_image_ids_without_per_id_queries(workspace, monkeypatch):
    """Batch target resolution fetches all ids in one chunked query instead of
    one lookup (and connection) per id."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    ids = list(_image_ids_by_name(service, session_id).values())

    def forbidden(*args, **kwargs):
        raise AssertionError("batch target resolution must not query per id")

    monkeypatch.setattr(TagManagerStore, "get_image", forbidden)

    result = service.batch_operation(
        session_id, BatchOperationRequest(op="add", tags=["fresh"], image_ids=ids)
    )
    assert result["affected"] == len(ids)


def test_batch_missing_image_id_reports_404(workspace):
    """A missing id still fails the whole batch with image_not_found."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    a_id = _image_ids_by_name(service, session_id)["a.png"]

    with pytest.raises(TagManagerError) as excinfo:
        service.batch_operation(
            session_id,
            BatchOperationRequest(op="add", tags=["fresh"], image_ids=[a_id, 987_654]),
        )
    assert excinfo.value.code == "image_not_found"
    assert excinfo.value.status_code == 404
    assert "fresh" not in (dataset / "a.txt").read_text(encoding="utf-8")


def test_tag_filters_match_unicode_casefolded_spellings(workspace):
    """The SQL filter side folds through the registered CASEFOLD function and
    matches the Python normalization for non-ASCII tags (LOWER() folds ASCII
    only, so a filter typed with one spelling missed the other)."""

    service, _store, session, dataset = workspace
    session_id = str(session["id"])
    _make_image(dataset, "u1.png")
    _make_image(dataset, "u2.png")
    (dataset / "u1.txt").write_text("W\u00d6LFE, solo\n", encoding="utf-8")
    (dataset / "u2.txt").write_text("w\u00f6lfe, duo\n", encoding="utf-8")
    service.index_session(session_id)

    found = service.list_images(
        session_id, image_filter=ImageFilter(include_tags=["W\u00d6LFE"])
    )
    assert {item["file_name"] for item in found["items"]} == {"u1.png", "u2.png"}

    stats = service.tag_stats(session_id)
    counts = {entry["tag"]: entry["count"] for entry in stats}
    assert counts.get("W\u00d6LFE", counts.get("w\u00f6lfe")) == 2
    assert not ("W\u00d6LFE" in counts and "w\u00f6lfe" in counts)


def test_api_endpoints_answer_through_the_thread_offload(workspace):
    """The blocking routes keep their envelopes after the to_thread offload."""

    service, _store, session, _dataset = workspace
    app = FastAPI()
    app.include_router(create_tag_manager_router(service))
    client = TestClient(app)
    sid = str(session["id"])

    listed = client.get("/api/v1/tag-manager/datasets")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1

    images = client.get(f"/api/v1/tag-manager/datasets/{sid}/images")
    assert images.status_code == 200
    a = next(item for item in images.json()["items"] if item["file_name"] == "a.png")

    detail = client.get(f"/api/v1/tag-manager/datasets/{sid}/images/{a['id']}")
    assert detail.status_code == 200
    saved = client.patch(
        f"/api/v1/tag-manager/datasets/{sid}/images/{a['id']}",
        json={
            "content": {"kind": "tag_txt", "tags": ["solo", "wolf", "rex"]},
            "expected_sidecar_mtime": detail.json()["sidecar_mtime"],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["sidecar_kind"] == "tag_txt"
    assert saved.json()["sidecar_mtime"] is not None

    batch = client.post(
        f"/api/v1/tag-manager/datasets/{sid}/batch",
        json={"op": "add", "tags": ["night"], "image_ids": [int(a["id"])]},
    )
    assert batch.status_code == 200
    assert batch.json()["affected"] == 1

    undone = client.post(f"/api/v1/tag-manager/datasets/{sid}/undo")
    assert undone.status_code == 200
    assert undone.json()["reverted"] == 1

    missing = client.get("/api/v1/tag-manager/datasets/nope")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "dataset_not_found"


def test_api_batch_runs_off_the_event_loop_thread(workspace):
    """The batch route executes the blocking service call on a worker thread,
    never on the event loop."""

    service, _store, session, _dataset = workspace
    app = FastAPI()
    app.include_router(create_tag_manager_router(service))
    client = TestClient(app)
    sid = str(session["id"])
    a_id = _image_ids_by_name(service, sid)["a.png"]
    seen: dict[str, bool] = {}
    original = service.batch_operation

    def batch_spy(session_id_arg, request):
        try:
            asyncio.get_running_loop()
            seen["off_loop"] = False
        except RuntimeError:
            seen["off_loop"] = True
        return original(session_id_arg, request)

    service.batch_operation = batch_spy

    response = client.post(
        f"/api/v1/tag-manager/datasets/{sid}/batch",
        json={"op": "add", "tags": ["night"], "image_ids": [a_id]},
    )
    assert response.status_code == 200
    assert response.json()["affected"] == 1
    assert seen["off_loop"] is True


def test_api_create_and_refresh_schedule_on_the_event_loop_thread(tmp_path):
    """create/refresh keep their event-loop semantics: the scan is queued via
    the running loop (never inline in a worker thread) and the 202 answers
    while the session becomes ready asynchronously."""

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")
    (dataset / "a.txt").write_text("solo, wolf\n", encoding="utf-8")
    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="loop-root", kind="input", writable=True)
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
    )
    seen = {"on_loop": 0, "inline": 0}
    original_schedule = service.schedule_index

    def schedule_spy(session_id):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] += 1
        except RuntimeError:
            seen["inline"] += 1
        original_schedule(session_id)

    service.schedule_index = schedule_spy

    app = FastAPI()
    app.include_router(create_tag_manager_router(service))
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/tag-manager/datasets",
            json={"root_id": "loop-root", "relative_path": "", "profile": "e621"},
        )
        assert created.status_code == 202
        sid = created.json()["id"]
        deadline = time.monotonic() + 10.0
        status = ""
        while time.monotonic() < deadline:
            status = client.get(f"/api/v1/tag-manager/datasets/{sid}").json()["status"]
            if status == "ready":
                break
            time.sleep(0.02)
        assert status == "ready"

        _make_image(dataset, "later.png")
        (dataset / "later.txt").write_text("fresh\n", encoding="utf-8")
        refreshed = client.post(f"/api/v1/tag-manager/datasets/{sid}/refresh")
        assert refreshed.status_code == 202
        deadline = time.monotonic() + 10.0
        names = set()
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/tag-manager/datasets/{sid}/images").json()
            names = {item["file_name"] for item in payload["items"]}
            if "later.png" in names:
                break
            time.sleep(0.02)
        assert "later.png" in names

    assert seen["on_loop"] == 2
    assert seen["inline"] == 0


def test_delete_session_from_worker_thread_cancels_queued_scan(workspace):
    """delete_session off the loop thread (the API now runs it via to_thread)
    still cancels a queued scan: the asyncio cancel is marshalled onto the
    loop that owns the future."""

    service, _store, session, _dataset = workspace
    session_id = str(session["id"])
    block = threading.Event()

    async def drive():
        loop = asyncio.get_running_loop()
        # Saturate the default executor so the scheduled scan stays queued.
        occupied = [loop.run_in_executor(None, block.wait) for _ in range(64)]
        service.schedule_index(session_id)
        future = service._index_futures[session_id]
        worker = threading.Thread(target=service.delete_session, args=(session_id,))
        worker.start()
        worker.join()
        deadline = time.monotonic() + 5.0
        while not future.cancelled() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert future.cancelled(), "a queued scan must be cancelled on delete"
        block.set()
        await asyncio.gather(*occupied)

    asyncio.run(drive())
