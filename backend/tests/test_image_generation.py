from __future__ import annotations

import asyncio
import base64
import io
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tagger2.image_generation.capabilities import (
    GOOGLE_BASE_RATIOS,
    capabilities_for,
    capability_for_style,
    capability_object,
)
from tagger2.image_generation.client import ImageGenerationClient, ImageRequest
from tagger2.image_generation.client import ImageCallResult, ResolvedImage
from tagger2.image_generation.contracts import ImageJobConfig
from tagger2.image_generation.parser import parse_response
from tagger2.image_generation.service import ImageGenerationService
from tagger2.image_generation.storage import ImageGenerationStorage
from tagger2.providers import APIKeyPool, ProviderConfig, prepare_image
from tagger2.config import AppConfig
from tagger2.main import create_app
from tagger2.security import SecurityError, validate_provider_url
from tagger2.storage import canonical_json


def _prepared() -> object:
    stream = io.BytesIO()
    Image.new("RGB", (8, 6), (32, 64, 96)).save(stream, format="PNG")
    return prepare_image(stream.getvalue(), max_bytes=64 * 1024)


def _client(*, family: str, style: str, model: str) -> ImageGenerationClient:
    capability = capability_object(
        kind="xai" if family == "xai_grok_image" else "gemini" if family == "google_gemini" else "openai",
        protocol="gemini" if family == "google_gemini" else "openai",
        model=model,
        configured_family=family,
    )
    return ImageGenerationClient(
        ProviderConfig(
            kind="gemini" if family == "google_gemini" else "openai",
            base_url="https://provider.example/v1",
            model=model,
            api_key="secret",
        ),
        family=family,
        base_url="https://provider.example/v1",
        api_style=style,
        capability=capability,
        max_output_bytes=4 * 1024 * 1024,
        max_pixels=4_000_000,
        max_edge=4096,
    )


def test_capability_registry_recognises_requested_model_families():
    assert capabilities_for(kind="xai", protocol="openai", model="grok-2-image-1212")["family"] == "xai_grok_image"
    assert capabilities_for(kind="gemini", protocol="gemini", model="gemini-3-pro-image")["family"] == "google_gemini"
    assert capabilities_for(kind="openai", protocol="openai", model="gpt-image-1")["family"] == "openai_gpt_image"


def test_google_native_request_contains_image_modalities_and_config():
    client = _client(family="google_gemini", style="native", model="gemini-3-pro-image")
    endpoint, payload, files = client._build_request(
        ImageRequest(
            model="gemini-3-pro-image",
            prompt="a red fox",
            operation="edit",
            n=2,
            aspect_ratio="16:9",
            image_size="2K",
            include_text_modality=True,
            multi_image_strategy="candidate_count",
        ),
        [_prepared()],
    )
    assert endpoint.endswith("/models/gemini-3-pro-image:generateContent")
    assert files is None
    generation = payload["generationConfig"]
    assert generation["responseModalities"] == ["IMAGE", "TEXT"]
    assert generation["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert generation["candidateCount"] == 2
    assert payload["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/jpeg"


def test_openai_and_xai_image_requests_use_generation_and_edit_endpoints():
    openai = _client(family="openai_gpt_image", style="openai_images", model="gpt-image-1")
    endpoint, payload, files = openai._build_request(
        ImageRequest(model="gpt-image-1", prompt="a city", operation="generate", n=1, size="1024x1024", quality="high"),
        [],
    )
    assert endpoint.endswith("/v1/images/generations")
    assert payload["quality"] == "high"
    assert files is None

    xai = _client(family="xai_grok_image", style="openai_images", model="grok-imagine-image-2.0")
    edit_endpoint, edit_payload, edit_files = xai._build_request(
        ImageRequest(
            model="grok-imagine-image-2.0",
            prompt="repaint",
            operation="edit",
            n=1,
            aspect_ratio="19.5:9",
            resolution="2k",
            quality="low",
        ),
        [_prepared(), _prepared()],
    )
    assert edit_endpoint.endswith("/v1/images/edits")
    assert edit_payload["aspect_ratio"] == "19.5:9"
    assert edit_payload["resolution"] == "2k"
    assert edit_payload["quality"] == "low"
    assert edit_files is None
    assert len(edit_payload["images"]) == 2
    assert all(item["url"].startswith("data:image/") for item in edit_payload["images"])


def test_chat_style_does_not_force_native_google_request():
    client = _client(family="google_gemini", style="openai_chat", model="gemini-3-pro-image")
    client.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    endpoint, payload, files = client._build_request(
        ImageRequest(
            model="gemini-3-pro-image",
            prompt="a lake",
            operation="generate",
            n=2,
            aspect_ratio="16:9",
            image_size="2K",
            multi_image_strategy="candidate_count",
        ),
        [],
    )
    assert endpoint.endswith("/v1beta/openai/chat/completions")
    assert payload["messages"][0]["content"][-1]["text"] == "a lake"
    assert payload["generation_config"]["imageConfig"] == {
        "aspectRatio": "16:9",
        "imageSize": "2K",
    }
    assert payload["generation_config"]["candidateCount"] == 2
    assert payload["extra_body"]["google"]["aspect_ratio"] == "16:9"
    assert payload["modalities"] == ["image"]
    assert files is None


def test_google_compatible_chat_uses_bearer_while_native_uses_google_header(monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("tagger2.image_generation.client.validate_provider_url", lambda value, **_: value)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"image-bytes").decode()}]})

    async def run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http_client:
            native = _client(family="google_gemini", style="native", model="gemini-3-pro-image")
            native.key_pool = APIKeyPool(["native-key"])
            await native._post_with_retries(http_client, "https://provider.example/native", {}, None)
            chat = _client(family="google_gemini", style="openai_chat", model="gemini-3-pro-image")
            chat.key_pool = APIKeyPool(["chat-key"])
            await chat._post_with_retries(http_client, "https://provider.example/chat", {}, None)

    asyncio.run(run())
    assert seen[0].headers["x-goog-api-key"] == "native-key"
    assert "authorization" not in seen[0].headers
    assert seen[1].headers["authorization"] == "Bearer chat-key"


def test_parser_supports_native_and_rejects_oversized_base64_before_decode():
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 64
    encoded = base64.b64encode(raw).decode("ascii")
    parsed = parse_response({"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded, "mimeType": "image/png"}}]}}]})
    assert parsed.route == "candidates[].parts[].inlineData"
    assert parsed.images[0].data == raw
    assert parse_response({"data": [{"b64_json": encoded}]}, max_decoded_bytes=16).images == []


def test_remote_image_url_allows_signed_cdn_query_but_not_credentials():
    assert validate_provider_url("https://cdn.example/image.png?sig=abc&expires=1", allow_query=True) == "https://cdn.example/image.png?sig=abc&expires=1"
    with pytest.raises(SecurityError):
        validate_provider_url("https://cdn.example/image.png?key=secret", allow_query=True)


def test_remote_image_redirect_is_rejected(monkeypatch):
    client = _client(family="openai_gpt_image", style="openai_images", model="gpt-image-1")
    monkeypatch.setattr("tagger2.image_generation.client.validate_provider_url", lambda value, **_: value)

    async def run():
        transport = httpx.MockTransport(lambda _request: httpx.Response(302, headers={"location": "https://cdn.example/next.png"}))
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http_client:
            with pytest.raises(Exception, match="redirect rejected"):
                await client._fetch_remote_image(http_client, "https://cdn.example/image.png?sig=abc")

    asyncio.run(run())


def test_provider_json_response_is_streamed_with_a_hard_size_limit(monkeypatch):
    client = _client(family="openai_gpt_image", style="openai_images", model="gpt-image-1")
    client.max_response_bytes = 1024
    monkeypatch.setattr("tagger2.image_generation.client.validate_provider_url", lambda value, **_: value)

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{" + b"x" * 768
            yield b"x" * 768 + b"}"

    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=OversizedStream(),
                headers={"content-type": "application/json"},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport) as http_client:
            with pytest.raises(Exception) as caught:
                await client._post_with_retries(
                    http_client,
                    "https://provider.example/v1/images/generations",
                    {"model": "gpt-image-1", "prompt": "test", "n": 1},
                    None,
                )
        assert getattr(caught.value, "code", "") == "image_provider_response_too_large"

    asyncio.run(run())


def test_storage_cancelled_attempts_are_retryable_and_delete_has_tombstone(tmp_path):
    storage = ImageGenerationStorage(tmp_path / "image.sqlite3")
    job_id = "a" * 32
    storage.create_job(
        job_id=job_id,
        provider_id="provider",
        model="gpt-image-1",
        family="openai_gpt_image",
        operation="generate",
        requested_count=1,
        config={"provider_id": "provider", "model": "gpt-image-1", "prompt": "test"},
        attempts=1,
        references=(),
    )
    attempt = storage.claim_attempt(job_id)
    assert attempt is not None
    storage.finish_attempt(attempt["id"], state="cancelled", error_code="image_job_cancelled")
    storage.request_cancel(job_id)
    assert storage.reset_retryable(job_id) == 1
    assert storage.get_job(job_id)["state"] == "queued"
    storage.finalize_job(job_id, state="succeeded")
    storage.mark_deleting(job_id)
    assert storage.get_job(job_id)["state"] == "deleting"
    assert storage.delete_job(job_id) is True
    assert storage.get_job(job_id) is None
    storage.close()


def test_service_persists_and_resumes_a_generation_without_duplicate_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tagger2.image_generation.service.validate_provider_url",
        lambda value, **_: str(value),
    )
    settings = AppConfig(project_root=tmp_path, data_dir=tmp_path / "data", production=False)
    profiles = {
        "xai": {
            "id": "xai",
            "name": "xAI",
            "kind": "xai",
            "base_url": "https://api.x.ai/v1",
            "config": {
                "protocol": "openai",
                "image_family": "xai_grok_image",
                "image_api_style": "openai_images",
            },
            "secret_ref": "provider_xai",
            "enabled": True,
        }
    }
    calls = 0
    image = _prepared()

    async def fake_generate(self, _request, _references):
        nonlocal calls
        calls += 1
        return ImageCallResult(
            images=[ResolvedImage(data=image.data, mime_type=image.mime_type, source="test", width=image.width, height=image.height)],
            parser_route="data[].b64_json",
        )

    monkeypatch.setattr(ImageGenerationClient, "generate", fake_generate)

    class FakeSecrets:
        def get_many(self, _namespace):
            return ["test-secret"]

    async def run():
        service = ImageGenerationService(settings, provider_profiles=profiles.get, secrets=FakeSecrets())
        await service.start()
        job = await service.create_job(
            ImageJobConfig(provider_id="xai", model="grok-2-image-1212", prompt="a test image", n=1),
            (),
        )
        for _ in range(100):
            current = service.get_job(job["id"])
            if current["state"] in {"succeeded", "failed", "partial"}:
                break
            await asyncio.sleep(0.01)
        current = service.get_job(job["id"])
        await service.close()
        return current

    current = asyncio.run(run())
    assert current["state"] == "succeeded"
    assert current["completed_count"] == 1
    assert calls == 1


def test_capability_projection_matches_selected_api_style():
    native = capability_object(
        kind="gemini",
        protocol="gemini",
        model="gemini-3-pro-image",
        configured_family="google_gemini",
    )
    chat = capability_for_style(native, "openai_chat")
    assert "aspect_ratio" in chat.parameters
    assert "image_size" in chat.parameters

    openai = capability_object(
        kind="openai",
        protocol="openai",
        model="gpt-image-1",
        configured_family="openai_gpt_image",
    )
    openai_chat = capability_for_style(openai, "openai_chat")
    assert openai_chat.parameters == ("temperature", "top_p", "system_instruction")


def test_nano_banana_model_overrides_constrain_size_and_reference_count():
    lite = capabilities_for(
        kind="gemini",
        protocol="gemini",
        model="gemini-3.1-flash-lite-image",
    )
    assert lite["enums"]["image_size"] == ["1K"]
    assert lite["enums"]["aspect_ratio"] == list(GOOGLE_BASE_RATIOS)
    assert lite["max_references"] == 10
    flash = capabilities_for(
        kind="gemini",
        protocol="gemini",
        model="gemini-3.1-flash-image",
    )
    assert flash["enums"]["image_size"] == ["512", "1K", "2K", "4K"]
    assert "1:8" in flash["enums"]["aspect_ratio"]
    pro = capabilities_for(
        kind="gemini",
        protocol="gemini",
        model="gemini-3-pro-image",
    )
    assert "1:8" not in pro["enums"]["aspect_ratio"]
    assert "9:21" not in pro["enums"]["aspect_ratio"]
    legacy = capabilities_for(
        kind="gemini",
        protocol="gemini",
        model="gemini-2.5-flash-image",
    )
    assert legacy["enums"]["image_size"] == ["1K"]
    assert legacy["max_references"] == 3


def test_official_legacy_grok_and_gpt_image_expose_only_supported_parameters():
    grok = capabilities_for(
        kind="xai",
        protocol="openai",
        model="grok-2-image-1212",
    )
    assert grok["operations"] == ["generate"]
    assert grok["max_references"] == 0
    assert grok["parameters"] == ["response_format"]
    openai = capabilities_for(
        kind="openai",
        protocol="openai",
        model="gpt-image-1",
    )
    assert "quality" in openai["parameters"]
    assert "response_format" not in openai["parameters"]


def test_grok_imagine_registry_matches_official_controls():
    grok = capabilities_for(
        kind="xai",
        protocol="openai",
        model="grok-imagine-image-2.0",
    )
    assert grok["known"] is True
    assert grok["operations"] == ["generate", "edit"]
    assert grok["parameters"] == ["aspect_ratio", "resolution", "quality", "response_format"]
    assert grok["enums"]["resolution"] == ["1k", "2k"]
    assert grok["enums"]["quality"] == ["low", "medium"]
    assert grok["enums"]["aspect_ratio"] == [
        "auto", "1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9",
        "9:19.5", "19.5:9", "9:20", "20:9", "1:2", "2:1", "21:9", "5:2",
    ]
    assert grok["max_references"] == 3
    assert grok["max_outputs"] == 10
    assert grok["defaults"]["resolution"] == "1k"


def test_gpt_image_2_accepts_custom_width_by_height_sizes(tmp_path):
    image = capabilities_for(
        kind="openai",
        protocol="openai",
        model="gpt-image-2",
    )
    assert "custom" in image["enums"]["size"]
    settings = AppConfig(project_root=tmp_path, data_dir=tmp_path / "data", production=False)
    service = ImageGenerationService(
        settings,
        provider_profiles=lambda _provider_id: None,
        secrets=None,
    )
    capability = capability_object(
        kind="openai",
        protocol="openai",
        model="gpt-image-2",
        configured_family="openai_gpt_image",
    )
    service._validate_config(
        ImageJobConfig(provider_id="openai", model="gpt-image-2", prompt="wide banner", size="1536x864"),
        capability,
        reference_count=0,
        image_style="openai_images",
    )
    with pytest.raises(Exception, match="不受模型支持"):
        service._validate_config(
            ImageJobConfig(provider_id="openai", model="gpt-image-2", prompt="weird size", size="banana"),
            capability,
            reference_count=0,
            image_style="openai_images",
        )
    strict = capability_object(
        kind="openai",
        protocol="openai",
        model="gpt-image-1",
        configured_family="openai_gpt_image",
    )
    with pytest.raises(Exception, match="不受模型支持"):
        service._validate_config(
            ImageJobConfig(provider_id="openai", model="gpt-image-1", prompt="no custom", size="1536x864"),
            strict,
            reference_count=0,
            image_style="openai_images",
        )


def test_unregistered_grok_model_requires_explicit_family_for_extended_parameters():
    automatic = capabilities_for(
        kind="xai",
        protocol="openai",
        model="grok-future-image",
    )
    assert automatic["known"] is False
    assert automatic["parameters"] == ["response_format"]

    explicit = capabilities_for(
        kind="xai",
        protocol="openai",
        model="grok-future-image",
        configured_family="xai_grok_image",
    )
    assert explicit["known"] is True
    assert "aspect_ratio" in explicit["parameters"]


def test_job_executes_with_frozen_provider_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tagger2.image_generation.service.validate_provider_url",
        lambda value, **_: str(value),
    )
    settings = AppConfig(project_root=tmp_path, data_dir=tmp_path / "data", production=False)
    profiles = {
        "image": {
            "id": "image",
            "name": "Original",
            "kind": "xai",
            "base_url": "https://original.example/v1",
            "config": {
                "protocol": "openai",
                "image_family": "xai_grok_image",
                "image_api_style": "openai_images",
                "timeout_seconds": 75,
                "max_concurrency": 2,
                "headers": {
                    "OpenAI-Organization": "org-public-id",
                    "X-Custom-Token": "must-not-be-persisted",
                },
            },
            "secret_ref": "provider_image",
            "enabled": True,
        }
    }
    captured: dict[str, object] = {}
    image = _prepared()

    async def fake_generate(self, _request, _references):
        captured.update({
            "base_url": self.base_url,
            "family": self.family,
            "style": self.api_style,
            "timeout": self.config.timeout_seconds,
        })
        return ImageCallResult(
            images=[ResolvedImage(data=image.data, mime_type=image.mime_type, source="test")],
            parser_route="test",
        )

    monkeypatch.setattr(ImageGenerationClient, "generate", fake_generate)

    class FakeSecrets:
        def get_many(self, namespace):
            assert namespace == "provider_image"
            return ["secret"]

    async def run():
        service = ImageGenerationService(settings, provider_profiles=profiles.get, secrets=FakeSecrets())

        async def no_start(_job_id):
            return None

        monkeypatch.setattr(service, "start_job", no_start)
        job = await service.create_job(
            ImageJobConfig(provider_id="image", model="grok-2-image-1212", prompt="snapshot"),
            (),
        )
        profiles["image"]["base_url"] = "https://changed.example/v1"
        profiles["image"]["kind"] = "openai"
        profiles["image"]["config"] = {
            "protocol": "openai",
            "image_family": "openai_gpt_image",
            "image_api_style": "openai_chat",
            "timeout_seconds": 5,
        }
        attempt = service.storage.claim_attempt(job["id"])
        assert attempt is not None
        await service._execute_attempt(job["id"], attempt)
        public = service.get_job(job["id"])
        raw = service.storage.get_job(job["id"])
        assert "_provider_snapshot" not in public["config"]
        assert raw["config"]["_provider_snapshot"]["base_url"] == "https://original.example/v1"
        assert raw["config"]["_provider_snapshot"]["headers"] == {
            "OpenAI-Organization": "org-public-id"
        }
        await service.close()

    asyncio.run(run())
    assert captured == {
        "base_url": "https://original.example/v1",
        "family": "xai_grok_image",
        "style": "openai_images",
        "timeout": 75.0,
    }


def test_parallel_strategy_runs_concurrently_and_shares_key_pool(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tagger2.image_generation.service.validate_provider_url",
        lambda value, **_: str(value),
    )
    settings = AppConfig(project_root=tmp_path, data_dir=tmp_path / "data", production=False)
    profiles = {
        "image": {
            "id": "image",
            "name": "Parallel",
            "kind": "xai",
            "base_url": "https://parallel.example/v1",
            "config": {
                "protocol": "openai",
                "image_family": "xai_grok_image",
                "image_api_style": "openai_images",
                "max_concurrency": 3,
            },
            "secret_ref": "provider_image",
            "enabled": True,
        }
    }
    image = _prepared()
    active = 0
    peak = 0
    pools: set[int] = set()

    async def fake_generate(self, _request, _references):
        nonlocal active, peak
        pools.add(id(self.key_pool))
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        active -= 1
        return ImageCallResult(
            images=[ResolvedImage(data=image.data, mime_type=image.mime_type, source="test")],
            parser_route="test",
        )

    monkeypatch.setattr(ImageGenerationClient, "generate", fake_generate)

    class FakeSecrets:
        def get_many(self, _namespace):
            return ["key-a", "key-b", "key-c"]

    async def run():
        service = ImageGenerationService(settings, provider_profiles=profiles.get, secrets=FakeSecrets())
        await service.start()
        job = await service.create_job(
            ImageJobConfig(provider_id="image", model="grok-2-image-1212", prompt="parallel", n=3),
            (),
        )
        for _ in range(200):
            current = service.get_job(job["id"])
            if current["state"] in {"succeeded", "failed", "partial"}:
                break
            await asyncio.sleep(0.01)
        current = service.get_job(job["id"])
        await service.close()
        return current

    current = asyncio.run(run())
    assert current["state"] == "succeeded"
    assert current["completed_count"] == 3
    assert peak >= 2
    assert len(pools) == 1


def test_restart_finishes_cancelling_job_but_requeues_running_job(tmp_path):
    storage = ImageGenerationStorage(tmp_path / "restart.sqlite3")
    for suffix in ("cancel", "run"):
        storage.create_job(
            job_id=suffix,
            provider_id="provider",
            model="gpt-image-1",
            family="openai_gpt_image",
            operation="generate",
            requested_count=1,
            config={"provider_id": "provider", "model": "gpt-image-1", "prompt": suffix},
            attempts=1,
            references=(),
        )
        assert storage.claim_attempt(suffix) is not None
    storage.request_cancel("cancel")
    recovered = storage.recover_interrupted()
    assert recovered == ["run"]
    assert storage.get_job("cancel")["state"] == "cancelled"
    assert storage.get_job("run")["state"] == "interrupted"
    with storage.connection() as connection:
        cancelled_attempt = connection.execute(
            "SELECT state FROM image_generation_attempts WHERE job_id='cancel'"
        ).fetchone()[0]
        resumed_attempt = connection.execute(
            "SELECT state FROM image_generation_attempts WHERE job_id='run'"
        ).fetchone()[0]
    assert cancelled_attempt == "cancelled"
    assert resumed_attempt == "pending"
    storage.close()


def test_future_image_database_schema_is_rejected_without_downgrade(tmp_path):
    database = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version=99")
    connection.close()

    with pytest.raises(RuntimeError, match="schema is newer"):
        ImageGenerationStorage(database)

    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    connection.close()


def test_job_snapshot_digest_is_checked_before_secrets_or_provider_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tagger2.image_generation.service.validate_provider_url",
        lambda value, **_: str(value),
    )
    settings = AppConfig(project_root=tmp_path, data_dir=tmp_path / "data", production=False)
    profile = {
        "id": "image",
        "name": "Image API",
        "kind": "openai",
        "base_url": "https://api.example/v1",
        "config": {
            "protocol": "openai",
            "image_family": "openai_gpt_image",
            "image_api_style": "openai_images",
        },
        "secret_ref": "provider_image",
        "enabled": True,
    }
    remote_calls = 0

    async def fake_generate(self, _request, _references):
        nonlocal remote_calls
        remote_calls += 1
        raise AssertionError("tampered jobs must not reach the provider")

    monkeypatch.setattr(ImageGenerationClient, "generate", fake_generate)

    class NoSecrets:
        def get_many(self, _namespace):
            raise AssertionError("tampered jobs must not read provider secrets")

    async def run():
        service = ImageGenerationService(
            settings,
            provider_profiles=lambda provider_id: profile if provider_id == "image" else None,
            secrets=NoSecrets(),
        )

        async def no_start(_job_id):
            return None

        monkeypatch.setattr(service, "start_job", no_start)
        job = await service.create_job(
            ImageJobConfig(provider_id="image", model="gpt-image-1", prompt="original"),
            (),
        )
        raw = service.storage.get_job(job["id"])
        tampered = dict(raw["config"])
        tampered["prompt"] = "tampered"
        with service.storage.transaction() as connection:
            connection.execute(
                "UPDATE image_generation_jobs SET config_json=? WHERE id=?",
                (canonical_json(tampered), job["id"]),
            )
        attempt = service.storage.claim_attempt(job["id"])
        assert attempt is not None
        with pytest.raises(Exception) as caught:
            await service._execute_attempt(job["id"], attempt)
        assert getattr(caught.value, "code", "") == "image_job_snapshot_tampered"
        await service.close()

    asyncio.run(run())
    assert remote_calls == 0


def test_image_generation_api_keeps_snapshots_private_and_rejects_tampered_artifacts(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr("tagger2.main.validate_provider_url", lambda value, **_: str(value))
    monkeypatch.setattr(
        "tagger2.image_generation.service.validate_provider_url",
        lambda value, **_: str(value),
    )
    settings = AppConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        production=True,
    )
    with TestClient(create_app(settings)) as client:
        created_provider = client.post(
            "/api/v1/providers",
            json={
                "name": "xAI Images",
                "kind": "xai",
                "base_url": "https://api.x.ai/v1",
                "primary_model": "grok-2-image-1212",
                "image_enabled": True,
                "image_family": "xai_grok_image",
                "image_api_style": "openai_images",
            },
        )
        assert created_provider.status_code == 200, created_provider.text
        provider = created_provider.json()
        assert provider["kind"] == "xai"
        assert provider["image_family"] == "xai_grok_image"
        assert provider["image_api_style"] == "openai_images"

        patched_provider = client.patch(
            f"/api/v1/providers/{provider['id']}",
            json={"image_base_url": "https://images.x.ai/v1", "image_enabled": True},
        )
        assert patched_provider.status_code == 200, patched_provider.text
        assert patched_provider.json()["image_base_url"] == "https://images.x.ai/v1"

        service = client.app.state.runtime.image_generation

        async def no_start(_job_id: str) -> None:
            return None

        monkeypatch.setattr(service, "start_job", no_start)
        created_job = client.post(
            "/api/v1/image-generation/jobs",
            files={
                "config": (
                    None,
                    json.dumps({
                        "provider_id": provider["id"],
                        "model": "grok-2-image-1212",
                        "prompt": "A precise studio product image",
                    }),
                    "application/json",
                )
            },
        )
        assert created_job.status_code == 202, created_job.text
        job = created_job.json()
        assert "_provider_snapshot" not in job["config"]
        assert "_capability_snapshot" not in job["config"]
        assert "images.x.ai" not in created_job.text

        malformed = client.post(
            "/api/v1/image-generation/jobs",
            files={"config": (None, "{{", "application/json")},
        )
        assert malformed.status_code == 422
        assert malformed.json()["code"] == "image_config_invalid"
        assert malformed.json()["request_id"]
        assert "detail" not in malformed.json()

        attempt = service.storage.claim_attempt(job["id"])
        assert attempt is not None
        prepared = _prepared()
        relative = Path("artifacts") / "output-000.jpg"
        target = service._safe_job_path(job["id"], relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(prepared.data)
        artifact = service.storage.record_artifact(
            job_id=job["id"],
            attempt_id=attempt["id"],
            ordinal=0,
            relative_path=relative.as_posix(),
            mime_type=prepared.mime_type,
            width=prepared.width,
            height=prepared.height,
            data=prepared.data,
            source="test",
        )
        downloaded = client.get(f"/api/v1/image-generation/artifacts/{artifact['id']}")
        assert downloaded.status_code == 200
        assert downloaded.content == prepared.data

        target.write_bytes(b"tampered")
        rejected = client.get(f"/api/v1/image-generation/artifacts/{artifact['id']}")
        assert rejected.status_code == 404
        assert rejected.json()["code"] == "image_artifact_not_found"
        assert str(tmp_path) not in rejected.text


def test_image_generation_routes_require_lan_bearer_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEST_TAGGER2_IMAGE_TOKEN", "correct-token")
    settings = AppConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        production=True,
        host="0.0.0.0",
        allow_lan=True,
        access_token_env="TEST_TAGGER2_IMAGE_TOKEN",
    )
    with TestClient(create_app(settings)) as client:
        denied = client.get("/api/v1/image-generation/capabilities")
        assert denied.status_code == 401
        allowed = client.get(
            "/api/v1/image-generation/capabilities",
            headers={"Authorization": "Bearer correct-token"},
        )
        assert allowed.status_code == 200
