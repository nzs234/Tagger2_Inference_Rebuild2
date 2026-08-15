"""Tests for manual full-path workflow binding."""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def workflow_client(monkeypatch):
    from backend.tagger2.config import AppConfig, reset_settings_cache
    from backend.tagger2.main import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "input").mkdir()
        (root / "output").mkdir()
        (root / "models").mkdir()
        monkeypatch.setenv("TAGGER2_DATA_DIR", str(root / "data"))
        monkeypatch.setenv("TAGGER2_CACHE_DIR", str(root / "cache"))
        monkeypatch.setenv("TAGGER2_LOG_DIR", str(root / "logs"))
        monkeypatch.setenv("TAGGER2_ALLOW_LAN", "0")
        reset_settings_cache()
        settings = AppConfig.from_env()
        settings.ensure_directories()
        app = create_app(settings)
        runtime = app.state.runtime
        input_root = runtime.allowlist.register(
            root / "input", kind="input", root_id="wfinput", label="Workflow input"
        )
        runtime.allowlist.register(
            root / "output", kind="output", root_id="wfoutput", label="Workflow output", writable=True
        )
        with TestClient(app) as client:
            yield client, root, input_root
        reset_settings_cache()


def test_manual_path_preview_and_binding(workflow_client):
    client, root, _input_root = workflow_client
    source = root / "input" / "dataset"
    source.mkdir()
    output = root / "output" / "processed"

    preview = client.post(
        "/api/v1/workflows/path-bindings/preview",
        json={
            "source_path": str(source),
            "output_path": str(output),
            "work_mode": "full_copy",
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "create_required"
    assert preview.json()["output_create_required"] is True
    assert not output.exists()

    bound = client.post(
        "/api/v1/workflows/path-bindings",
        json={
            "source_path": str(source),
            "output_path": str(output),
            "work_mode": "full_copy",
            "create_output": True,
        },
    )
    assert bound.status_code == 200, bound.text
    body = bound.json()
    assert body["source"]["relative_path"] == "dataset"
    assert body["output"]["relative_path"] == "processed"
    assert body["output_created"] is True
    assert output.is_dir()
    assert str(root) not in bound.text

    again = client.post(
        "/api/v1/workflows/path-bindings",
        json={
            "source_path": str(source),
            "output_path": str(output),
            "work_mode": "full_copy",
        },
    )
    assert again.status_code == 200
    assert again.json()["source"]["root_id"] == body["source"]["root_id"]
    assert again.json()["output"]["root_id"] == body["output"]["root_id"]


def test_manual_path_binding_rejects_overlap(workflow_client):
    client, root, _input_root = workflow_client
    source = root / "input"
    response = client.post(
        "/api/v1/workflows/path-bindings/preview",
        json={
            "source_path": str(source),
            "output_path": str(source / "generated"),
            "work_mode": "full_copy",
        },
    )
    assert response.status_code == 200
    assert "source_output_overlap" in response.json()["errors"]


def test_manual_path_binding_rejects_relative_source(workflow_client):
    client, _root, _input_root = workflow_client
    response = client.post(
        "/api/v1/workflows/path-bindings/preview",
        json={"source_path": "dataset", "work_mode": "in_place"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "not_applicable"
    assert "absolute path" in response.json()["errors"][0]
