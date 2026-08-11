# Ported verbatim from the e621-standard-caption-workflow project
# (shared/anima_caption_format) to keep rule-stage behaviour identical.
# Only the module docstring/import paths are adapted for this package.
"""Deterministic module-6 flat TXT serialization for an already valid payload."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping

from .normalizer import ARRAY_FIELDS, CaptionDisplayPolicy, FIELDS, display_tag, flat_txt_representable


_TAG_FIELDS = FIELDS[:-1]


class FlatTextSerializationError(ValueError):
    pass


def _display_tag(value: str, policy: CaptionDisplayPolicy) -> str:
    result = display_tag(value, policy)
    if not flat_txt_representable(result):
        raise FlatTextSerializationError("normalized tag is not representable in flat TXT")
    return result


def _tag_section(payload: Mapping[str, object], field: str, policy: CaptionDisplayPolicy) -> str:
    value = payload[field]
    if field in ARRAY_FIELDS:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise FlatTextSerializationError(f"{field} is not a normalized string array")
        values = value
    elif field == "character":
        if not isinstance(value, str):
            raise FlatTextSerializationError("character is not a normalized string")
        values = [part.strip() for part in value.split(",") if part.strip()]
    else:
        if not isinstance(value, str):
            raise FlatTextSerializationError(f"{field} is not a normalized string")
        values = [value] if value else []
    return ", ".join(_display_tag(item, policy) for item in values)


def serialize_flat_txt(payload: Mapping[str, object], caption_policy: CaptionDisplayPolicy) -> bytes:
    """Serialize a normalized payload without changing JSON field semantics."""
    if set(payload) != set(FIELDS):
        raise FlatTextSerializationError("flat TXT requires exactly the normalized nine fields")
    tag_sections = [_tag_section(payload, field, caption_policy) for field in _TAG_FIELDS]
    first = next((index for index, section in enumerate(tag_sections) if section), None)
    triggers = [_display_tag(term, caption_policy) for term in caption_policy.trigger_terms] if caption_policy.triggers_enabled else []
    if triggers:
        if first is None:
            tag_sections.insert(0, ", ".join(triggers))
        else:
            tag_sections[first] = ", ".join([*triggers, tag_sections[first]])
    sections = [section for section in tag_sections if section]
    nl = payload["nl"]
    if not isinstance(nl, str):
        raise FlatTextSerializationError("nl is not a normalized string")
    if nl:
        sections.append(nl)
    if not sections:
        raise FlatTextSerializationError("flat TXT cannot serialize an empty payload")
    result = ", \n\n".join(sections).rstrip()
    if not result.endswith("."):
        result += "."
    return result.encode("utf-8")


def flat_txt_sha256(payload: Mapping[str, object], caption_policy: CaptionDisplayPolicy) -> str:
    return hashlib.sha256(serialize_flat_txt(payload, caption_policy)).hexdigest()
