"""Async vision providers for Gemini, OpenAI-compatible servers and proxies."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import quote

import httpx

from ..anima import AnimaPayload, parse_anima_response
from .base import (
    APIKeyPool,
    ProviderConfig,
    ProviderError,
    ProviderHTTPMixin,
    ProviderKind,
    ProviderProtocol,
    backoff_seconds,
)
from .image import PreparedImage, prepare_image


ImageInput = str | Path | bytes | bytearray | PreparedImage
ImageInputs = ImageInput | Sequence[ImageInput] | None


class VisionProvider(ProviderHTTPMixin):
    """Common provider interface.

    A provider instance owns an ``httpx.AsyncClient`` by default.  Tests and
    embedding applications can pass a client with a custom transport; such a
    client is never closed by :meth:`aclose` unless ``owns_client`` is true.
    """

    def __init__(self, config: ProviderConfig | Mapping[str, Any], client: httpx.AsyncClient | None = None) -> None:
        self.config = config if isinstance(config, ProviderConfig) else ProviderConfig.from_mapping(config)
        self.key_pool = APIKeyPool(self.config.api_keys)
        self.semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds),
            limits=httpx.Limits(
                max_connections=max(4, self.config.max_concurrency * 2),
                max_keepalive_connections=max(2, self.config.max_concurrency),
            ),
            follow_redirects=False,
        )
        self._owns_client = client is None
        # Injected transports are used by tests and trusted embedding hosts.
        # Production-owned clients re-resolve and validate every destination
        # immediately before the connection attempt.
        self.validate_destination = client is None

    @property
    def kind(self) -> ProviderKind:
        return self.config.kind  # type: ignore[return-value]

    @property
    def model(self) -> str:
        return self.config.model

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> "VisionProvider":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def generate(
        self,
        image: ImageInputs,
        prompt: str,
        *,
        model: str | None = None,
        validator: Callable[[str], Any] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is required")
        prepared = await self._prepare_images(image)
        system = system_prompt.strip() if isinstance(system_prompt, str) and system_prompt.strip() else None
        selected = model.strip() if model and model.strip() else self.config.model
        models = [selected]
        models.extend(fallback for fallback in self.config.fallback_models if fallback not in models)
        last: Exception | None = None
        for selected_model in models:
            try:
                return await self._generate_with_validation(
                    prepared,
                    prompt,
                    selected_model,
                    validator=validator,
                    system_prompt=system,
                )
            except asyncio.CancelledError:
                raise
            except ProviderError as exc:
                # Credentials are shared across a profile's model choices, so
                # switching models cannot repair an authentication failure.
                if exc.status_code in {401, 403}:
                    raise
                last = exc
            except Exception as exc:
                last = exc
        if isinstance(last, ProviderError):
            raise last
        raise ProviderError("provider returned an invalid response", code="provider_response_invalid") from last

    async def _generate_with_validation(
        self,
        images: Sequence[PreparedImage],
        prompt: str,
        model: str,
        *,
        validator: Callable[[str], Any] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        last: Exception | None = None
        # A malformed JSON response is a transient model failure.  Retry it a
        # bounded number of times before switching to the configured backup.
        for attempt in range(self.config.max_retries + 1):
            try:
                text = await self._generate_model(images, prompt, model, system_prompt=system_prompt)
                if not text.strip():
                    raise ProviderError("provider returned an empty response", retryable=True, code="provider_empty_response")
                if validator is not None:
                    validator(text)
                return text.strip()
            except asyncio.CancelledError:
                raise
            except ProviderError as exc:
                last = exc
                validation_codes = {
                    "provider_empty_response",
                    "provider_malformed_response",
                    "provider_no_candidates",
                    "provider_no_choices",
                }
                if not exc.retryable or exc.code not in validation_codes or attempt >= self.config.max_retries:
                    raise
                await asyncio.sleep(backoff_seconds(self.config.retry_base_seconds, attempt, exc.retry_after))
            except Exception as exc:
                last = exc
                if attempt >= self.config.max_retries:
                    raise
                await asyncio.sleep(backoff_seconds(self.config.retry_base_seconds, attempt))
        raise last or ProviderError("provider request failed")

    async def generate_anima(
        self,
        image: str | Path | bytes | bytearray | PreparedImage,
        prompt: str,
        *,
        trigger_artist: str = "",
        model: str | None = None,
    ) -> AnimaPayload:
        def validate(value: str) -> AnimaPayload:
            return parse_anima_response(value, trigger_artist=trigger_artist)

        text = await self.generate(image, prompt, model=model, validator=validate)
        try:
            return parse_anima_response(text, trigger_artist=trigger_artist)
        except Exception as exc:
            raise ProviderError("provider returned invalid Anima JSON", retryable=True, code="provider_invalid_json") from exc

    async def discover_models(self) -> list[str]:
        raise NotImplementedError

    async def get_models(self) -> list[str]:
        return await self.discover_models()

    async def close(self) -> None:
        await self.aclose()

    async def test(self) -> dict[str, Any]:
        models = await self.discover_models()
        return {"ok": True, "kind": self.kind.value, "models": models}

    async def _prepare_images(self, image: ImageInputs) -> list[PreparedImage]:
        """Prepare one or more ordered visual inputs without blocking the event loop."""

        if image is None:
            return []
        if isinstance(image, (str, Path, bytes, bytearray, PreparedImage)):
            inputs: Sequence[ImageInput] = [image]
        elif isinstance(image, Sequence):
            inputs = list(cast(Sequence[ImageInput], image))
        else:
            raise TypeError("image must be an image source, a sequence of image sources, or None")

        prepared: list[PreparedImage] = []
        for source in inputs:
            if isinstance(source, PreparedImage):
                prepared.append(source)
                continue
            # Pillow decode, EXIF transpose, resize and JPEG compression are
            # CPU/file-system work. Keep them off the async provider loop so
            # SSE progress, cancellation and unrelated requests stay live.
            prepared.append(await asyncio.to_thread(
                prepare_image,
                source,
                max_bytes=self.config.max_image_bytes,
                max_source_bytes=self.config.max_source_bytes,
                max_pixels=self.config.max_image_pixels,
                max_dimension=self.config.max_image_dimension,
            ))
        return prepared

    async def _generate_model(
        self,
        images: Sequence[PreparedImage],
        prompt: str,
        model: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        raise NotImplementedError


class GeminiProvider(VisionProvider):
    """Google Gemini ``generateContent`` API."""

    def _base(self) -> str:
        base = self.config.base_url.rstrip("/")
        if not base.endswith(("/v1", "/v1beta")):
            base += "/v1beta"
        return base

    def _inject_key(self, headers: dict[str, str], key: str | None) -> None:
        if key:
            headers.setdefault("x-goog-api-key", key)
            if self.kind is ProviderKind.ANTIGRAVITY:
                headers.setdefault("Authorization", f"Bearer {key}")

    async def _generate_model(
        self,
        images: Sequence[PreparedImage],
        prompt: str,
        model: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "contents": [{"parts": [
                *[{"inline_data": {"mime_type": image.mime_type, "data": image.base64}} for image in images],
                {"text": prompt},
            ]}],
            "generationConfig": {
                "temperature": self.config.temperature,
                "topP": self.config.top_p,
                "maxOutputTokens": self.config.max_output_tokens,
            },
        }
        if self.config.top_k is not None:
            payload["generationConfig"]["topK"] = self.config.top_k
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if self.config.json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        path = f"{self._base()}/models/{quote(model, safe='')}:generateContent"
        response = await self._request("POST", path, headers={"Content-Type": "application/json"}, json=payload)
        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("provider returned malformed JSON", retryable=True, code="provider_malformed_response") from exc
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise ProviderError("provider returned no candidates", retryable=True, code="provider_no_candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))
        if not text:
            raise ProviderError("provider returned no text", retryable=True, code="provider_empty_response")
        return text

    async def discover_models(self) -> list[str]:
        response = await self._request("GET", f"{self._base()}/models")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned malformed model list", code="provider_malformed_response") from exc
        result: list[str] = []
        for item in data.get("models", []) if isinstance(data, dict) else []:
            if not isinstance(item, Mapping):
                continue
            methods = item.get("supportedGenerationMethods", [])
            if methods and "generateContent" not in methods:
                continue
            name = str(item.get("name", ""))
            if name.startswith("models/"):
                name = name[7:]
            if name:
                result.append(name)
        return sorted(dict.fromkeys(result))


class AntigravityProvider(GeminiProvider):
    """Gemini-shaped Antigravity proxy (Bearer plus x-goog-api-key)."""


class OpenAICompatibleProvider(VisionProvider):
    """OpenAI ``/v1/chat/completions`` compatible endpoint."""

    def _base(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/v1") else base + "/v1"

    async def _generate_model(
        self,
        images: Sequence[PreparedImage],
        prompt: str,
        model: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        content: list[dict[str, Any]] = [
            *[{"type": "image_url", "image_url": {"url": image.data_url}} for image in images],
            {"type": "text", "text": prompt},
        ]
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_output_tokens,
        }
        if self.config.json_mode and self.config.protocol is ProviderProtocol.OPENAI:
            payload["response_format"] = {"type": "json_object"}
        response = await self._request("POST", f"{self._base()}/chat/completions", headers={"Content-Type": "application/json"}, json=payload)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned malformed JSON", retryable=True, code="provider_malformed_response") from exc
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider returned no choices", retryable=True, code="provider_no_choices")
        message = choices[0].get("message", {})
        value = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(value, list):
            value = "".join(str(part.get("text", "")) for part in value if isinstance(part, Mapping))
        if not isinstance(value, str) or not value.strip():
            raise ProviderError("provider returned no text", retryable=True, code="provider_empty_response")
        return value

    async def discover_models(self) -> list[str]:
        response = await self._request("GET", f"{self._base()}/models")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned malformed model list", code="provider_malformed_response") from exc
        values = data.get("data", []) if isinstance(data, Mapping) else data if isinstance(data, list) else []
        result: list[str] = []
        for item in values:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, Mapping) and item.get("id"):
                result.append(str(item["id"]))
        return sorted(dict.fromkeys(result))


class LMStudioProvider(OpenAICompatibleProvider):
    """LM Studio's OpenAI-compatible local endpoint."""


class ClaudeProvider(VisionProvider):
    """Anthropic Claude ``/v1/messages`` vision API."""

    def _base(self) -> str:
        base = self.config.base_url.rstrip("/")
        return base if base.endswith("/v1") else base + "/v1"

    def _inject_key(self, headers: dict[str, str], key: str | None) -> None:
        if key:
            headers.setdefault("x-api-key", key)
        headers.setdefault("anthropic-version", "2023-06-01")

    async def _generate_model(
        self,
        images: Sequence[PreparedImage],
        prompt: str,
        model: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "messages": [{
                "role": "user",
                "content": [
                    *[{
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image.mime_type,
                            "data": image.base64,
                        },
                    } for image in images],
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        if self.config.top_k is not None:
            payload["top_k"] = self.config.top_k
        if system_prompt:
            payload["system"] = system_prompt
        response = await self._request(
            "POST",
            f"{self._base()}/messages",
            headers={"Content-Type": "application/json"},
            json=payload,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned malformed JSON", retryable=True, code="provider_malformed_response") from exc
        content = data.get("content") if isinstance(data, Mapping) else None
        if not isinstance(content, list):
            raise ProviderError("provider returned no content", retryable=True, code="provider_no_candidates")
        text = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
        if not text.strip():
            raise ProviderError("provider returned no text", retryable=True, code="provider_empty_response")
        return text

    async def discover_models(self) -> list[str]:
        response = await self._request("GET", f"{self._base()}/models")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned malformed model list", code="provider_malformed_response") from exc
        values = data.get("data", []) if isinstance(data, Mapping) else []
        result = [str(item["id"]) for item in values if isinstance(item, Mapping) and item.get("id")]
        return sorted(dict.fromkeys(result))


OpenAIProvider = OpenAICompatibleProvider


def create_provider(config: ProviderConfig | Mapping[str, Any], client: httpx.AsyncClient | None = None) -> VisionProvider:
    cfg = config if isinstance(config, ProviderConfig) else ProviderConfig.from_mapping(config)
    kind = cfg.kind
    if kind is ProviderKind.GEMINI:
        return GeminiProvider(cfg, client=client)
    if kind is ProviderKind.ANTIGRAVITY:
        return AntigravityProvider(cfg, client=client)
    if kind is ProviderKind.LM_STUDIO:
        return LMStudioProvider(cfg, client=client)
    if kind is ProviderKind.CLAUDE or (kind is ProviderKind.CUSTOM and cfg.protocol is ProviderProtocol.CLAUDE):
        return ClaudeProvider(cfg, client=client)
    if kind is ProviderKind.CUSTOM and cfg.protocol is ProviderProtocol.GEMINI:
        return GeminiProvider(cfg, client=client)
    return OpenAICompatibleProvider(cfg, client=client)


__all__ = [
    "AntigravityProvider",
    "ClaudeProvider",
    "GeminiProvider",
    "LMStudioProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "VisionProvider",
    "create_provider",
]
