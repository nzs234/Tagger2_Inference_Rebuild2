"""HTTP adapters for Google Gemini, OpenAI GPT Image and xAI image APIs."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx

from ..providers.base import APIKeyPool, ProviderConfig, ProviderError, response_error
from ..providers.image import PreparedImage
from ..security import UploadValidationError, validate_image_bytes, validate_provider_url
from .capabilities import ImageCapability
from .parser import ParsedImage, parse_response, truncate_debug


@dataclass(frozen=True, slots=True)
class ImageRequest:
    model: str
    prompt: str
    operation: str
    n: int
    aspect_ratio: str | None = None
    image_size: str | None = None
    multi_image_strategy: str = "parallel"
    include_text_modality: bool = False
    system_instruction: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    size: str | None = None
    quality: str | None = None
    background: str | None = None
    output_format: str | None = None
    output_compression: int | None = None
    moderation: str | None = None
    input_fidelity: str | None = None
    response_format: str | None = None


@dataclass(slots=True)
class ResolvedImage:
    data: bytes
    mime_type: str
    source: str
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class ImageCallResult:
    images: list[ResolvedImage] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    parser_route: str | None = None
    finish_reason: str | None = None
    hint: str | None = None
    effective_endpoint: str = ""
    response_preview: Any = None


class ImageGenerationClient:
    """One bounded, redirect-free image API client.

    The client is intentionally separate from :class:`VisionProvider`: that
    class has a stable text-returning contract used by the tagging workflow.
    """

    def __init__(
        self,
        config: ProviderConfig,
        *,
        family: str,
        base_url: str,
        api_style: str,
        capability: ImageCapability,
        max_output_bytes: int,
        max_pixels: int,
        max_edge: int,
        max_response_bytes: int | None = None,
        key_pool: APIKeyPool | None = None,
    ) -> None:
        self.config = config
        self.family = family
        self.base_url = base_url.rstrip("/")
        self.api_style = api_style
        self.capability = capability
        self.max_output_bytes = max(64 * 1024, int(max_output_bytes))
        self.max_response_bytes = max(
            1024 * 1024,
            int(max_response_bytes or self.max_output_bytes * 2),
        )
        self.max_pixels = max(1_000_000, int(max_pixels))
        self.max_edge = max(256, int(max_edge))
        self.key_pool = key_pool or APIKeyPool(config.api_keys)

    async def generate(self, request: ImageRequest, references: Sequence[PreparedImage]) -> ImageCallResult:
        timeout = httpx.Timeout(max(1.0, float(self.config.timeout_seconds)), connect=30.0)
        limits = httpx.Limits(max_connections=max(4, min(16, request.n)), max_keepalive_connections=4)
        async with httpx.AsyncClient(timeout=timeout, limits=limits, follow_redirects=False) as client:
            endpoint, payload, files = self._build_request(request, references)
            response = await self._post_with_retries(client, endpoint, payload, files)
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise ProviderError(
                    "provider returned malformed JSON",
                    status_code=response.status_code,
                    retryable=True,
                    code="image_provider_malformed_response",
                ) from exc
            parsed = parse_response(body, max_decoded_bytes=self.max_output_bytes)
            if not parsed.images:
                hint = f" {parsed.hint}" if parsed.hint else ""
                raise ProviderError(
                    f"provider returned no images{hint}".strip(),
                    status_code=response.status_code,
                    retryable=False,
                    code="image_provider_no_images",
                )
            resolved = await self._resolve_images(client, parsed.images)
            if not resolved:
                raise ProviderError(
                    "provider image response could not be validated",
                    status_code=response.status_code,
                    retryable=False,
                    code="image_provider_invalid_image",
                )
            return ImageCallResult(
                images=resolved,
                texts=parsed.texts,
                parser_route=parsed.route,
                finish_reason=parsed.finish_reason,
                hint=parsed.hint,
                effective_endpoint=endpoint,
                response_preview=truncate_debug(body),
            )

    def _base(self, suffix: str) -> str:
        return f"{self.base_url}/{suffix.lstrip('/')}"

    def _google_base(self) -> str:
        base = self.base_url.rstrip("/")
        if not base.endswith(("/v1", "/v1beta")):
            base += "/v1beta"
        return base

    def _openai_base(self) -> str:
        base = self.base_url.rstrip("/")
        final_segment = base.rsplit("/", 1)[-1].casefold()
        if final_segment == "openai" or re.fullmatch(r"v\d+(?:beta)?", final_segment):
            return base
        return base + "/v1"

    @staticmethod
    def _google_generation_config(request: ImageRequest) -> dict[str, Any]:
        modalities = ["IMAGE"] + (["TEXT"] if request.include_text_modality else [])
        generation: dict[str, Any] = {
            "responseModalities": modalities,
            "imageConfig": {},
        }
        if request.aspect_ratio:
            generation["imageConfig"]["aspectRatio"] = request.aspect_ratio
        if request.image_size:
            generation["imageConfig"]["imageSize"] = request.image_size
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.top_p is not None:
            generation["topP"] = request.top_p
        if request.top_k is not None:
            generation["topK"] = request.top_k
        if request.multi_image_strategy == "candidate_count" and request.n > 1:
            generation["candidateCount"] = request.n
        return generation

    @classmethod
    def _google_compatible_extensions(cls, request: ImageRequest) -> dict[str, Any]:
        generation = cls._google_generation_config(request)
        return {
            "generation_config": generation,
            "extra_body": {
                "google": {
                    "aspect_ratio": request.aspect_ratio,
                    "image_size": request.image_size,
                    "generation_config": generation,
                }
            },
        }

    def _build_request(
        self,
        request: ImageRequest,
        references: Sequence[PreparedImage],
    ) -> tuple[str, dict[str, Any], list[tuple[str, tuple[str, bytes, str]]] | None]:
        if self.api_style == "native" or (self.api_style == "auto" and self.family == "google_gemini"):
            return self._build_google(request, references)
        if self.api_style in {"openai_chat", "chat"}:
            return self._build_chat(request, references)
        return self._build_images(request, references)

    def _build_google(
        self,
        request: ImageRequest,
        references: Sequence[PreparedImage],
    ) -> tuple[str, dict[str, Any], None]:
        parts: list[dict[str, Any]] = [
            {"inline_data": {"mime_type": image.mime_type, "data": image.base64}}
            for image in references
        ]
        parts.append({"text": request.prompt})
        generation = self._google_generation_config(request)
        payload: dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": generation,
        }
        if request.system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": request.system_instruction}]}
        endpoint = f"{self._google_base()}/models/{quote(request.model, safe='')}:generateContent"
        return endpoint, payload, None

    def _build_images(
        self,
        request: ImageRequest,
        references: Sequence[PreparedImage],
    ) -> tuple[str, dict[str, Any], list[tuple[str, tuple[str, bytes, str]]] | None]:
        base = self._openai_base()
        values: dict[str, Any] = {"model": request.model, "prompt": request.prompt, "n": request.n}
        for key, value in (
            ("size", request.size),
            ("quality", request.quality),
            ("background", request.background),
            ("output_format", request.output_format),
            ("output_compression", request.output_compression),
            ("moderation", request.moderation),
            ("response_format", request.response_format),
        ):
            if value is not None:
                values[key] = value
        if request.aspect_ratio is not None and self.family == "xai_grok_image":
            values["aspect_ratio"] = request.aspect_ratio
        if self.family == "google_gemini":
            values.update(self._google_compatible_extensions(request))
        if not references:
            return f"{base}/images/generations", values, None
        files = [
            ("image[]", (f"reference-{index}.jpg", image.data, image.mime_type))
            for index, image in enumerate(references)
        ]
        if request.input_fidelity is not None:
            values["input_fidelity"] = request.input_fidelity
        return f"{base}/images/edits", values, files

    def _build_chat(
        self,
        request: ImageRequest,
        references: Sequence[PreparedImage],
    ) -> tuple[str, dict[str, Any], None]:
        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": image.data_url}}
            for image in references
        ]
        content.append({"type": "text", "text": request.prompt})
        messages: list[dict[str, Any]] = []
        if request.system_instruction:
            messages.append({"role": "system", "content": request.system_instruction})
        messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "n": request.n,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.top_p is not None:
            payload["top_p"] = request.top_p
        if self.family == "google_gemini":
            payload.update(self._google_compatible_extensions(request))
            payload["modalities"] = (
                ["image", "text"] if request.include_text_modality else ["image"]
            )
        return f"{self._openai_base()}/chat/completions", payload, None

    async def _post_with_retries(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        payload: Mapping[str, Any],
        files: list[tuple[str, tuple[str, bytes, str]]] | None,
    ) -> httpx.Response:
        try:
            validate_provider_url(endpoint, allow_local=self.config.allow_local, resolve_dns=True)
        except Exception as exc:
            raise ProviderError("image provider destination rejected", code="image_provider_url_blocked") from exc
        last: ProviderError | None = None
        for attempt in range(self.config.max_retries + 1):
            key = self.key_pool.next_key()
            headers = dict(self.config.headers)
            if self.family == "google_gemini" and self.api_style in {"native", "auto"}:
                if key:
                    headers.setdefault("x-goog-api-key", key)
            elif key:
                headers.setdefault("Authorization", f"Bearer {key}")
            try:
                validate_provider_url(endpoint, allow_local=self.config.allow_local, resolve_dns=True)
                if files is None:
                    request = client.build_request(
                        "POST",
                        endpoint,
                        headers=headers,
                        json=dict(payload),
                    )
                else:
                    form_payload = {
                        key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                        if isinstance(value, (dict, list))
                        else value
                        for key, value in payload.items()
                    }
                    request = client.build_request(
                        "POST",
                        endpoint,
                        headers=headers,
                        data=form_payload,
                        files=files,
                    )
                response = await self._send_bounded(client, request)
                if response.status_code >= 400:
                    error = response_error(response)
                    last = ProviderError(
                        error.message,
                        status_code=error.status_code,
                        retryable=error.retryable,
                        code=f"image_{error.code}",
                        retry_after=error.retry_after,
                    )
                    if not error.retryable or attempt >= self.config.max_retries:
                        raise last
                    self.key_pool.cooldown(key, self.config.key_cooldown_seconds)
                    await asyncio.sleep(min(120.0, max(0.0, error.retry_after or self.config.retry_base_seconds * (2**attempt))))
                    continue
                return response
            except asyncio.CancelledError:
                raise
            except ProviderError:
                raise
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                last = ProviderError("image provider request failed", retryable=True, code="image_provider_request_failed")
                if attempt >= self.config.max_retries:
                    raise last from exc
                self.key_pool.cooldown(key, self.config.key_cooldown_seconds)
                await asyncio.sleep(min(30.0, self.config.retry_base_seconds * (2**attempt)))
        raise last or ProviderError("image provider request failed", code="image_provider_request_failed")

    async def _send_bounded(
        self,
        client: httpx.AsyncClient,
        request: httpx.Request,
    ) -> httpx.Response:
        """Buffer a provider JSON envelope without allowing an unbounded body."""

        response = await client.send(request, stream=True)
        try:
            declared = response.headers.get("content-length")
            if declared:
                try:
                    declared_bytes = int(declared)
                except ValueError as exc:
                    raise ProviderError(
                        "provider response size is invalid",
                        code="image_provider_invalid_length",
                    ) from exc
                if declared_bytes > self.max_response_bytes:
                    raise ProviderError(
                        "provider response exceeds size limit",
                        code="image_provider_response_too_large",
                    )
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self.max_response_bytes:
                    raise ProviderError(
                        "provider response exceeds size limit",
                        code="image_provider_response_too_large",
                    )
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=request,
            )
        finally:
            await response.aclose()

    async def _resolve_images(self, client: httpx.AsyncClient, images: Sequence[ParsedImage]) -> list[ResolvedImage]:
        resolved: list[ResolvedImage] = []
        for image in images:
            data = image.data
            mime = image.mime_type
            if data is None and image.url:
                data, mime = await self._fetch_remote_image(client, image.url)
            if not data:
                continue
            try:
                info = validate_image_bytes(
                    data,
                    max_bytes=self.max_output_bytes,
                    max_pixels=self.max_pixels,
                    max_edge=self.max_edge,
                )
            except UploadValidationError as exc:
                raise ProviderError("provider returned invalid image data", code="image_provider_invalid_image") from exc
            resolved.append(
                ResolvedImage(
                    data=data,
                    mime_type=str(info.get("mime") or mime or "image/png"),
                    source=image.source,
                    width=int(info["width"]),
                    height=int(info["height"]),
                )
            )
        return resolved

    async def _fetch_remote_image(self, client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
        try:
            validate_provider_url(
                url,
                allow_local=self.config.allow_local,
                resolve_dns=True,
                allow_query=True,
            )
        except Exception as exc:
            raise ProviderError("provider image URL rejected", code="image_provider_url_blocked") from exc
        try:
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise ProviderError("provider image redirect rejected", code="image_provider_redirect_blocked")
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        declared_bytes = int(declared)
                    except ValueError as exc:
                        raise ProviderError("provider image size is invalid", code="image_provider_invalid_length") from exc
                    if declared_bytes > self.max_output_bytes:
                        raise ProviderError("provider image exceeds size limit", code="image_provider_image_too_large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_output_bytes:
                        raise ProviderError("provider image exceeds size limit", code="image_provider_image_too_large")
                    chunks.append(chunk)
                data = b"".join(chunks)
                mime = response.headers.get("content-type", "image/png").split(";", 1)[0].strip()
                return data, mime if mime.startswith("image/") else "image/png"
        except ProviderError:
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise ProviderError("provider image download failed", retryable=True, code="image_provider_download_failed") from exc


__all__ = ["ImageCallResult", "ImageGenerationClient", "ImageRequest", "ResolvedImage"]
