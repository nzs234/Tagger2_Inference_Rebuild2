"""Tests for the on-demand workflow resource fetcher."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from tagger2.tag_manager.tag_db import TagDatabase, TagDatabaseError
from tagger2.workflow.resource_fetch import (
    ResourceFetchManager,
    manager_for,
)
from tagger2.workflow.resources import WorkflowResourceCatalog


SNAPSHOT = {
    "format": "classify-snapshot-v1",
    "profile": "e621",
    "source": {"url": "https://e621.net/db_export/", "timestamp": "2026-08-12"},
    "tags": [{"name": "general", "category": "general", "post_count": 1}],
    "aliases": [],
    "implications": [],
}
PAYLOAD = json.dumps(SNAPSHOT).encode("utf-8")
FINGERPRINT = hashlib.sha256(PAYLOAD).hexdigest()
RESOURCE_ID = "classify-e621-test-v1"


def _make_catalog(tmp_path: Path) -> WorkflowResourceCatalog:
    catalog = WorkflowResourceCatalog(tmp_path / "resources")
    catalog.import_resource(
        _payload_file(tmp_path, PAYLOAD),
        RESOURCE_ID,
        "classify",
        profile="e621",
    )
    return catalog


def _payload_file(tmp_path: Path, payload: bytes) -> Path:
    source = tmp_path / f"source-{len(payload)}.bin"
    source.write_bytes(payload)
    return source


def _fake_client_factory(payload: bytes, calls: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/missing/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=payload)

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    return factory


def _blob_name(catalog: WorkflowResourceCatalog, resource_id: str) -> str:
    manifest = catalog.get_manifest(resource_id)
    assert manifest is not None
    return f"{resource_id}.{manifest.resource_fingerprint[:16]}"


def test_download_verifies_and_places_blob(tmp_path: Path) -> None:
    catalog = _make_catalog(tmp_path)
    blob = catalog.resource_dir / "classify" / _blob_name(catalog, RESOURCE_ID)
    blob.unlink()  # manifest ships, blob does not
    calls: list[str] = []
    manager = ResourceFetchManager(
        catalog,
        base_url="https://example.invalid/assets",
        client_factory=_fake_client_factory(PAYLOAD, calls),
    )

    state = manager.get_or_start(RESOURCE_ID)
    assert state.done.wait(timeout=10)
    assert state.state == "ready"
    assert state.path == blob
    assert blob.is_file()
    assert [url for url in calls if url.endswith(_blob_name(catalog, RESOURCE_ID))]
    # Repeated calls never re-download.
    again = manager.get_or_start(RESOURCE_ID)
    assert again.state == "ready" and again.path == blob
    assert len([url for url in calls if url.endswith(_blob_name(catalog, RESOURCE_ID))]) == 1


def test_download_rejects_fingerprint_mismatch(tmp_path: Path) -> None:
    catalog = _make_catalog(tmp_path)
    blob = catalog.resource_dir / "classify" / _blob_name(catalog, RESOURCE_ID)
    blob.unlink()
    calls: list[str] = []
    # Same length as the manifest size so the size check passes and the
    # fingerprint check is what rejects the content.
    corrupted = bytes([PAYLOAD[0] ^ 0xFF]) + PAYLOAD[1:]
    manager = ResourceFetchManager(
        catalog,
        base_url="https://example.invalid/assets",
        client_factory=_fake_client_factory(corrupted, calls),
    )

    state = manager.get_or_start(RESOURCE_ID)
    assert state.done.wait(timeout=10)
    assert state.state == "error"
    assert "指纹" in state.error
    assert not blob.exists()
    assert not Path(str(blob) + ".part").exists()


def test_download_handles_missing_host_asset(tmp_path: Path) -> None:
    catalog = _make_catalog(tmp_path)
    blob = catalog.resource_dir / "classify" / _blob_name(catalog, RESOURCE_ID)
    blob.unlink()
    calls: list[str] = []
    # Point the fetch at a URL that 404s (no matching asset on the host).
    manager = ResourceFetchManager(
        catalog,
        base_url="https://example.invalid/missing",
        client_factory=_fake_client_factory(PAYLOAD, calls),
    )

    state = manager.get_or_start(RESOURCE_ID)
    assert state.done.wait(timeout=10)
    assert state.state == "error"
    assert not blob.exists()


def test_manager_is_shared_per_resource_dir(tmp_path: Path) -> None:
    catalog = WorkflowResourceCatalog(tmp_path / "resources")
    assert manager_for(catalog) is manager_for(catalog)
    other = WorkflowResourceCatalog(tmp_path / "resources2")
    assert manager_for(other) is not manager_for(catalog)


def test_tag_database_triggers_fetch_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_loaded starts the fetch, reports progress, and loads once ready."""

    catalog = _make_catalog(tmp_path)
    blob = catalog.resource_dir / "classify" / _blob_name(catalog, RESOURCE_ID)
    blob.unlink()
    calls: list[str] = []
    manager = ResourceFetchManager(
        catalog,
        base_url="https://example.invalid/assets",
        client_factory=_fake_client_factory(PAYLOAD, calls),
    )
    monkeypatch.setattr(
        "tagger2.workflow.resource_fetch.manager_for", lambda _catalog: manager
    )
    database = TagDatabase(catalog=catalog)

    # First use starts the background fetch and surfaces progress.
    with pytest.raises(TagDatabaseError) as excinfo:
        database.ensure_loaded("e621")
    assert "后台下载" in str(excinfo.value)

    state = manager.state_for(RESOURCE_ID)
    assert state is not None
    assert state.done.wait(timeout=10)
    assert state.state == "ready"

    # Once the blob landed, loading works without any further setup.
    database.ensure_loaded("e621")
    assert database.available_profiles().get("e621")
    assert database.lookup("e621", "general") is not None


def test_tag_database_reports_downloading_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the fetch is in flight the error carries progress guidance."""

    catalog = _make_catalog(tmp_path)
    blob = catalog.resource_dir / "classify" / _blob_name(catalog, RESOURCE_ID)
    blob.unlink()
    manifest = catalog.get_manifest(RESOURCE_ID)
    assert manifest is not None

    class StubManager:
        def __init__(self) -> None:
            from tagger2.workflow.resource_fetch import ResourceFetchState

            self.state = ResourceFetchState(
                state="downloading", received=manifest.size_bytes // 2, total=manifest.size_bytes
            )

        def get_or_start(self, resource_id: str):
            return self.state

    monkeypatch.setattr(
        "tagger2.workflow.resource_fetch.manager_for", lambda _catalog: StubManager()
    )
    database = TagDatabase(catalog=catalog)
    with pytest.raises(TagDatabaseError) as excinfo:
        database.ensure_loaded("e621")
    assert "后台下载" in str(excinfo.value)
    assert "50%" in str(excinfo.value)


def test_manifest_documents_asset_host(tmp_path: Path) -> None:
    """The manifest carries everything the fetcher needs (id, size, digest)."""

    catalog = _make_catalog(tmp_path)
    manifest = catalog.get_manifest(RESOURCE_ID)
    assert manifest is not None
    assert manifest.size_bytes == len(PAYLOAD)
    assert manifest.resource_fingerprint == FINGERPRINT
    data = json.loads((catalog.resource_dir / "classify" / f"{RESOURCE_ID}.manifest.json").read_text())
    assert data["resource_id"] == RESOURCE_ID
