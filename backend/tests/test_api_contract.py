from __future__ import annotations

import io
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from tagger2.config import AppConfig
from tagger2.anima import parse_anima_response
from tagger2.main import create_app


def _client(tmp_path: Path, **overrides):
    settings = AppConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        production=True,
        **overrides,
    )
    return TestClient(create_app(settings))


def test_api_errors_use_one_structured_envelope(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        prompt_defaults = client.get("/api/v1/prompts/defaults")
        assert prompt_defaults.status_code == 200
        assert set(prompt_defaults.json()) == {"tag_prompt", "nl_prompt", "json_prompt"}
        assert "booru-style tags" in prompt_defaults.json()["tag_prompt"]
        assert "Field guidance:" in prompt_defaults.json()["json_prompt"]
        assert "Placement rules:" in prompt_defaults.json()["json_prompt"]

        invalid_response = client.post(
            "/api/v1/jobs",
            json={
                "mode": "online",
                "source": {"type": "upload", "upload_id": "missing"},
                "provider_id": "gemini",
                "online_response": "nl",
                "output": {"json": True, "txt": False},
            },
        )
        assert invalid_response.status_code == 400
        assert invalid_response.json()["code"] == "invalid_online_response"

        validation = client.post("/api/v1/scans", json={})
        assert validation.status_code == 422
        assert validation.json()["code"] == "validation_error"
        assert validation.json()["fields"]
        assert validation.json()["request_id"]
        assert validation.json()["retryable"] is False

        missing = client.get("/api/v1/jobs/not-found")
        assert missing.status_code == 404
        assert missing.json()["code"] == "job_not_found"
        assert "detail" not in missing.json()

        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_failed_upload_batch_removes_partial_files(tmp_path: Path) -> None:
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, format="PNG")

    with _client(tmp_path) as client:
        response = client.post(
            "/api/v1/uploads",
            files=[
                ("files", ("valid.png", stream.getvalue(), "image/png")),
                ("files", ("broken.png", b"not-an-image", "image/png")),
            ],
        )

        assert response.status_code == 400
        assert client.app.state.runtime.upload_index == {}
        upload_root = tmp_path / "data" / "uploads"
        assert not [path for path in upload_root.rglob("*") if path.is_file()]
        assert not [path for path in upload_root.iterdir() if path.is_dir()]


def test_scanned_job_defaults_to_source_folder(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    Image.new("RGB", (8, 8), "white").save(images / "one.png")

    with _client(tmp_path) as client:
        root = client.post(
            "/api/v1/roots",
            json={"name": "images", "kind": "input", "path": str(images)},
        ).json()
        response = client.post(
            "/api/v1/jobs",
            json={
                "mode": "online",
                "source": {"type": "scan", "root_id": root["id"]},
                "provider_id": "gemini",
                "output": {"json": True, "txt": False},
            },
        )
        assert response.status_code == 200
        job = response.json()
        assert job["output_root_id"] is None


def test_registered_roots_persist_without_exposing_paths(tmp_path: Path) -> None:
    allowed = tmp_path / "private-images"
    allowed.mkdir()
    settings_file = tmp_path / "data" / "settings.json"

    with _client(tmp_path) as client:
        created_response = client.post(
            "/api/v1/roots",
            json={"name": "Training images", "kind": "input", "path": str(allowed)},
        )
        assert created_response.status_code == 200
        created = created_response.json()
        duplicate = client.post(
            "/api/v1/roots",
            json={"name": "Training images", "kind": "input", "path": str(allowed)},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["id"] == created["id"]

        conflict = client.post(
            "/api/v1/roots",
            json={"name": "Wrong kind", "kind": "output", "path": str(allowed)},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "root_conflict"

        updated = client.put("/api/v1/settings", json={"default_mode": "local"})
        assert updated.status_code == 200
        assert updated.json()["default_mode"] == "local"
        public_payload = [created, client.get("/api/v1/roots").json(), updated.json()]
        exposed = json.dumps(public_payload).replace("\\\\", "\\").casefold()
        assert str(allowed).casefold() not in exposed

    document = json.loads(settings_file.read_text(encoding="utf-8"))
    assert document["default_mode"] == "local"
    assert len(document["roots"]) == 1
    assert document["roots"][0]["path"] == str(allowed.resolve())
    assert not list(settings_file.parent.glob(".settings.json.*.tmp"))

    # Duplicate and malformed entries in a hand-edited file are ignored.
    document["roots"].extend(
        [
            dict(document["roots"][0]),
            {"root_id": document["roots"][0]["root_id"], "path": str(tmp_path)},
            "invalid",
        ]
    )
    settings_file.write_text(json.dumps(document), encoding="utf-8")

    with _client(tmp_path) as restarted:
        public_roots = restarted.get("/api/v1/roots").json()["items"]
        restored = [root for root in public_roots if root["id"] == created["id"]]
        assert len(restored) == 1
        assert restarted.get("/api/v1/settings").json()["default_mode"] == "local"
        scan = restarted.post(
            "/api/v1/scans",
            json={"root_id": created["id"], "recursive": False},
        )
        assert scan.status_code == 200
        rewritten = restarted.put(
            "/api/v1/settings", json={"default_mode": "online"}
        )
        assert rewritten.status_code == 200
        assert str(allowed).casefold() not in rewritten.text.replace("\\\\", "\\").casefold()

    rewritten_document = json.loads(settings_file.read_text(encoding="utf-8"))
    assert len(rewritten_document["roots"]) == 1


def test_corrupt_settings_file_does_not_break_root_registration(tmp_path: Path) -> None:
    settings_file = tmp_path / "data" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text("{not-json", encoding="utf-8")
    allowed = tmp_path / "recovered-root"
    allowed.mkdir()

    with _client(tmp_path) as client:
        assert client.get("/api/v1/settings").status_code == 200
        response = client.post(
            "/api/v1/roots",
            json={"name": "Recovered", "kind": "input", "path": str(allowed)},
        )
        assert response.status_code == 200
        root_id = response.json()["id"]

    recovered = json.loads(settings_file.read_text(encoding="utf-8"))
    assert [root["root_id"] for root in recovered["roots"]] == [root_id]


def test_provider_url_rejects_query_credentials(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/v1/providers",
            json={
                "name": "unsafe",
                "kind": "openai",
                "base_url": "https://example.test/v1?api_key=secret",
                "primary_model": "vision",
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_provider_url"
        assert "secret" not in response.text


def test_model_download_rejects_non_huggingface_url(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/v1/models/downloads",
            json={"url": "https://example.com/owner/model"},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_huggingface_url"


def test_model_category_thresholds_are_public_persistent_and_resettable(tmp_path: Path) -> None:
    model_dir = tmp_path / "models" / "category-tagger"
    model_dir.mkdir(parents=True)
    (model_dir / "model.onnx").write_bytes(b"onnx")
    (model_dir / "selected_tags.csv").write_text(
        "name,category,best_threshold\nportrait,general,0.42\nhero,character,0.71\n",
        encoding="utf-8",
    )

    with _client(tmp_path) as client:
        models = client.get("/api/v1/models").json()["items"]
        model = next(item for item in models if item["name"] == "category-tagger")
        assert model["threshold_source"] == "model"
        assert model["thresholds"]["default"] == pytest.approx(0.42)
        assert model["thresholds"]["general"] == pytest.approx(0.42)
        assert model["thresholds"]["character"] == pytest.approx(0.71)
        changed = client.patch(
            f"/api/v1/models/{model['id']}",
            json={"thresholds": {"general": 0.5, "character": 0.8}},
        )
        assert changed.status_code == 200
        assert changed.json()["threshold_source"] == "custom"

    with _client(tmp_path) as restarted:
        model = next(
            item
            for item in restarted.get("/api/v1/models").json()["items"]
            if item["name"] == "category-tagger"
        )
        assert model["thresholds"]["general"] == pytest.approx(0.5)
        assert model["thresholds"]["character"] == pytest.approx(0.8)
        reset = restarted.patch(
            f"/api/v1/models/{model['id']}",
            json={"reset_thresholds": True},
        )
        assert reset.json()["threshold_source"] == "model"
        assert reset.json()["thresholds"]["general"] == pytest.approx(0.42)


def test_provider_type_is_editable_and_deleted_defaults_stay_deleted(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        created = client.post(
            "/api/v1/providers",
            json={
                "name": "NewAPI gateway",
                "kind": "custom",
                "protocol": "openai",
                    "base_url": "https://example.com/v1",
                "primary_model": "vision-a",
            },
        )
        assert created.status_code == 200, created.text
        profile = created.json()
        assert profile["kind"] == "custom"
        assert profile["protocol"] == "openai"

        changed = client.patch(
            f"/api/v1/providers/{profile['id']}",
            json={
                "kind": "claude",
                "protocol": "gemini",
                "base_url": "https://api.anthropic.com",
                "primary_model": "claude-sonnet-4-5",
                "fallback_model": None,
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["kind"] == "claude"
        assert changed.json()["protocol"] == "claude"
        assert changed.json()["primary_model"] == "claude-sonnet-4-5"

        assert client.delete(f"/api/v1/providers/{profile['id']}").status_code == 204
        assert client.delete("/api/v1/providers/gemini").status_code == 204
        ids = {item["id"] for item in client.get("/api/v1/providers").json()["items"]}
        assert profile["id"] not in ids
        assert "gemini" not in ids

    with _client(tmp_path) as restarted:
        ids = {item["id"] for item in restarted.get("/api/v1/providers").json()["items"]}
        assert "gemini" not in ids


def test_unsaved_provider_discovery_uses_ephemeral_keys_and_closes_client(tmp_path: Path, monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeProvider:
        async def discover_models(self) -> list[str]:
            return ["model-b", "model-a", "model-a"]

        async def aclose(self) -> None:
            calls["closed"] = True

    def fake_create_provider(config):
        calls["config"] = config
        return FakeProvider()

    monkeypatch.setattr("tagger2.main.create_provider", fake_create_provider)
    with _client(tmp_path) as client:
        before_ids = {item["id"] for item in client.get("/api/v1/providers").json()["items"]}
        response = client.post(
            "/api/v1/providers/discover-models",
            json={
                "kind": "custom",
                "protocol": "openai",
                    "base_url": "https://example.com/v1",
                "api_keys": ["key-a", "key-b", "key-a"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "items": [
                {"id": "model-b", "name": "model-b"},
                {"id": "model-a", "name": "model-a"},
                {"id": "model-a", "name": "model-a"},
            ]
        }
        after_ids = {item["id"] for item in client.get("/api/v1/providers").json()["items"]}
        assert after_ids == before_ids

    config = calls["config"]
    assert config["api_keys"] == ("key-a", "key-b")
    assert config["model"] == "discovery"
    assert calls["closed"] is True
    assert "key-a" not in response.text
    assert "key-b" not in response.text


def test_unsaved_provider_discovery_rejects_invalid_url(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/v1/providers/discover-models",
            json={
                "kind": "custom",
                "protocol": "openai",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_keys": ["secret-key"],
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_provider_url"
        assert "secret-key" not in response.text


def test_lan_token_is_header_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_TAGGER2_TOKEN", "correct-token")
    with _client(
        tmp_path,
        host="0.0.0.0",
        allow_lan=True,
        access_token_env="TEST_TAGGER2_TOKEN",
    ) as client:
        leaked = client.get("/api/v1/health?access_token=correct-token")
        assert leaked.status_code == 401
        allowed = client.get(
            "/api/v1/health",
            headers={"Authorization": "Bearer correct-token"},
        )
        assert allowed.status_code == 200


def test_mocked_online_job_writes_named_hash_tracked_artifacts(tmp_path: Path) -> None:
    class FakeProvider:
        model = "mock-vision"

        def __init__(self) -> None:
            self.calls = 0

        async def generate_anima(self, image, prompt, **kwargs):
            self.calls += 1
            return parse_anima_response(
                '{"quality":["highres"],"count":"solo","character":"",'
                '"series":"demo_series","artist":"","appearance":["red_hair"],'
                '"tags":["digital_art"],"environment":["indoor_scene"],'
                '"nl":"A_red-haired subject indoors."}'
            )

    stream = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(stream, format="PNG")
    fake = FakeProvider()

    with _client(tmp_path) as client:
        runtime = client.app.state.runtime
        runtime.provider = lambda *_args, **_kwargs: fake
        upload = client.post(
            "/api/v1/uploads",
            files={"files": ("sample.png", stream.getvalue(), "image/png")},
        ).json()
        created = client.post(
            "/api/v1/jobs",
            json={
                "mode": "online",
                "source": {"type": "upload", "upload_id": upload["upload_id"]},
                "provider_id": "gemini",
                "online_concurrency": 7,
                "output": {
                    "json": True,
                    "txt": True,
                    "txt_include_tags": True,
                    "replace_underscores": True,
                    "conflict": "overwrite",
                },
            },
        )
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]
        stored_job = runtime.storage.get_job(job_id)
        assert stored_job is not None
        assert stored_job.config["_worker_concurrency"] == 7
        assert stored_job.config["provider_snapshot"]["config"]["max_concurrency"] == 7
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)

        assert job["state"] == "succeeded"
        result = client.get(f"/api/v1/jobs/{job_id}/results").json()["items"][0]
        assert fake.calls == 1
        assert result["anima"]["appearance"] == ["red hair"]
        assert result["anima"]["tags"] == ["digital art"]
        assert result["anima"]["environment"] == ["indoor scene"]
        assert result["caption"] == "A_red-haired subject indoors."
        assert {artifact["path"] for artifact in result["artifacts"]} == {
            "sample.json",
            "sample.txt",
        }
        item = runtime.storage.list_items(job_id)[0]
        stored_artifacts = runtime.storage.list_artifacts(item.id)
        assert {artifact.kind for artifact in stored_artifacts} == {
            "anima_json",
            "anima_txt",
        }
        json_artifact = next(value for value in stored_artifacts if value.kind == "anima_json")
        txt_artifact = next(value for value in stored_artifacts if value.kind == "anima_txt")
        document = json.loads(Path(json_artifact.path).read_text(encoding="utf-8"))
        assert document["series"] == "demo series"
        assert document["nl"] == "A_red-haired subject indoors."
        assert "red hair" in Path(txt_artifact.path).read_text(encoding="utf-8")
