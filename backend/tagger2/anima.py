"""Strict Anima caption parsing and normalization.

The online providers deliberately return text rather than silently trusting a
model's JSON.  This module is the single boundary where that text becomes an
Anima payload.  It accepts the small amount of formatting commonly emitted by
vision models (markdown fences, leading prose and comma separated tag lists),
then emits exactly the nine fields used by Anima training files.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

try:  # pydantic is a runtime dependency of the new backend.
    from pydantic import BaseModel, ConfigDict
except Exception:  # pragma: no cover - keeps this utility importable in small tools
    BaseModel = None  # type: ignore[assignment,misc]
    ConfigDict = None  # type: ignore[assignment,misc]


ANIMA_JSON_KEYS = (
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
ANIMA_SCHEMA_VERSION = "1"


if BaseModel is not None:

    class AnimaPayload(BaseModel):
        """The exact on-disk Anima object.

        ``extra='forbid'`` is intentional: metadata belongs in SQLite and
        should never accidentally become part of a training caption file.
        Normalization is performed before constructing this model.
        """

        model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

        quality: list[str]
        count: str
        character: str
        series: str
        artist: str
        appearance: list[str]
        tags: list[str]
        environment: list[str]
        nl: str

else:  # pragma: no cover - only used when pydantic is intentionally absent

    @dataclass(frozen=True, slots=True)
    class AnimaPayload:  # type: ignore[no-redef]
        quality: list[str]
        count: str
        character: str
        series: str
        artist: str
        appearance: list[str]
        tags: list[str]
        environment: list[str]
        nl: str

        @classmethod
        def model_validate(cls, value: Mapping[str, Any]) -> "AnimaPayload":
            unknown = set(value) - set(ANIMA_JSON_KEYS)
            if unknown or set(value) != set(ANIMA_JSON_KEYS):
                raise ValueError("Anima payload must contain exactly the nine schema fields")
            return cls(**dict(value))

        def model_dump(self, **_: Any) -> dict[str, Any]:
            return asdict(self)


def _clean_string(value: Any, *, field: str = "value") -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (dict, set)):
        raise ValueError(f"{field} must be a string")
    if isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a string")
    return str(value).strip()


def _as_tag_list(value: Any, *, field: str) -> list[str]:
    """Convert model-friendly list variants into clean individual tags."""

    if value is None:
        return []
    if isinstance(value, str):
        # Models often put one tag per line or join tags with commas.  A comma
        # inside a prose phrase is uncommon in booru tags and can be escaped by
        # returning a JSON array when it matters.
        parts = re.split(r"[,;\n]+", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            if isinstance(item, (list, tuple)):
                result.extend(_as_tag_list(item, field=field))
            elif isinstance(item, str):
                clean = item.strip()
                if clean:
                    result.append(clean)
            else:
                raise ValueError(f"{field} must contain only strings")
        return result
    raise ValueError(f"{field} must be a list of strings")


def _tag_key(value: str) -> str:
    # Treat common separators and whitespace as equivalent while preserving the
    # original spelling in the output.
    return re.sub(r"[_\s]+", " ", value.casefold()).strip()


def _dedupe(tags: Iterable[str], blocked: set[str] | None = None) -> list[str]:
    blocked_keys = {_tag_key(item) for item in (blocked or set()) if item}
    seen: set[str] = set()
    result: list[str] = []
    for item in tags:
        clean = item.strip()
        key = _tag_key(clean)
        if not key or key in blocked_keys or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def normalize_trigger_artist(value: Any) -> str:
    """Normalize a trigger artist token without changing its semantic spelling."""

    text = _clean_string(value, field="trigger_artist")
    # A pasted token can contain a leading/trailing ``@`` or comma.  The token
    # itself remains intact apart from those transport characters.
    return text.strip(" \t\r\n,")


def _extract_legacy(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """Translate the old ``{"image": {"tags", "caption"}}`` shape.

    This narrow compatibility path is useful for responses from older prompt
    profiles.  Unknown top-level keys are still rejected after translation.
    """

    image = raw.get("image")
    if not isinstance(image, Mapping):
        return None
    allowed = {"image", *ANIMA_JSON_KEYS}
    if set(raw) - allowed:
        return None
    return {
        "quality": raw.get("quality", []),
        "count": raw.get("count", ""),
        "character": raw.get("character", ""),
        "series": raw.get("series", ""),
        "artist": raw.get("artist", ""),
        "appearance": raw.get("appearance", []),
        "tags": image.get("tags", raw.get("tags", [])),
        "environment": raw.get("environment", []),
        "nl": image.get("caption", raw.get("nl", "")),
    }


def normalize_anima_payload(
    raw: Mapping[str, Any] | AnimaPayload,
    trigger_artist: str = "",
    *,
    require_caption: bool = True,
) -> AnimaPayload:
    """Validate and normalize a decoded model object.

    All four tag arrays are deduplicated globally in schema order.  A trigger
    artist is written to ``artist`` and removed from every generated tag list.
    """

    if isinstance(raw, AnimaPayload):
        data = _payload_dict(raw)
    elif hasattr(raw, "model_dump"):
        # Accept the shared API DTO (``schemas.AnimaPayload``) without making
        # this low-level module import schemas and creating a cycle.
        data = dict(raw.model_dump())  # type: ignore[union-attr]
    elif isinstance(raw, Mapping):
        legacy = _extract_legacy(raw)
        data = legacy if legacy is not None else dict(raw)
    else:
        raise ValueError("model output is not a JSON object")

    missing = [key for key in ANIMA_JSON_KEYS if key not in data]
    unknown = [key for key in data if key not in ANIMA_JSON_KEYS]
    if missing:
        raise ValueError(f"incomplete output: missing keys {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unexpected Anima fields: {', '.join(sorted(unknown))}")

    trigger = normalize_trigger_artist(trigger_artist)
    blocked = {trigger}
    if trigger.startswith("@"):
        blocked.add(trigger[1:])
    else:
        blocked.add("@" + trigger)

    normalized: dict[str, Any] = {
        "quality": _dedupe(_as_tag_list(data["quality"], field="quality"), blocked),
        "count": _clean_string(data["count"], field="count"),
        "character": _clean_string(data["character"], field="character"),
        "series": _clean_string(data["series"], field="series"),
        "artist": trigger,
        "appearance": _dedupe(_as_tag_list(data["appearance"], field="appearance"), blocked),
        "tags": _dedupe(_as_tag_list(data["tags"], field="tags"), blocked),
        "environment": _dedupe(_as_tag_list(data["environment"], field="environment"), blocked),
        "nl": _clean_string(data["nl"], field="nl"),
    }

    # A model may repeat a concept in multiple arrays (for example ``red fur``
    # in both appearance and tags).  Keep the first occurrence according to
    # the on-disk schema order so the emitted caption is deterministic.
    used: set[str] = set()
    for field in ("quality", "appearance", "tags", "environment"):
        unique: list[str] = []
        for tag in normalized[field]:
            marker = _tag_key(tag)
            if marker in used:
                continue
            used.add(marker)
            unique.append(tag)
        normalized[field] = unique

    # Count tags belong only in ``count``.  If the model omitted count, promote
    # the first conventional count tag; otherwise remove duplicates from tags.
    count_re = re.compile(r"^(?:solo|duo|trio|quartet|multiple characters|\d+(?:boy|boys|girl|girls|person|people))$", re.I)
    if not normalized["count"]:
        for candidate in normalized["tags"]:
            if count_re.match(candidate):
                normalized["count"] = candidate
                break
    normalized["tags"] = [
        tag for tag in normalized["tags"] if not count_re.match(tag) or _tag_key(tag) == _tag_key(normalized["count"])
    ]
    if normalized["count"]:
        normalized["tags"] = [tag for tag in normalized["tags"] if _tag_key(tag) != _tag_key(normalized["count"])]

    if require_caption and not normalized["nl"]:
        raise ValueError("missing nl caption")
    if not any(normalized[name] for name in ("quality", "appearance", "tags", "environment")):
        raise ValueError("missing tag arrays")

    try:
        return AnimaPayload.model_validate(normalized)
    except AttributeError:  # fallback dataclass has the same API in normal use
        return AnimaPayload(**normalized)  # type: ignore[call-arg]
    except Exception as exc:
        raise ValueError(f"invalid Anima payload: {exc}") from exc


def _payload_dict(payload: AnimaPayload | Any) -> dict[str, Any]:
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    return asdict(payload)  # type: ignore[arg-type]


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from a provider response."""

    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty response")
    value = text.strip().lstrip("\ufeff")
    value = re.sub(r"^```(?:json|javascript|js)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```\s*$", "", value)
    try:
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    except json.JSONDecodeError:
        pass

    start = value.find("{")
    if start < 0:
        raise ValueError("response does not contain a JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
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
                candidate = value[start : index + 1]
                try:
                    decoded = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON object: {exc.msg}") from exc
                if not isinstance(decoded, dict):
                    raise ValueError("model output is not a JSON object")
                return decoded
    raise ValueError("incomplete output: unterminated JSON object")


def parse_anima_response(text: str, trigger_artist: str = "", *, require_caption: bool = True) -> AnimaPayload:
    return normalize_anima_payload(
        extract_json_object(text), trigger_artist=trigger_artist, require_caption=require_caption
    )


def anima_dict(payload: AnimaPayload) -> dict[str, Any]:
    """Return a stable, JSON-serializable object in schema order."""

    data = _payload_dict(payload)
    return {key: data[key] for key in ANIMA_JSON_KEYS}


def replace_anima_underscores(payload: AnimaPayload) -> AnimaPayload:
    """Replace underscores in structured Anima tags while preserving ``nl``."""

    data = anima_dict(payload)
    for field in ("quality", "appearance", "tags", "environment"):
        data[field] = [value.replace("_", " ") for value in data[field]]
    for field in ("count", "character", "series", "artist"):
        data[field] = data[field].replace("_", " ")
    return AnimaPayload.model_validate(data)


def anima_json(payload: AnimaPayload, *, indent: int = 2) -> str:
    return json.dumps(anima_dict(payload), ensure_ascii=False, indent=indent)


__all__ = [
    "ANIMA_JSON_KEYS",
    "ANIMA_SCHEMA_VERSION",
    "AnimaPayload",
    "anima_dict",
    "anima_json",
    "extract_json_object",
    "normalize_anima_payload",
    "normalize_trigger_artist",
    "parse_anima_response",
    "replace_anima_underscores",
]
