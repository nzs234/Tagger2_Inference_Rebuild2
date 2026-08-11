"""Formatting rules applied only when local-model tags leave the inference engine."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .schemas import TagItem


# Local taggers often score generic visual/action tags highest. Keep identity
# tags useful to downstream dataset tooling by placing them before general tags.
_CATEGORY_ORDER = {
    "character": 0,
    "species": 1,
    "copyright": 2,
    "artist": 3,
    "meta": 4,
    "rating": 5,
    "general": 99,
}


def _local_tag_sort_key(value: Mapping[str, Any]) -> tuple[int, float, str]:
    category = str(value.get("category") or "general").strip().casefold()
    score = value.get("score")
    numeric_score = float(score) if isinstance(score, (int, float)) else -1.0
    return (_CATEGORY_ORDER.get(category, 50), -numeric_score, str(value.get("text") or "").casefold())


def escape_tag_parentheses(value: str) -> str:
    """Normalize each parenthesis to exactly one preceding backslash."""

    result: list[str] = []
    for character in value:
        if character in "()":
            slash_count = 0
            for previous in reversed(result):
                if previous != "\\":
                    break
                slash_count += 1
            if slash_count == 0:
                result.append("\\")
            elif slash_count > 1:
                del result[-(slash_count - 1):]
        result.append(character)
    return "".join(result)


def format_local_tags(
    tags: Iterable[TagItem],
    output: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Filter and format local tags without mutating model predictions."""

    include_rating = bool(output.get("include_rating", False))
    replace_underscores = bool(output.get("replace_underscores", False))
    escape_parentheses = bool(output.get("escape_parentheses", True))
    values: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    for tag in tags:
        if not include_rating and tag.category.strip().casefold() == "rating":
            continue
        value = tag.model_dump(mode="json")
        text = str(value["text"])
        if replace_underscores:
            text = text.replace("_", " ")
        if escape_parentheses:
            text = escape_tag_parentheses(text)
        value["text"] = text
        key = " ".join(text.split()).casefold()
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(values)
            values.append(value)
            continue
        current_score = values[existing_index].get("score")
        candidate_score = value.get("score")
        if isinstance(candidate_score, (int, float)) and (
            not isinstance(current_score, (int, float)) or candidate_score > current_score
        ):
            values[existing_index] = value
    values.sort(key=_local_tag_sort_key)
    return values


__all__ = ["escape_tag_parentheses", "format_local_tags"]
