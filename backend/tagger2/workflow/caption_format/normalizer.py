# Ported verbatim from the e621-standard-caption-workflow project
# (shared/anima_caption_format) to keep rule-stage behaviour identical.
# Only the module docstring/import paths are adapted for this package.
"""Pure, deterministic structural validation for caption exports.

This module deliberately has no filesystem or core dependency.  The export
worker uses it for one JSON document at a time, and the core decides how to
persist issues or commit a validated result.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping


MAX_JSON_BYTES = 1_048_576
MAX_NL_BYTES = 16 * 1024
ARRAY_FIELDS = ("quality", "appearance", "tags", "environment")
STRING_FIELDS = ("count", "character", "series", "artist", "nl")
FIELDS = ("quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl")
COUNT_VALUES = frozenset({"", "solo", "duo", "trio", "group"})
# `count` reuses the module-2 bucket labels, which legitimately repeat inside `tags`.
COLLISION_EXEMPT_FIELDS = frozenset({"count"})
# Escape unescaped parentheses only; `\(` / `\)` already carry their escape.
_ESCAPE_PATTERN = re.compile(r"(?<!\\)([()])")


@dataclass(frozen=True)
class FieldError:
    code: str
    field: str | None = None


@dataclass(frozen=True)
class CaptionDisplayPolicy:
    replace_underscores_with_spaces: bool
    preserve_escapes: bool
    triggers_enabled: bool
    trigger_terms: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "CaptionDisplayPolicy":
        terms = value.get("triggerTerms", ())
        if not isinstance(terms, (list, tuple)) or not all(isinstance(term, str) for term in terms):
            raise ValueError("caption display trigger terms are invalid")
        flags = (
            value.get("replaceUnderscoresWithSpaces"),
            value.get("preserveEscapes"),
            value.get("triggersEnabled"),
        )
        if not all(type(flag) is bool for flag in flags):
            raise ValueError("caption display flags are invalid")
        return cls(flags[0], flags[1], flags[2], tuple(terms))  # type: ignore[arg-type]


@dataclass(frozen=True)
class NormalizationResult:
    payload: dict[str, object] | None
    json_bytes: bytes | None
    field_errors: tuple[FieldError, ...]
    conversions: dict[str, int]

    @property
    def valid(self) -> bool:
        return self.payload is not None and not self.field_errors


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _parse_object(raw: bytes) -> tuple[dict[str, object] | None, tuple[FieldError, ...]]:
    if not raw:
        return None, (FieldError("json_missing_or_blank"),)
    if len(raw) > MAX_JSON_BYTES:
        return None, (FieldError("json_too_large"),)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, (FieldError("json_invalid_encoding"),)
    if not text.strip():
        return None, (FieldError("json_missing_or_blank"),)
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicates, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError):
        return None, (FieldError("json_syntax_invalid"),)
    if not isinstance(value, dict):
        return None, (FieldError("json_root_not_object"),)
    return value, ()


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _split_array_items(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_array(field: str, value: object, conversions: dict[str, int]) -> tuple[list[str] | None, list[FieldError]]:
    if value is None or value == "":
        if value is not None:
            conversions["array_empty_to_list"] = conversions.get("array_empty_to_list", 0) + 1
        return [], []
    if isinstance(value, str):
        conversions["array_string_split"] = conversions.get("array_string_split", 0) + 1
        source = [value]
    elif isinstance(value, list):
        source = value
    else:
        return None, [FieldError("field_type_invalid", field)]

    result: list[str] = []
    seen: set[str] = set()
    errors: list[FieldError] = []
    for item in source:
        if not isinstance(item, str):
            errors.append(FieldError("array_element_type_invalid", field))
            continue
        for part in _split_array_items(item):
            key = part.casefold()
            if key in seen:
                conversions["array_duplicate_removed"] = conversions.get("array_duplicate_removed", 0) + 1
                continue
            seen.add(key)
            result.append(part)
    return (None if errors else result), errors


def _normalize_string(field: str, value: object, conversions: dict[str, int]) -> tuple[str | None, list[FieldError]]:
    if value is None or value == []:
        if value is not None:
            conversions["string_empty_to_empty"] = conversions.get("string_empty_to_empty", 0) + 1
        return "", []
    if isinstance(value, str):
        return value.strip(), []
    if isinstance(value, list):
        if len(value) != 1 or not isinstance(value[0], str):
            return None, [FieldError("field_type_invalid", field)]
        conversions["single_string_array_unwrapped"] = conversions.get("single_string_array_unwrapped", 0) + 1
        return value[0].strip(), []
    return None, [FieldError("field_type_invalid", field)]


def _normalize_character(value: str, conversions: dict[str, int]) -> str:
    parts = _split_array_items(value)
    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = part.casefold()
        if key not in seen:
            seen.add(key)
            result.append(part)
        else:
            conversions["character_duplicate_removed"] = conversions.get("character_duplicate_removed", 0) + 1
    normalized = ", ".join(result)
    if normalized != value:
        conversions["character_normalized"] = conversions.get("character_normalized", 0) + 1
    return normalized


def display_tag(value: str, policy: CaptionDisplayPolicy) -> str:
    """Single source of truth for the flat TXT display form of one tag."""
    result = value.replace("_", " ") if policy.replace_underscores_with_spaces else value
    if policy.preserve_escapes:
        result = _ESCAPE_PATTERN.sub(r"\\\1", result)
    return result


def flat_txt_representable(value: str) -> bool:
    return bool(value) and value == value.strip() and not any(character in value for character in ",\r\n\x00")


def _tag_values(payload: Mapping[str, object]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for field in FIELDS[:-1]:
        value = payload[field]
        if field in ARRAY_FIELDS:
            values.extend((field, item) for item in value)  # type: ignore[union-attr]
        elif field == "character":
            values.extend((field, item.strip()) for item in str(value).split(",") if item.strip())
        elif value:
            values.append((field, str(value)))
    return values


def _semantic_errors(payload: Mapping[str, object], policy: CaptionDisplayPolicy, display_checks: bool) -> list[FieldError]:
    errors: list[FieldError] = []
    raw_seen: dict[str, str] = {}
    formatted_seen: dict[str, str] = {}
    tags = _tag_values(payload)
    for field, value in tags:
        if _contains_control(value):
            errors.append(FieldError("tag_control_character", field))
            continue
        if field in COLLISION_EXEMPT_FIELDS:
            continue
        key = value.casefold()
        previous = raw_seen.get(key)
        if previous is not None and previous != field:
            errors.append(FieldError("cross_field_tag_collision", field))
        else:
            raw_seen.setdefault(key, field)
        if not display_checks:
            continue
        formatted = display_tag(value, policy)
        if not flat_txt_representable(formatted):
            errors.append(FieldError("tag_not_flat_txt_representable", field))
        display = formatted.casefold()
        previous_display = formatted_seen.get(display)
        if previous_display is not None and previous_display != field:
            errors.append(FieldError("formatted_tag_collision", field))
        else:
            formatted_seen.setdefault(display, field)
    if display_checks and policy.triggers_enabled:
        for trigger in policy.trigger_terms:
            display = display_tag(trigger, policy).casefold()
            if display in formatted_seen:
                errors.append(FieldError("trigger_tag_collision"))
    return errors


def normalize_json_bytes(raw: bytes, caption_policy: CaptionDisplayPolicy, *, export_format: str = "both") -> NormalizationResult:
    """Validate one raw business JSON document without writing any filesystem state.

    Display-layer checks (flat TXT representability, formatted and trigger
    collisions) only apply when a flat TXT file is actually produced.
    """
    if export_format not in {"json", "flat_txt", "both"}:
        raise ValueError("export format is invalid")
    document, parse_errors = _parse_object(raw)
    if document is None:
        return NormalizationResult(None, None, parse_errors, {})

    conversions: dict[str, int] = {}
    errors: list[FieldError] = []
    extra = set(document) - set(FIELDS)
    errors.extend(FieldError("extra_field", field) for field in sorted(extra))
    normalized: dict[str, object] = {}
    for field in ARRAY_FIELDS:
        value, field_errors = _normalize_array(field, document.get(field), conversions)
        if field not in document:
            conversions["missing_field_defaulted"] = conversions.get("missing_field_defaulted", 0) + 1
        errors.extend(field_errors)
        if value is not None:
            normalized[field] = value
    for field in STRING_FIELDS:
        value, field_errors = _normalize_string(field, document.get(field), conversions)
        if field not in document:
            conversions["missing_field_defaulted"] = conversions.get("missing_field_defaulted", 0) + 1
        errors.extend(field_errors)
        if value is not None:
            normalized[field] = value

    if len(normalized) != len(FIELDS):
        return NormalizationResult(None, None, tuple(errors), conversions)
    normalized["character"] = _normalize_character(str(normalized["character"]), conversions)
    nl = str(normalized["nl"])
    if _contains_control(nl):
        # Newlines and tabs are permitted whitespace; all other controls are not.
        if any(character not in "\r\n\t" and unicodedata.category(character) == "Cc" for character in nl):
            errors.append(FieldError("nl_control_character", "nl"))
    normalized["nl"] = " ".join(nl.split())
    if len(str(normalized["nl"]).encode("utf-8")) > MAX_NL_BYTES:
        errors.append(FieldError("nl_too_large", "nl"))
    if normalized["count"] not in COUNT_VALUES:
        errors.append(FieldError("count_invalid", "count"))
    errors.extend(_semantic_errors(normalized, caption_policy, export_format != "json"))
    if not any(normalized[field] for field in FIELDS):
        errors.append(FieldError("payload_all_empty"))
    if errors:
        return NormalizationResult(None, None, tuple(errors), conversions)

    ordered = {field: normalized[field] for field in FIELDS}
    data = (json.dumps(ordered, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return NormalizationResult(ordered, data, (), conversions)


normalize_annotation = normalize_json_bytes
