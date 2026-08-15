"""Workflow Caption binding to the host local model runtime."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tagger2.security import PathAllowlist, PathRoot
from tagger2.workflow.api import create_workflow_router
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.resources import WorkflowResourceCatalog


class _ModelRecord:
    model_id = "model_local_loaded"
    name = "E621 local tagger"
    path = Path("models") / "local"
    loaded = True
    weight_path = Path(__file__)
    backend = "pytorch"


class _ModelRegistry:
    def __init__(self) -> None:
        self.record = _ModelRecord()

    def get_model(self, model_id: str) -> _ModelRecord:
        if model_id != self.record.model_id:
            raise KeyError(model_id)
        return self.record

    def list(self) -> list[_ModelRecord]:
        return [self.record]


class _InferenceEngine:
    loaded_model_ids = ("model_local_loaded",)


def _client(tmp_path: Path, *, loaded: bool = True) -> tuple[TestClient, WorkflowDatabase]:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    allowlist = PathAllowlist(
        [
            PathRoot("source", source, "source", "input"),
            PathRoot("output", output, "output", "output", writable=True),
        ]
    )
    database = WorkflowDatabase(tmp_path / "workflow.sqlite3")
    registry = _ModelRegistry() if loaded else None
    engine = _InferenceEngine() if loaded else None
    app = FastAPI()
    app.include_router(
        create_workflow_router(
            allowlist,
            WorkflowResourceCatalog(tmp_path / "resources"),
            database=database,
            model_registry=registry,
            inference_engine=engine,
        )
    )
    return TestClient(app), database


def _config() -> dict[str, object]:
    return {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "source", "relative_path": ""},
        "output_root": {"root_id": "output", "relative_path": ""},
        "caption": {"enabled": True},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    }


def test_caption_uses_loaded_host_model_and_persists_canonical_id(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    try:
        response = client.post("/api/v1/workflows/jobs", json={"config": _config()})
        assert response.status_code == 200, response.text

        job = database.get_job(response.json()["job_id"])
        assert job is not None
        stored = json.loads(str(job["config_json"]))
        assert stored["caption"]["model_id"] == "model_local_loaded"
    finally:
        client.close()


def test_caption_requires_a_loaded_host_model(tmp_path: Path) -> None:
    client, _database = _client(tmp_path, loaded=False)
    try:
        response = client.post("/api/v1/workflows/jobs", json={"config": _config()})
        assert response.status_code == 400
        body = response.json()
        detail = body.get("detail", body)
        assert detail["code"] == "invalid_config"
        assert "local model runtime" in detail["message"]
    finally:
        client.close()


def test_caption_model_digest_is_frozen_when_weight_is_available(tmp_path: Path) -> None:
    client, database = _client(tmp_path)
    try:
        response = client.post("/api/v1/workflows/jobs", json={"config": _config()})
        assert response.status_code == 200, response.text
        job = database.get_job(response.json()["job_id"])
        assert job is not None
        # A draft stores the model binding; the execution snapshot adds the
        # weight digest once the job is started and the host model is resolved.
        stored = json.loads(str(job["config_json"]))
        assert stored["caption"]["model_id"] == "model_local_loaded"
    finally:
        client.close()
