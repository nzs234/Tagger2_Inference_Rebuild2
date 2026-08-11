# Ported verbatim from the e621-standard-caption-workflow project
# (workers/replace/src/anima_replace_worker/replacement.py).
# The keep/replace/drop semantics and cross-field dedup priority are frozen.
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


VALID_ACTIONS = frozenset({"keep", "replace", "drop"})
ARRAY_FIELDS = ("quality", "appearance", "tags", "environment")
# Sample level dedup order is the frozen flat TXT field order (ROADMAP.md:1004); the first
# field that emits a tag keeps it so Export never sees a cross_field_tag_collision.
PRIORITY_FIELDS = ("quality", "character", "appearance", "tags", "environment")


class ReplacementError(ValueError):
    pass


@dataclass(frozen=True)
class ReplacementRule:
    action: str
    replacement_tags: tuple[str, ...]


@dataclass(frozen=True)
class ReplacementSummary:
    replaced: int
    dropped: int
    passthrough: int
    keep_rewritten: int = 0

    def merge(self, other: "ReplacementSummary") -> "ReplacementSummary":
        return ReplacementSummary(
            self.replaced + other.replaced, self.dropped + other.dropped,
            self.passthrough + other.passthrough, self.keep_rewritten + other.keep_rewritten,
        )


def _output_tags(action: str, replacement_tags: str) -> tuple[str, ...]:
    if action not in VALID_ACTIONS:
        raise ReplacementError("replacement action is invalid")
    if action == "drop":
        if replacement_tags:
            raise ReplacementError("drop rules must not have replacement tags")
        return ()
    if not replacement_tags:
        raise ReplacementError("keep and replace rules require replacement tags")
    if action == "keep":
        return (replacement_tags,)
    tags = tuple(tag.strip() for tag in replacement_tags.split("|"))
    if not tags or any(not tag or any(character in tag for character in ",\r\n\x00") for tag in tags):
        raise ReplacementError("replace rule contains an invalid replacement tag")
    return tags


def rule_from_csv(action: object, replacement_tags: object) -> ReplacementRule:
    if not isinstance(action, str) or not isinstance(replacement_tags, str):
        raise ReplacementError("replacement CSV values must be strings")
    return ReplacementRule(action, _output_tags(action, replacement_tags))


def _transform_tags(tags: list[str], rules: Mapping[str, ReplacementRule], seen: set[str]) -> tuple[list[str], ReplacementSummary]:
    output: list[str] = []
    replaced = dropped = passthrough = keep_rewritten = 0
    for tag in tags:
        rule = rules.get(tag)
        if rule is None:
            candidates = (tag,)
            passthrough += 1
        elif rule.action == "drop":
            candidates = ()
            dropped += 1
        else:
            candidates = rule.replacement_tags
            if rule.action == "replace":
                replaced += 1
            elif candidates != (tag,):
                keep_rewritten += 1
        for candidate in candidates:
            key = candidate.casefold()
            if key not in seen:
                output.append(candidate)
                seen.add(key)
    return output, ReplacementSummary(replaced, dropped, passthrough, keep_rewritten)


def _field_tags(value: Mapping[str, object], field: str) -> list[str]:
    if field == "character":
        character = value["character"]
        if not isinstance(character, str):
            raise ReplacementError("character must be a string")
        return [tag.strip() for tag in character.split(",") if tag.strip()]
    tags = value[field]
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ReplacementError(f"{field} must be a string array")
    return tags


def replace_projection(value: Mapping[str, object], rules: Mapping[str, ReplacementRule]) -> tuple[dict[str, object], ReplacementSummary]:
    required = {"quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl"}
    if set(value) != required:
        raise ReplacementError("replacement projection must contain exactly the nine fields")
    result = dict(value)
    seen: set[str] = set()
    totals = ReplacementSummary(0, 0, 0, 0)
    for field in PRIORITY_FIELDS:
        transformed, summary = _transform_tags(_field_tags(value, field), rules, seen)
        result[field] = ", ".join(transformed) if field == "character" else transformed
        totals = totals.merge(summary)
    return result, totals
