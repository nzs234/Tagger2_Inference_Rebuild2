"""Service-level tests for the tag manager: index, edit, batch, undo/redo, routes."""

import json
import logging
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tagger2.security import PathAllowlist
from tagger2.tag_manager.api import create_tag_manager_router
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
