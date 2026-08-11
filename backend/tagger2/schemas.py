"""Public data-transfer objects and Anima response normalisation.

The API deliberately uses plain, structured values.  Nothing in this module
contains HTML or a server-side path.  Keeping the DTOs here gives the HTTP
layer and the worker layer one contract to share without importing either one.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Iterable, Literal, Mapping, Sequence

try:  # pydantic v2 is the supported runtime
    from pydantic import (
        BaseModel,
        ConfigDict,
        Field,
        StrictStr,
        field_validator,
    )
except ImportError:  # pragma: no cover - useful for lightweight tooling
    from pydantic import BaseModel, Field, StrictStr, validator as field_validator  # type: ignore

    ConfigDict = dict  # type: ignore

from .anima import AnimaPayload


ANIMA_KEYS = (
    "quality",
    "count",
    "character",
    "series",
    "artist",
    "appearance",
    "tags",
    "environment",
    "nl",
)
ANIMA_ARRAY_KEYS = ("quality", "appearance", "tags", "environment")


class JobMode(str, Enum):
    LOCAL = "local"
    ONLINE = "online"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ItemStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderKind(str, Enum):
    CUSTOM = "custom"
    GEMINI = "gemini"
    OPENAI = "openai"
    CLAUDE = "claude"
    LM_STUDIO = "lm_studio"
    ANTIGRAVITY = "antigravity"


class ArtifactKind(str, Enum):
    JSON = "json"
    TXT = "txt"


class DTOModel(BaseModel):
    """Base model shared by public DTOs.

    ``extra=forbid`` is important for the Anima wire format: accepting an
    unexpected field would silently make malformed model output look valid.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.strip().split())
    return str(value).strip()


def _tag_list(value: Any) -> list[str]:
    """Convert a model-provided tag collection into clean, unique strings."""

    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_text(item)
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def normalize_trigger_artist(value: str | None) -> str:
    """Return a stable artist trigger token.

    The canonical Anima parser preserves a meaningful leading ``@`` while
    removing transport whitespace/separators; generated tag arrays still
    remove both spellings when the trigger is present.
    """

    try:
        from .anima import normalize_trigger_artist as _canonical

        return _canonical(value or "")
    except Exception:
        return _clean_text(value).strip(" ,")


class TagItem(DTOModel):
    text: StrictStr = Field(min_length=1, max_length=512)
    category: StrictStr = Field(default="general", min_length=1, max_length=64)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    source: StrictStr = Field(default="local", min_length=1, max_length=64)
    model_id: StrictStr = Field(default="", max_length=256)

    @field_validator("text", "category", "source", "model_id", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return _clean_text(value)


class ArtifactRef(DTOModel):
    kind: ArtifactKind | str
    path: StrictStr = Field(min_length=1)
    size: int | None = Field(default=None, ge=0)
    sha256: StrictStr | None = None


class ImageTiming(DTOModel):
    total_ms: float | None = Field(default=None, ge=0)
    preprocess_ms: float | None = Field(default=None, ge=0)
    inference_ms: float | None = Field(default=None, ge=0)
    write_ms: float | None = Field(default=None, ge=0)


class ModelResult(DTOModel):
    model_id: StrictStr = Field(min_length=1, max_length=256)
    model_name: StrictStr = Field(min_length=1, max_length=256)
    tags: list[TagItem] = Field(default_factory=list)


class ImageResult(DTOModel):
    image_id: StrictStr = Field(min_length=1)
    file_name: StrictStr = Field(min_length=1)
    status: ItemStatus
    model_id: StrictStr | None = Field(default=None, max_length=256)
    tags: list[TagItem] = Field(default_factory=list)
    caption: StrictStr | None = None
    anima: AnimaPayload | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[StrictStr] = Field(default_factory=list)
    timing: ImageTiming | None = None
    model_results: list[ModelResult] = Field(default_factory=list)


class JobEvent(DTOModel):
    seq: int = Field(ge=0)
    job_id: StrictStr = Field(min_length=1)
    state: JobState
    phase: StrictStr = Field(default="", max_length=128)
    processed: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    current_item: StrictStr | None = None
    rate: float | None = Field(default=None, ge=0)
    eta: float | None = Field(default=None, ge=0)
    error: StrictStr | None = None


class ErrorEnvelope(DTOModel):
    code: StrictStr = Field(min_length=1, max_length=128)
    message: StrictStr = Field(min_length=1, max_length=2048)
    fields: dict[str, Any] | None = None
    request_id: StrictStr = Field(min_length=1)
    retryable: bool = False


class RootRef(DTOModel):
    root_id: StrictStr = Field(min_length=1)
    label: StrictStr = Field(min_length=1)
    kind: StrictStr = Field(min_length=1)


class ScanRequest(DTOModel):
    root_id: StrictStr = Field(min_length=1)
    relative_path: StrictStr = ""
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=2000)
    extensions: list[StrictStr] = Field(default_factory=list)


class UploadResponse(DTOModel):
    upload_id: StrictStr = Field(min_length=1)
    files: list[StrictStr] = Field(default_factory=list)


class ModelPublic(DTOModel):
    model_id: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)
    backend: StrictStr = Field(min_length=1)
    architecture: StrictStr = ""
    input_size: int | tuple[int, int] | None = None
    num_classes: int | None = Field(default=None, ge=0)
    loaded: bool = False
    unsafe_weights: bool = False
    adapter_types: list[StrictStr] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)


class ModelLoadRequest(DTOModel):
    adapter_type: StrictStr = "none"
    adapter_path: StrictStr | None = None
    adapter_scale: float = Field(default=1.0, ge=0.0, le=4.0)
    allow_unsafe_pickle: bool = False


class ProviderPublic(DTOModel):
    provider_id: StrictStr = Field(min_length=1)
    kind: ProviderKind
    protocol: str = "openai"
    name: StrictStr = Field(min_length=1)
    base_url: StrictStr = Field(min_length=1)
    configured: bool = False
    key_suffix: StrictStr | None = Field(default=None, min_length=4, max_length=4)
    primary_model: StrictStr | None = None
    fallback_models: list[StrictStr] = Field(default_factory=list)
    retries: int = Field(default=2, ge=0, le=10)


class JobCreateRequest(DTOModel):
    mode: JobMode
    image_ids: list[StrictStr] = Field(default_factory=list)
    root_id: StrictStr | None = None
    relative_paths: list[StrictStr] = Field(default_factory=list)
    model_ids: list[StrictStr] = Field(default_factory=list)
    provider_id: StrictStr | None = None
    online_response: Literal["json", "nl", "nl_tags"] | None = None
    output_root_id: StrictStr | None = None
    output_format: ArtifactKind = ArtifactKind.JSON
    write_txt: bool = False
    overwrite: bool = False
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    category_thresholds: dict[str, float] = Field(default_factory=dict)
    batch_size: int = Field(default=1, ge=1, le=512)
    online_concurrency: int = Field(default=3, ge=1, le=128)
    separate_models: bool = False


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from a provider response.

    Markdown fences and prose around the object are accepted, while malformed
    or unterminated JSON raises ``ValueError``.  Braces inside JSON strings are
    handled correctly.
    """

    clean = _clean_text(text)
    if not clean:
        raise ValueError("empty response")
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        parsed = json.loads(clean)
        if not isinstance(parsed, dict):
            raise ValueError("response is not a JSON object")
        return parsed
    except json.JSONDecodeError:
        pass

    start = clean.find("{")
    if start < 0:
        raise ValueError("response does not contain a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(clean)):
        char = clean[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(clean[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON object: {exc.msg}") from exc
                if not isinstance(parsed, dict):
                    raise ValueError("response is not a JSON object")
                return parsed
    raise ValueError("unterminated JSON object")


def normalize_anima_payload(
    raw: Mapping[str, Any], *, trigger_artist: str = "", require_content: bool = True
) -> dict[str, Any]:
    """Normalise provider output to the exact Anima shape.

    A legacy ``{"image": {"tags": ..., "caption": ...}}`` wrapper is
    accepted solely at this boundary.  The returned dictionary contains no
    metadata and is suitable for direct JSON serialisation.
    """

    try:
        from .anima import normalize_anima_payload as _canonical

        canonical_payload = _canonical(
            raw,
            trigger_artist=trigger_artist,
            require_caption=require_content,
        )
        if hasattr(canonical_payload, "model_dump"):
            return canonical_payload.model_dump(mode="json")
        return dict(canonical_payload)  # pragma: no cover - fallback dataclass
    except ImportError:
        pass
    if not isinstance(raw, Mapping):
        raise ValueError("model output is not a JSON object")
    value = dict(raw)
    if isinstance(value.get("image"), Mapping):
        image = value["image"]
        value = {
            "quality": value.get("quality", []),
            "count": value.get("count", ""),
            "character": value.get("character", ""),
            "series": value.get("series", ""),
            "artist": value.get("artist", ""),
            "appearance": value.get("appearance", []),
            "tags": image.get("tags", value.get("tags", [])),
            "environment": value.get("environment", []),
            "nl": image.get("caption", value.get("nl", value.get("caption", ""))),
        }
    missing = [key for key in ANIMA_KEYS if key not in value and key != "artist"]
    # Artist is supplied by the application and can intentionally be absent in
    # provider output; all other keys are part of the strict contract.
    if missing:
        raise ValueError(f"incomplete output: missing keys {', '.join(missing)}")

    trigger = normalize_trigger_artist(trigger_artist)
    blocked = {trigger.casefold()} if trigger else set()
    normalized_payload: dict[str, Any] = {
        "quality": _tag_list(value.get("quality")),
        "count": _clean_text(value.get("count")),
        "character": _clean_text(value.get("character")),
        "series": _clean_text(value.get("series")),
        "artist": trigger,
        "appearance": _tag_list(value.get("appearance")),
        "tags": _tag_list(value.get("tags")),
        "environment": _tag_list(value.get("environment")),
        "nl": _clean_text(value.get("nl") or value.get("caption")),
    }
    seen: set[str] = set()
    for key in ANIMA_ARRAY_KEYS:
        filtered: list[str] = []
        for item in normalized_payload[key]:
            marker = item.casefold()
            if marker in blocked or marker in seen:
                continue
            seen.add(marker)
            filtered.append(item)
        normalized_payload[key] = filtered
    if not normalized_payload["nl"]:
        raise ValueError("missing nl caption")
    if require_content and not any(
        normalized_payload[key] for key in ("quality", "appearance", "tags", "environment")
    ):
        raise ValueError("missing tag arrays")
    # Validate once more so callers get the same strict shape and no unknown
    # fields can sneak into the persisted artifact.
    return AnimaPayload.model_validate(normalized_payload).model_dump(mode="json")


def parse_anima_response(text: str, *, trigger_artist: str = "") -> dict[str, Any]:
    try:
        from .anima import parse_anima_response as _canonical

        payload = _canonical(text, trigger_artist=trigger_artist)
        return payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
    except ImportError:
        return normalize_anima_payload(extract_json_object(text), trigger_artist=trigger_artist)


__all__ = [
    "ANIMA_KEYS",
    "ANIMA_ARRAY_KEYS",
    "JobMode",
    "JobState",
    "ItemStatus",
    "ProviderKind",
    "ArtifactKind",
    "TagItem",
    "AnimaPayload",
    "ArtifactRef",
    "ImageTiming",
    "ModelResult",
    "ImageResult",
    "JobEvent",
    "ErrorEnvelope",
    "RootRef",
    "ScanRequest",
    "UploadResponse",
    "ModelPublic",
    "ModelLoadRequest",
    "ProviderPublic",
    "JobCreateRequest",
    "normalize_trigger_artist",
    "extract_json_object",
    "normalize_anima_payload",
    "parse_anima_response",
]
