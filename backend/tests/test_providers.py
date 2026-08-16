import asyncio
import io
import json
import threading
import time

import httpx
from PIL import Image

from tagger2.providers import (
    APIKeyPool,
    ClaudeProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    create_provider,
    parse_retry_after,
    prepare_image,
)


def _image() -> bytes:
    stream = io.BytesIO()
    Image.new("RGBA", (12, 8), (255, 0, 0, 180)).save(stream, format="PNG")
    return stream.getvalue()


def test_key_pool_round_robin_and_retry_after():
    pool = APIKeyPool(["a", "b", "a"])
    assert [pool.next_key(), pool.next_key()] == ["a", "b"]
    pool.cooldown("a", 60)
    assert pool.next_key() == "b"
    assert parse_retry_after("3") == 3


def test_source_larger_than_wire_limit_is_compressed():
    stream = io.BytesIO()
    Image.new("RGB", (256, 256), (32, 64, 96)).save(stream, format="BMP")
    prepared = prepare_image(stream.getvalue(), max_source_bytes=300_000, max_bytes=2_000)
    assert len(stream.getvalue()) > 2_000
    assert len(prepared.data) <= 2_000


def test_gemini_provider_payload_and_anima_result():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"quality":["highres"],"count":"solo","character":"","series":"","artist":"","appearance":["red fur"],"tags":["digital art"],"environment":["outdoors"],"nl":"caption"}'
                                }
                            ]
                        }
                    }
                ]
            },
        )

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GeminiProvider(
            ProviderConfig(kind="gemini", base_url="https://example.test/v1beta", model="vision", api_key="secret"),
            client=client,
        )
        result = await provider.generate_anima(_image(), "return JSON")
        await provider.aclose()
        return result

    result = asyncio.run(run())
    assert result.count == "solo"
    assert seen[0].url.path.endswith("/models/vision:generateContent")
    assert seen[0].headers["x-goog-api-key"] == "secret"
    assert "secret" not in str(seen[0].url)


def test_provider_image_preparation_keeps_event_loop_responsive(monkeypatch):
    import tagger2.providers.client as provider_client

    original_prepare = provider_client.prepare_image
    prepare_threads = []

    def slow_prepare(*args, **kwargs):
        prepare_threads.append(threading.get_ident())
        time.sleep(0.08)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(provider_client, "prepare_image", slow_prepare)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": '{"quality":["highres"],"count":"solo","character":"","series":"","artist":"","appearance":["red fur"],"tags":["digital art"],"environment":["outdoors"],"nl":"caption"}'
                                }
                            ]
                        }
                    }
                ]
            },
        )

    async def run():
        loop_thread = threading.get_ident()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GeminiProvider(
            ProviderConfig(
                kind="gemini",
                base_url="https://example.test/v1beta",
                model="vision",
                api_key="secret",
            ),
            client=client,
        )
        stopped = asyncio.Event()
        beats = 0

        async def heartbeat():
            nonlocal beats
            while not stopped.is_set():
                beats += 1
                await asyncio.sleep(0.005)

        pulse = asyncio.create_task(heartbeat())
        try:
            result = await provider.generate_anima(_image(), "return JSON")
        finally:
            stopped.set()
            await pulse
            await provider.aclose()
        return result, loop_thread, beats

    result, loop_thread, beats = asyncio.run(run())
    assert result.count == "solo"
    assert prepare_threads and prepare_threads[0] != loop_thread
    assert beats >= 3


def test_auth_error_is_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GeminiProvider(
            ProviderConfig(kind="gemini", base_url="https://example.test", model="vision", api_key="secret", max_retries=4),
            client=client,
        )
        with __import__("pytest").raises(ProviderError) as error:
            await provider.generate(_image(), "prompt")
        await provider.aclose()
        return error.value

    error = asyncio.run(run())
    assert calls == 1
    assert error.code == "provider_auth"


def test_request_time_dns_rebinding_is_blocked(monkeypatch):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        "tagger2.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            ProviderConfig(kind="openai", base_url="https://provider.example", model="vision"),
            client=client,
        )
        provider.validate_destination = True
        try:
            with __import__("pytest").raises(ProviderError) as error:
                await provider.generate([], "prompt")
            return error.value
        finally:
            await provider.aclose()

    error = asyncio.run(run())
    assert error.code == "provider_destination_blocked"
    assert calls == 0


def test_provider_does_not_follow_redirect_to_a_new_destination():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            ProviderConfig(kind="openai", base_url="https://provider.example", model="vision"),
            client=client,
        )
        try:
            with __import__("pytest").raises(ProviderError):
                await provider._request("GET", "https://provider.example/redirect")
        finally:
            await provider.aclose()

    asyncio.run(run())
    assert calls == ["https://provider.example/redirect"]


def test_rate_limit_rotates_key_without_waiting_for_retry_after():
    keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers.get("x-goog-api-key"))
        if len(keys) == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GeminiProvider(
            ProviderConfig(
                kind="gemini",
                base_url="https://example.test",
                model="vision",
                api_keys=("key-a", "key-b"),
                max_retries=1,
            ),
            client=client,
        )
        value = await provider.generate(_image(), "prompt")
        await provider.aclose()
        return value

    assert asyncio.run(run()) == "ok"
    assert keys == ["key-a", "key-b"]


def test_claude_provider_payload_headers_and_model_discovery():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-5"}]})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "result"}]})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = ClaudeProvider(
            ProviderConfig(
                kind="claude",
                base_url="https://api.anthropic.test",
                model="claude-sonnet-4-5",
                api_key="secret",
                top_k=20,
            ),
            client=client,
        )
        result = await provider.generate(_image(), "describe")
        models = await provider.discover_models()
        await provider.aclose()
        return result, models

    result, models = asyncio.run(run())
    assert result == "result"
    assert models == ["claude-sonnet-4-5"]
    assert seen[0].url.path == "/v1/messages"
    assert seen[0].headers["x-api-key"] == "secret"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"
    payload = __import__("json").loads(seen[0].content)
    assert payload["messages"][0]["content"][0]["source"]["type"] == "base64"
    assert payload["top_k"] == 20


def test_system_prompt_is_mapped_to_each_provider_protocol():
    payloads = {}

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        payloads["gemini"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    def openai_handler(request: httpx.Request) -> httpx.Response:
        payloads["openai"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    def claude_handler(request: httpx.Request) -> httpx.Response:
        payloads["claude"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    async def run():
        providers = [
            GeminiProvider(
                ProviderConfig(kind="gemini", base_url="https://gemini.test", model="vision"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler)),
            ),
            OpenAICompatibleProvider(
                ProviderConfig(kind="openai", base_url="https://openai.test", model="vision"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(openai_handler)),
            ),
            ClaudeProvider(
                ProviderConfig(kind="claude", base_url="https://claude.test", model="vision"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(claude_handler)),
            ),
        ]
        try:
            return await asyncio.gather(*(provider.generate(_image(), "prompt", system_prompt="system rules") for provider in providers))
        finally:
            await asyncio.gather(*(provider.aclose() for provider in providers))

    assert asyncio.run(run()) == ["ok", "ok", "ok"]
    assert payloads["gemini"]["systemInstruction"]["parts"][0]["text"] == "system rules"
    assert payloads["openai"]["messages"][0] == {"role": "system", "content": "system rules"}
    assert payloads["openai"]["messages"][1]["role"] == "user"
    assert payloads["claude"]["system"] == "system rules"


def test_multiple_images_keep_order_and_empty_base_mode_sends_text_only():
    payloads = {}

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        payloads["gemini"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    def openai_handler(request: httpx.Request) -> httpx.Response:
        payloads.setdefault("openai", []).append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    def claude_handler(request: httpx.Request) -> httpx.Response:
        payloads["claude"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    async def run():
        providers = [
            GeminiProvider(
                ProviderConfig(kind="gemini", base_url="https://gemini.test", model="vision"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler)),
            ),
            OpenAICompatibleProvider(
                ProviderConfig(kind="openai", base_url="https://openai.test", model="vision"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(openai_handler)),
            ),
            ClaudeProvider(
                ProviderConfig(kind="claude", base_url="https://claude.test", model="vision"),
                client=httpx.AsyncClient(transport=httpx.MockTransport(claude_handler)),
            ),
        ]
        try:
            await asyncio.gather(*(provider.generate([_image(), _image()], "two images") for provider in providers))
            await providers[1].generate([], "text only")
        finally:
            await asyncio.gather(*(provider.aclose() for provider in providers))

    asyncio.run(run())
    gemini_parts = payloads["gemini"]["contents"][0]["parts"]
    assert [part.get("inline_data", {}).get("mime_type") for part in gemini_parts[:-1]] == ["image/jpeg", "image/jpeg"]
    assert gemini_parts[-1] == {"text": "two images"}
    openai_multi_content = payloads["openai"][0]["messages"][-1]["content"]
    assert [part["type"] for part in openai_multi_content] == ["image_url", "image_url", "text"]
    openai_content = payloads["openai"][1]["messages"][-1]["content"]
    assert [part["type"] for part in openai_content] == ["text"]
    assert openai_content[0]["text"] == "text only"
    claude_content = payloads["claude"]["messages"][0]["content"]
    assert [part["type"] for part in claude_content] == ["image", "image", "text"]


def test_custom_provider_protocol_selects_compatible_client():
    common = {"kind": "custom", "base_url": "https://gateway.test", "model": "vision"}
    assert isinstance(create_provider({**common, "protocol": "openai"}), OpenAICompatibleProvider)
    assert isinstance(create_provider({**common, "protocol": "gemini"}), GeminiProvider)
    assert isinstance(create_provider({**common, "protocol": "claude"}), ClaudeProvider)
