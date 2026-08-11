# Ported verbatim from the e621-standard-caption-workflow project
# (core/src/anima_core/raw_e621.py). Strict raw e621 grouped JSON parsing:
# a malformed document is an error and never falls back to the tagger.
from __future__ import annotations

import json
import math
from dataclasses import dataclass


MAX_JSON_BYTES = 1_048_576
MAX_TAG_BYTES = 512
MAX_TAGS = 16_384
RAW_E621_GROUPS = (
    "artist",
    "character",
    "contributor",
    "copyright",
    "general",
    "invalid",
    "lore",
    "meta",
    "species",
)
CLASSIFY_GROUPS = ("copyright", "general", "meta", "species")
_RAW_DISTINCTIVE_GROUPS = frozenset(RAW_E621_GROUPS) - {"artist", "character"}


class RawE621JsonError(ValueError):
    pass


@dataclass(frozen=True)
class RawE621Annotation:
    artist: str
    character: str
    classify_tags: tuple[str, ...]


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RawE621JsonError(f"raw E621 JSON has duplicate key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise RawE621JsonError(f"raw E621 JSON contains non-finite value: {value}")


def _is_candidate(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    if _RAW_DISTINCTIVE_GROUPS.intersection(value):
        return True
    return isinstance(value.get("artist"), list) or isinstance(value.get("character"), list)


def _parse(raw: bytes | None) -> object | None:
    if raw is None or not raw:
        return None
    if len(raw) > MAX_JSON_BYTES:
        raise RawE621JsonError("raw E621 JSON exceeds 1 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RawE621JsonError("raw E621 JSON is not UTF-8") from exc
    if not text.strip():
        return None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_object, parse_constant=_reject_nonfinite)
    except RawE621JsonError:
        raise
    except json.JSONDecodeError as exc:
        raise RawE621JsonError("raw E621 JSON is invalid") from exc


def _tags(value: object, group: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RawE621JsonError(f"raw E621 JSON group {group} must be a string array")
    tags: list[str] = []
    for tag in value:
        if not isinstance(tag, str) or not tag or tag != tag.strip():
            raise RawE621JsonError(f"raw E621 JSON group {group} contains an invalid tag")
        if len(tag.encode("utf-8")) > MAX_TAG_BYTES or any(character in tag for character in ",\r\n\x00"):
            raise RawE621JsonError(f"raw E621 JSON group {group} contains an unsafe tag")
        tags.append(tag)
    return tuple(tags)


def _joined(tags: tuple[str, ...]) -> str:
    return ", ".join(tags)


def _classify_tags(groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for group in (*CLASSIFY_GROUPS, "character"):
        for tag in groups[group]:
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                values.append(tag)
                if len(values) > MAX_TAGS:
                    raise RawE621JsonError("raw E621 JSON contains more than 16384 usable tags")
    if not values:
        raise RawE621JsonError("raw E621 JSON contains no usable tags")
    return tuple(values)


def parse_raw_e621_annotation(raw: bytes | None) -> RawE621Annotation | None:
    """Return a strictly parsed raw E621 annotation, or None for another format."""
    document = _parse(raw)
    if not _is_candidate(document):
        return None
    if not isinstance(document, dict):
        raise RawE621JsonError("raw E621 JSON must be an object")
    actual = set(document)
    expected = set(RAW_E621_GROUPS)
    if actual != expected:
        unexpected = sorted(actual - expected)
        missing = sorted(expected - actual)
        detail = f"unexpected groups: {', '.join(unexpected)}" if unexpected else f"missing groups: {', '.join(missing)}"
        raise RawE621JsonError(f"raw E621 JSON has {detail}")
    groups = {group: _tags(document[group], group) for group in RAW_E621_GROUPS}
    return RawE621Annotation(
        artist=_joined(groups["artist"]),
        character=_joined(groups["character"]),
        classify_tags=_classify_tags(groups),
    )
