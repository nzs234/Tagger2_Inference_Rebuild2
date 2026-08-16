"""Provider configuration, key rotation and shared HTTP primitives."""

from __future__ import annotations

import asyncio
import email.utils
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

import httpx

from ..security import SecurityError, validate_provider_url


class ProviderKind(str, Enum):
    CUSTOM = "custom"
    GEMINI = "gemini"
    XAI = "xai"
    OPENAI = "openai"
    CLAUDE = "claude"
    LM_STUDIO = "lm_studio"
    ANTIGRAVITY = "antigravity"


class ProviderProtocol(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    CLAUDE = "claude"


class ProviderError(RuntimeError):
    """A sanitized provider failure suitable for API responses and logs."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        code: str = "provider_error",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.code = code
        self.retry_after = retry_after


def _coerce_kind(value: ProviderKind | str) -> ProviderKind:
    if isinstance(value, ProviderKind):
        return value
    value = str(getattr(value, "value", value)).strip().lower()
    aliases = {
        "official": ProviderKind.GEMINI,
        "gemini_official": ProviderKind.GEMINI,
        "openai_compatible": ProviderKind.OPENAI,
        "openai-compatible": ProviderKind.OPENAI,
        "lmstudio": ProviderKind.LM_STUDIO,
        "antigravity_gemini": ProviderKind.ANTIGRAVITY,
    }
    if value in aliases:
        return aliases[value]
    return ProviderKind(value)


def validate_base_url(value: str, *, allow_local: bool = False) -> str:
    try:
        normalized = validate_provider_url(
            value,
            allow_local=allow_local,
            resolve_dns=False,
        )
    except SecurityError as exc:
        raise ValueError(str(exc)) from exc
    return normalized.rstrip("/")


@dataclass(slots=True)
class ProviderConfig:
    kind: ProviderKind | str
    base_url: str
    model: str
    protocol: ProviderProtocol | str | None = None
    id: str | None = None
    name: str | None = None
    backup_model: str | None = None
    fallback_models: tuple[str, ...] = ()
    api_key: str | None = None
    api_keys: tuple[str, ...] = ()
    temperature: float = 0.7
    top_p: float = 0.95
    top_k: int | None = 40
    max_output_tokens: int = 8192
    timeout_seconds: float = 120.0
    max_concurrency: int = 3
    max_retries: int = 2
    retry_base_seconds: float = 1.0
    key_cooldown_seconds: float = 30.0
    allow_local: bool = False
    json_mode: bool = True
    max_image_bytes: int = 20 * 1024 * 1024
    max_source_bytes: int = 64 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_image_dimension: int = 4096
    headers: Mapping[str, str] = field(default_factory=dict)
    prompt_profile: str | None = None

    def __post_init__(self) -> None:
        self.kind = _coerce_kind(self.kind)
        if self.protocol is None:
            inferred = {
                ProviderKind.GEMINI: ProviderProtocol.GEMINI,
                ProviderKind.ANTIGRAVITY: ProviderProtocol.GEMINI,
                ProviderKind.CLAUDE: ProviderProtocol.CLAUDE,
            }.get(self.kind, ProviderProtocol.OPENAI)
            self.protocol = inferred
        else:
            self.protocol = ProviderProtocol(str(getattr(self.protocol, "value", self.protocol)).strip().lower())
        if self.kind is not ProviderKind.CUSTOM:
            self.protocol = {
                ProviderKind.GEMINI: ProviderProtocol.GEMINI,
                ProviderKind.ANTIGRAVITY: ProviderProtocol.GEMINI,
                ProviderKind.CLAUDE: ProviderProtocol.CLAUDE,
                ProviderKind.XAI: ProviderProtocol.OPENAI,
            }.get(self.kind, ProviderProtocol.OPENAI)
        self.base_url = validate_base_url(self.base_url, allow_local=self.allow_local)
        self.model = str(self.model).strip()
        if not self.model:
            raise ValueError("provider model is required")
        self.backup_model = str(self.backup_model).strip() if self.backup_model else None
        fallbacks = [str(item).strip() for item in (self.fallback_models or ()) if str(item).strip()]
        if self.backup_model and self.backup_model not in fallbacks:
            fallbacks.insert(0, self.backup_model)
        self.fallback_models = tuple(dict.fromkeys(fallbacks))
        self.backup_model = self.fallback_models[0] if self.fallback_models else None
        keys: list[str] = []
        for key in ((self.api_key,) if self.api_key else ()) + tuple(self.api_keys or ()):
            key = str(key).strip()
            if key and key not in keys:
                keys.append(key)
        self.api_keys = tuple(keys)
        self.api_key = keys[0] if keys else None
        if not 0 <= float(self.temperature) <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if not 0 <= float(self.top_p) <= 1:
            raise ValueError("top_p must be between 0 and 1")
        if (self.top_k is not None and int(self.top_k) < 0) or int(self.max_output_tokens) <= 0:
            raise ValueError("top_k and max_output_tokens are invalid")
        if float(self.timeout_seconds) <= 0 or int(self.max_concurrency) <= 0:
            raise ValueError("timeout and concurrency must be positive")
        if int(self.max_retries) < 0 or float(self.retry_base_seconds) < 0:
            raise ValueError("retry settings are invalid")
        if min(
            int(self.max_image_bytes),
            int(self.max_source_bytes),
            int(self.max_image_pixels),
            int(self.max_image_dimension),
        ) <= 0:
            raise ValueError("image limits must be positive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProviderConfig":
        data = dict(value)
        if "api_url" in data and "base_url" not in data:
            data["base_url"] = data.pop("api_url")
        if "endpoint" in data and "base_url" not in data:
            data["base_url"] = data.pop("endpoint")
        if "api_type" in data and "kind" not in data:
            data["kind"] = data.pop("api_type")
        if "provider_id" in data and "id" not in data:
            data["id"] = data.pop("provider_id")
        if "primary_model" in data and "model" not in data:
            data["model"] = data.pop("primary_model")
        if "max_tokens" in data and "max_output_tokens" not in data:
            data["max_output_tokens"] = data.pop("max_tokens")
        if "retry_count" in data and "max_retries" not in data:
            data["max_retries"] = data.pop("retry_count")
        if "retry_delay" in data and "retry_base_seconds" not in data:
            data["retry_base_seconds"] = data.pop("retry_delay")
        if "concurrent_count" in data and "max_concurrency" not in data:
            data["max_concurrency"] = data.pop("concurrent_count")
        if "concurrency" in data and "max_concurrency" not in data:
            data["max_concurrency"] = data.pop("concurrency")
        if "retries" in data and "max_retries" not in data:
            data["max_retries"] = data.pop("retries")
        if "timeout" in data and "timeout_seconds" not in data:
            data["timeout_seconds"] = data.pop("timeout")
        # UI/API keys may arrive as a list or a newline-separated string.
        keys = data.get("api_keys", ())
        if isinstance(keys, str):
            data["api_keys"] = tuple(line.strip() for line in keys.replace(",", "\n").splitlines() if line.strip())
        fallbacks = data.get("fallback_models", ())
        if isinstance(fallbacks, str):
            data["fallback_models"] = tuple(line.strip() for line in fallbacks.replace(",", "\n").splitlines() if line.strip())
        return cls(**data)

    def public_dict(self) -> dict[str, Any]:
        key = self.api_key
        kind = _coerce_kind(self.kind)
        return {
            "id": self.id,
            "name": self.name or kind.value,
            "kind": kind.value,
            "protocol": str(getattr(self.protocol, "value", self.protocol)),
            "base_url": self.base_url,
            "model": self.model,
            "backup_model": self.backup_model,
            "fallback_models": list(self.fallback_models),
            "secret_configured": bool(self.api_keys),
            "secret_last4": key[-4:] if key else None,
            "max_concurrency": self.max_concurrency,
            "max_retries": self.max_retries,
        }


class APIKeyPool:
    """Thread/async-safe round-robin pool with temporary key cooling."""

    def __init__(self, keys: Any = ()) -> None:
        if isinstance(keys, str):
            keys = [keys]
        self._keys = tuple(dict.fromkeys(str(k).strip() for k in (keys or ()) if str(k).strip()))
        self._index = 0
        self._cooldowns: dict[str, float] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def keys(self) -> tuple[str, ...]:
        return self._keys

    def next_key(self) -> str | None:
        if not self._keys:
            return None
        now = time.monotonic()
        with self._lock:
            for offset in range(len(self._keys)):
                index = (self._index + offset) % len(self._keys)
                key = self._keys[index]
                if self._cooldowns.get(key, 0.0) <= now:
                    self._index = (index + 1) % len(self._keys)
                    return key
            # All keys are cooling.  Use the one that becomes available first;
            # the caller's retry delay prevents a hot spin.
            key = min(self._keys, key=lambda item: self._cooldowns.get(item, 0.0))
            self._index = (self._keys.index(key) + 1) % len(self._keys)
            return key

    next = next_key

    def cooldown(self, key: str | None, seconds: float) -> None:
        if key and seconds > 0:
            with self._lock:
                self._cooldowns[key] = max(self._cooldowns.get(key, 0.0), time.monotonic() + seconds)

    def wait_seconds(self, key: str | None = None) -> float:
        if not key:
            return 0.0
        with self._lock:
            return max(0.0, self._cooldowns.get(key, 0.0) - time.monotonic())


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except (ValueError, TypeError):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - datetime.now(timezone.utc).timestamp())
        except (TypeError, ValueError, OverflowError):
            return None


def load_api_keys_from_file(path: str) -> list[str]:
    """Read a newline-delimited key pool without retaining comments/URLs."""

    result: list[str] = []
    seen: set[str] = set()
    with open(path, "r", encoding="utf-8-sig") as stream:
        for line in stream:
            key = line.strip().strip("\"'")
            if not key or key.startswith("#"):
                continue
            if "key=" in key:
                key = key.split("key=", 1)[1].split("&", 1)[0].strip()
            if key and key not in seen:
                seen.add(key)
                result.append(key)
    return result


def backoff_seconds(base: float, attempt: int, retry_after: float | None = None) -> float:
    value = float(retry_after) if retry_after is not None else float(base) * (2**attempt)
    # Small jitter prevents a pool of workers from retrying in lockstep.  Keep
    # it bounded so tests and UI estimates remain predictable.
    if retry_after is None and value > 0:
        value += random.uniform(0.0, min(0.25, value * 0.1))
    return min(120.0, max(0.0, value))


RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def response_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    retryable = status in RETRYABLE_STATUS_CODES
    retry_after = parse_retry_after(response.headers.get("retry-after"))
    if status in {401, 403}:
        code = "provider_auth"
        message = "provider authentication failed"
    elif status == 429:
        code = "provider_rate_limited"
        message = "provider rate limit reached"
    elif retryable:
        code = "provider_temporary_error"
        message = f"provider temporarily unavailable (HTTP {status})"
    else:
        code = "provider_http_error"
        message = f"provider request failed (HTTP {status})"
    return ProviderError(message, status_code=status, retryable=retryable, code=code, retry_after=retry_after)


class ProviderHTTPMixin:
    """Shared retrying request implementation for provider subclasses."""

    config: ProviderConfig
    key_pool: APIKeyPool
    client: httpx.AsyncClient
    semaphore: asyncio.Semaphore
    validate_destination: bool

    async def _request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, json: Any = None) -> httpx.Response:
        request_headers = dict(self.config.headers)
        request_headers.update(headers or {})
        async with self.semaphore:
            last: ProviderError | None = None
            for attempt in range(self.config.max_retries + 1):
                key = self.key_pool.next_key()
                call_headers = dict(request_headers)
                self._inject_key(call_headers, key)
                try:
                    if self.validate_destination:
                        validate_provider_url(
                            url,
                            allow_local=self.config.allow_local,
                            resolve_dns=True,
                        )
                    response = await self.client.request(method, url, headers=call_headers, json=json)
                except SecurityError as exc:
                    raise ProviderError(
                        "provider destination is not allowed",
                        retryable=False,
                        code="provider_destination_blocked",
                    ) from exc
                except httpx.TimeoutException:
                    last = ProviderError("provider request timed out", retryable=True, code="provider_timeout")
                except httpx.RequestError:
                    last = ProviderError("provider connection failed", retryable=True, code="provider_connection")
                else:
                    if 200 <= response.status_code < 300:
                        return response
                    last = response_error(response)
                    if last.status_code in {401, 403}:
                        raise last
                if last is None or not last.retryable or attempt >= self.config.max_retries:
                    raise last or ProviderError("provider request failed")
                delay = backoff_seconds(self.config.retry_base_seconds, attempt, last.retry_after)
                if last.status_code == 429:
                    self.key_pool.cooldown(key, max(self.config.key_cooldown_seconds, delay))
                    # Retry immediately with a different credential.  The
                    # Retry-After value still cools the rate-limited key.
                    if len(self.key_pool) > 1:
                        delay = 0.0
                else:
                    self.key_pool.cooldown(key, delay)
                await asyncio.sleep(delay)
            raise last or ProviderError("provider request failed")

    def _inject_key(self, headers: dict[str, str], key: str | None) -> None:
        if key:
            headers.setdefault("Authorization", f"Bearer {key}")


__all__ = [
    "APIKeyPool",
    "ProviderConfig",
    "ProviderError",
    "ProviderKind",
    "ProviderProtocol",
    "RETRYABLE_STATUS_CODES",
    "backoff_seconds",
    "parse_retry_after",
    "load_api_keys_from_file",
    "normalize_base_url",
    "response_error",
    "validate_base_url",
]

normalize_base_url = validate_base_url
