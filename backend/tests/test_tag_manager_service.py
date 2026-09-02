"""Service-level tests for the tag manager: index, edit, batch, undo/redo, routes."""

import json
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
    TagTxtContent,
)
from tagger2.tag_manager.service import TagManagerError, TagManagerService
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
