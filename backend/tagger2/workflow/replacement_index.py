"""e621 replacement index resource: strict CSV validation and rule loading.

The on-disk contract matches the source project's ``e621-replacement-csv-v1``
format so imported indexes keep their exact keep/replace/drop semantics::

    source_tag,canonical_e621_tag,action,replacement_tags

``replacement_tags`` holds a single tag for ``keep``, a ``|`` separated list for
``replace`` and must be empty for ``drop``.

The designated e621 index additionally uses a fourth action, ``pass``, for
identity passthrough (``replacement_tags`` equals ``source_tag``). Those rows are
validated strictly and then intentionally omitted from the executable rule table:
a tag with no rule already passes through unchanged and is counted as
passthrough by the ported transform, so behaviour is identical while the rule
table stays ~104k entries smaller. A ``pass`` row whose replacement differs from
its source is a hard error rather than a silent rewrite.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .stages.replacement import ReplacementError, ReplacementRule, rule_from_csv

REPLACEMENT_CSV_HEADER = ("source_tag", "canonical_e621_tag", "action", "replacement_tags")
REPLACEMENT_RUNTIME_FORMAT = "e621-replacement-csv-v1"
PASSTHROUGH_ACTION = "pass"
MAX_REPORTED_ERRORS = 100


class ReplacementIndexError(ValueError):
    """Raised when a replacement index cannot be read as a valid resource."""


@dataclass(frozen=True)
class ReplacementIndexReport:
    """Outcome of validating a replacement index CSV."""

    valid: bool
    rule_count: int
    action_counts: dict[str, int]
    pipe_replacement_count: int
    errors: list[str]
    truncated: bool = False
    passthrough_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "rule_count": self.rule_count,
            "action_counts": dict(self.action_counts),
            "pipe_replacement_count": self.pipe_replacement_count,
            "errors": list(self.errors),
            "truncated": self.truncated,
            "passthrough_count": self.passthrough_count,
        }


def _iter_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    # ``utf-8-sig`` mirrors the source reader so an exported BOM is not a failure.
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            raise ReplacementIndexError("replacement index is empty") from None
        if tuple(field.strip() for field in header) != REPLACEMENT_CSV_HEADER:
            raise ReplacementIndexError(
                "replacement index header must be "
                + ",".join(REPLACEMENT_CSV_HEADER)
            )
        for line_number, row in enumerate(reader, start=2):
            if len(row) != len(REPLACEMENT_CSV_HEADER):
                raise ReplacementIndexError(
                    f"line {line_number}: expected {len(REPLACEMENT_CSV_HEADER)} columns,"
                    f" found {len(row)}"
                )
            yield line_number, dict(zip(REPLACEMENT_CSV_HEADER, row))




def _source_tag_error(source_tag: str) -> str | None:
    """Validate a source tag.

    A tag made up entirely of whitespace is legitimate index content: the
    designated e621 index carries junk tags such as U+3000 with a ``drop`` rule.
    Only an empty tag, a tag padded around real content, or a tag carrying a
    CSV/flat-TXT breaking character is rejected.
    """

    if not source_tag:
        return "source_tag is blank"
    if "\x00" in source_tag:
        return "source_tag contains NUL"
    if any(character in source_tag for character in ",\r\n"):
        return "source_tag contains a CSV separator or newline"
    stripped = source_tag.strip()
    if stripped and stripped != source_tag:
        return f"source_tag is padded with whitespace: {source_tag!r}"
    return None


def _passthrough_error(source_tag: str, replacement_tags: str) -> str | None:
    """Validate an identity-passthrough row without repairing it."""

    if replacement_tags != source_tag:
        return (
            f"{PASSTHROUGH_ACTION!r} action must repeat source_tag"
            f" {source_tag!r}, found {replacement_tags!r}"
        )
    return None


def validate_replacement_index(path: Path) -> ReplacementIndexReport:
    """Validate a replacement index without loading it into memory as rules.

    Reports the concrete offending line for every rejected row and never
    repairs the input.
    """

    errors: list[str] = []
    action_counts: dict[str, int] = {"keep": 0, "replace": 0, "drop": 0, PASSTHROUGH_ACTION: 0}
    seen: set[str] = set()
    rule_count = 0
    pipe_replacement_count = 0
    passthrough_count = 0
    truncated = False

    try:
        for line_number, row in _iter_rows(path):
            if len(errors) >= MAX_REPORTED_ERRORS:
                truncated = True
                break

            source_tag = row["source_tag"]
            action = row["action"].strip()
            replacement_tags = row["replacement_tags"]

            problem = _source_tag_error(source_tag)
            if problem:
                errors.append(f"line {line_number}: {problem}")
                continue

            key = source_tag.casefold()
            if key in seen:
                errors.append(f"line {line_number}: duplicate source_tag {source_tag!r}")
                continue
            seen.add(key)

            if action == PASSTHROUGH_ACTION:
                problem = _passthrough_error(source_tag, replacement_tags)
                if problem:
                    errors.append(f"line {line_number}: {problem}")
                    continue
                action_counts[PASSTHROUGH_ACTION] += 1
                passthrough_count += 1
                continue

            try:
                rule = rule_from_csv(action, replacement_tags)
            except ReplacementError as exc:
                errors.append(f"line {line_number}: {exc}")
                continue

            rule_count += 1
            action_counts[rule.action] = action_counts.get(rule.action, 0) + 1
            if rule.action == "replace" and len(rule.replacement_tags) > 1:
                pipe_replacement_count += 1
    except ReplacementIndexError as exc:
        errors.append(str(exc))
    except UnicodeDecodeError:
        errors.append("replacement index is not valid UTF-8")
    except OSError as exc:
        errors.append(f"cannot read replacement index: {exc}")

    return ReplacementIndexReport(
        valid=not errors,
        rule_count=rule_count,
        action_counts=action_counts,
        pipe_replacement_count=pipe_replacement_count,
        errors=errors,
        truncated=truncated,
        passthrough_count=passthrough_count,
    )


def load_replacement_rules(path: Path) -> dict[str, ReplacementRule]:
    """Load a validated replacement index into an executable rule table."""

    rules: dict[str, ReplacementRule] = {}
    for line_number, row in _iter_rows(path):
        source_tag = row["source_tag"]
        problem = _source_tag_error(source_tag)
        if problem:
            raise ReplacementIndexError(f"line {line_number}: {problem}")
        if source_tag in rules:
            raise ReplacementIndexError(
                f"line {line_number}: duplicate source_tag {source_tag!r}"
            )
        action = row["action"].strip()
        if action == PASSTHROUGH_ACTION:
            problem = _passthrough_error(source_tag, row["replacement_tags"])
            if problem:
                raise ReplacementIndexError(f"line {line_number}: {problem}")
            # Identity rows are omitted: no rule means unchanged passthrough.
            continue
        try:
            rules[source_tag] = rule_from_csv(action, row["replacement_tags"])
        except ReplacementError as exc:
            raise ReplacementIndexError(f"line {line_number}: {exc}") from exc
    if not rules:
        raise ReplacementIndexError("replacement index contains no rules")
    return rules


__all__ = [
    "PASSTHROUGH_ACTION",
    "REPLACEMENT_CSV_HEADER",
    "REPLACEMENT_RUNTIME_FORMAT",
    "ReplacementIndexError",
    "ReplacementIndexReport",
    "validate_replacement_index",
    "load_replacement_rules",
]
