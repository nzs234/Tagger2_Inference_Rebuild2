"""Classification snapshot resource: official e621/Danbooru tag data.

The Classify stage needs three official tables: the tag list with categories,
the alias table and the implication table. The resource catalog is
content-addressed per single file, so those three tables are bundled into one
strictly validated JSON document, ``classify-snapshot-v1``::

    {
      "format": "classify-snapshot-v1",
      "profile": "e621",
      "source": {"url": ..., "timestamp": ..., "note": ...},
      "tags": [{"name": "solo", "category": "general", "post_count": 1234}, ...],
      "aliases": [{"antecedent_name": "1girl", "consequent_name": "solo"}, ...],
      "implications": [{"antecedent_name": ..., "consequent_name": ...}, ...]
    }

``build_snapshot_from_official_csv`` converts the published e621/Danbooru DB
exports into that bundle. The exports encode ``category`` as an integer, so the
mapping is declared per profile rather than guessed, and an unknown category is
an error instead of being folded into ``general``.

Nothing here repairs input. A malformed row reports its own line number and the
whole resource is rejected, matching the replacement-index reader.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .stages.classify import ClassificationRules, ClassifyError, build_classification_rules

CLASSIFY_SNAPSHOT_FORMAT = "classify-snapshot-v1"
MAX_REPORTED_ERRORS = 100

# The published DB exports store ``category`` as an integer. Declared per
# profile so a Danbooru export can never be read with e621 semantics.
E621_CATEGORIES = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "species",
    6: "invalid",
    7: "meta",
    8: "lore",
}
DANBOORU_CATEGORIES = {
    0: "general",
    1: "artist",
    3: "copyright",
    4: "character",
    5: "meta",
}
PROFILE_CATEGORIES: dict[str, dict[int, str]] = {
    "e621": E621_CATEGORIES,
    "danbooru": DANBOORU_CATEGORIES,
}


class ClassifySnapshotError(ValueError):
    """Raised when a classification snapshot cannot be read as a valid resource."""


@dataclass(frozen=True)
class ClassifySnapshotReport:
    """Outcome of validating a classification snapshot."""

    valid: bool
    profile: str
    tag_count: int
    alias_count: int
    implication_count: int
    category_counts: dict[str, int]
    errors: list[str]
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "profile": self.profile,
            "tag_count": self.tag_count,
            "alias_count": self.alias_count,
            "implication_count": self.implication_count,
            "category_counts": dict(self.category_counts),
            "errors": list(self.errors),
            "truncated": self.truncated,
        }


def _tag_name_error(name: str) -> str | None:
    """Validate a tag name without repairing it.

    The same rules as the replacement index: a name must be non-empty, must not
    be padded around real content and must not carry a character that would
    break CSV or the flat TXT format.
    """

    if not name:
        return "tag name is blank"
    if "\x00" in name:
        return "tag name contains NUL"
    if any(character in name for character in ",\r\n"):
        return "tag name contains a CSV separator or newline"
    stripped = name.strip()
    if stripped and stripped != name:
        return f"tag name is padded with whitespace: {name!r}"
    return None


def _read_snapshot_document(path: Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise ClassifySnapshotError(f"cannot read classification snapshot: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        document = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ClassifySnapshotError("classification snapshot is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ClassifySnapshotError(f"classification snapshot is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ClassifySnapshotError("classification snapshot must be a JSON object")
    if document.get("format") != CLASSIFY_SNAPSHOT_FORMAT:
        raise ClassifySnapshotError(
            f"classification snapshot format must be {CLASSIFY_SNAPSHOT_FORMAT!r},"
            f" found {document.get('format')!r}"
        )
    profile = document.get("profile")
    if profile not in PROFILE_CATEGORIES:
        raise ClassifySnapshotError(f"unsupported classification profile: {profile!r}")
    for key in ("tags", "aliases"):
        if not isinstance(document.get(key), list):
            raise ClassifySnapshotError(f"classification snapshot {key!r} must be an array")
    implications = document.get("implications", [])
    if not isinstance(implications, list):
        raise ClassifySnapshotError("classification snapshot 'implications' must be an array")
    return document


def validate_classify_snapshot(path: Path) -> ClassifySnapshotReport:
    """Validate a classification snapshot and report every offending row.

    Errors are collected rather than raised so a preview can list the concrete
    problems, mirroring ``validate_replacement_index``.
    """

    errors: list[str] = []
    category_counts: dict[str, int] = {}
    truncated = False
    profile = ""
    tag_count = 0
    alias_count = 0
    implication_count = 0

    try:
        document = _read_snapshot_document(Path(path))
    except ClassifySnapshotError as exc:
        return ClassifySnapshotReport(
            valid=False,
            profile="",
            tag_count=0,
            alias_count=0,
            implication_count=0,
            category_counts={},
            errors=[str(exc)],
        )

    profile = str(document["profile"])
    allowed_categories = set(PROFILE_CATEGORIES[profile].values())

    seen_tags: set[str] = set()
    for index, row in enumerate(document["tags"]):
        if len(errors) >= MAX_REPORTED_ERRORS:
            truncated = True
            break
        if not isinstance(row, dict):
            errors.append(f"tags[{index}]: row must be an object")
            continue
        name = row.get("name")
        if not isinstance(name, str):
            errors.append(f"tags[{index}]: name must be a string")
            continue
        problem = _tag_name_error(name)
        if problem:
            errors.append(f"tags[{index}]: {problem}")
            continue
        if name in seen_tags:
            errors.append(f"tags[{index}]: duplicate tag name {name!r}")
            continue
        category = row.get("category")
        if not isinstance(category, str) or category not in allowed_categories:
            errors.append(
                f"tags[{index}]: category must be one of"
                f" {sorted(allowed_categories)}, found {category!r}"
            )
            continue
        post_count = row.get("post_count", 0)
        if not isinstance(post_count, int) or isinstance(post_count, bool) or post_count < 0:
            errors.append(f"tags[{index}]: post_count must be a non-negative integer")
            continue
        seen_tags.add(name)
        tag_count += 1
        category_counts[category] = category_counts.get(category, 0) + 1

    seen_aliases: set[str] = set()
    for index, row in enumerate(document["aliases"]):
        if len(errors) >= MAX_REPORTED_ERRORS:
            truncated = True
            break
        if not isinstance(row, dict):
            errors.append(f"aliases[{index}]: row must be an object")
            continue
        source = row.get("antecedent_name")
        target = row.get("consequent_name")
        if not isinstance(source, str) or not isinstance(target, str):
            errors.append(f"aliases[{index}]: antecedent_name and consequent_name must be strings")
            continue
        problem = _tag_name_error(source) or _tag_name_error(target)
        if problem:
            errors.append(f"aliases[{index}]: {problem}")
            continue
        if source == target:
            errors.append(f"aliases[{index}]: alias points at itself: {source!r}")
            continue
        if source in seen_aliases:
            errors.append(f"aliases[{index}]: duplicate antecedent_name {source!r}")
            continue
        seen_aliases.add(source)
        alias_count += 1

    for index, row in enumerate(document.get("implications", [])):
        if len(errors) >= MAX_REPORTED_ERRORS:
            truncated = True
            break
        if not isinstance(row, dict):
            errors.append(f"implications[{index}]: row must be an object")
            continue
        source = row.get("antecedent_name")
        target = row.get("consequent_name")
        if not isinstance(source, str) or not isinstance(target, str):
            errors.append(
                f"implications[{index}]: antecedent_name and consequent_name must be strings"
            )
            continue
        problem = _tag_name_error(source) or _tag_name_error(target)
        if problem:
            errors.append(f"implications[{index}]: {problem}")
            continue
        implication_count += 1

    # An alias cycle only surfaces once the chains are flattened, so run the
    # real builder rather than declaring the snapshot valid on row checks alone.
    if not errors:
        try:
            build_classification_rules(
                profile,
                list(document["tags"]),
                list(document["aliases"]),
                list(document.get("implications", [])),
            )
        except ClassifyError as exc:
            errors.append(str(exc))

    return ClassifySnapshotReport(
        valid=not errors,
        profile=profile,
        tag_count=tag_count,
        alias_count=alias_count,
        implication_count=implication_count,
        category_counts=category_counts,
        errors=errors,
        truncated=truncated,
    )


def load_classification_rules(path: Path) -> ClassificationRules:
    """Load a validated snapshot into executable classification rules."""

    document = _read_snapshot_document(Path(path))
    profile = str(document["profile"])
    allowed_categories = set(PROFILE_CATEGORIES[profile].values())

    tags: list[dict[str, Any]] = []
    for index, row in enumerate(document["tags"]):
        if not isinstance(row, dict):
            raise ClassifySnapshotError(f"tags[{index}]: row must be an object")
        name = row.get("name")
        if not isinstance(name, str):
            raise ClassifySnapshotError(f"tags[{index}]: name must be a string")
        problem = _tag_name_error(name)
        if problem:
            raise ClassifySnapshotError(f"tags[{index}]: {problem}")
        category = row.get("category")
        if not isinstance(category, str) or category not in allowed_categories:
            raise ClassifySnapshotError(
                f"tags[{index}]: category must be one of"
                f" {sorted(allowed_categories)}, found {category!r}"
            )
        tags.append(row)

    aliases: list[dict[str, Any]] = []
    for index, row in enumerate(document["aliases"]):
        if not isinstance(row, dict):
            raise ClassifySnapshotError(f"aliases[{index}]: row must be an object")
        aliases.append(row)

    implications = [row for row in document.get("implications", []) if isinstance(row, dict)]

    try:
        return build_classification_rules(profile, tags, aliases, implications)
    except ClassifyError as exc:
        raise ClassifySnapshotError(str(exc)) from exc


def _iter_csv_rows(path: Path, required: tuple[str, ...]) -> Iterator[tuple[int, dict[str, str]]]:
    try:
        stream = path.open("r", encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise ClassifySnapshotError(f"cannot read {path.name}: {exc}") from exc
    with stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ClassifySnapshotError(f"{path.name} is empty")
        missing = [column for column in required if column not in reader.fieldnames]
        if missing:
            raise ClassifySnapshotError(
                f"{path.name} is missing required column(s): {', '.join(missing)}"
            )
        yield from enumerate(reader, start=2)


def build_snapshot_from_official_csv(
    *,
    profile: str,
    tags_csv: Path,
    aliases_csv: Path,
    implications_csv: Path | None = None,
    source_url: str | None = None,
    source_timestamp: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Convert published e621/Danbooru DB exports into a snapshot bundle.

    ``category`` in those exports is an integer, mapped through the profile's
    declared table. An unknown category is an error rather than being folded
    into ``general``, and an alias row that is not ``active`` is skipped because
    only active aliases are applied by the site itself.
    """

    if profile not in PROFILE_CATEGORIES:
        raise ClassifySnapshotError(f"unsupported classification profile: {profile!r}")
    categories = PROFILE_CATEGORIES[profile]

    tags: list[dict[str, Any]] = []
    seen_tags: set[str] = set()
    for line_number, row in _iter_csv_rows(Path(tags_csv), ("name", "category")):
        name = row["name"] or ""
        problem = _tag_name_error(name)
        if problem:
            raise ClassifySnapshotError(f"{Path(tags_csv).name} line {line_number}: {problem}")
        if name in seen_tags:
            raise ClassifySnapshotError(
                f"{Path(tags_csv).name} line {line_number}: duplicate tag name {name!r}"
            )
        raw_category = (row.get("category") or "").strip()
        try:
            category_name = categories[int(raw_category)]
        except (KeyError, ValueError) as exc:
            raise ClassifySnapshotError(
                f"{Path(tags_csv).name} line {line_number}: unknown {profile} category"
                f" {raw_category!r}"
            ) from exc
        raw_post_count = (row.get("post_count") or "0").strip() or "0"
        try:
            post_count = int(raw_post_count)
        except ValueError as exc:
            raise ClassifySnapshotError(
                f"{Path(tags_csv).name} line {line_number}: post_count is not an integer:"
                f" {raw_post_count!r}"
            ) from exc
        if post_count < 0:
            raise ClassifySnapshotError(
                f"{Path(tags_csv).name} line {line_number}: post_count is negative"
            )
        seen_tags.add(name)
        tags.append({"name": name, "category": category_name, "post_count": post_count})

    aliases: list[dict[str, str]] = []
    seen_aliases: set[str] = set()
    for line_number, row in _iter_csv_rows(
        Path(aliases_csv), ("antecedent_name", "consequent_name")
    ):
        status = (row.get("status") or "active").strip().casefold()
        if status and status != "active":
            continue
        source = row["antecedent_name"] or ""
        target = row["consequent_name"] or ""
        problem = _tag_name_error(source) or _tag_name_error(target)
        if problem:
            raise ClassifySnapshotError(f"{Path(aliases_csv).name} line {line_number}: {problem}")
        if source == target:
            raise ClassifySnapshotError(
                f"{Path(aliases_csv).name} line {line_number}: alias points at itself:"
                f" {source!r}"
            )
        if source in seen_aliases:
            raise ClassifySnapshotError(
                f"{Path(aliases_csv).name} line {line_number}: duplicate antecedent_name"
                f" {source!r}"
            )
        seen_aliases.add(source)
        aliases.append({"antecedent_name": source, "consequent_name": target})

    implications: list[dict[str, str]] = []
    if implications_csv is not None:
        for line_number, row in _iter_csv_rows(
            Path(implications_csv), ("antecedent_name", "consequent_name")
        ):
            status = (row.get("status") or "active").strip().casefold()
            if status and status != "active":
                continue
            source = row["antecedent_name"] or ""
            target = row["consequent_name"] or ""
            problem = _tag_name_error(source) or _tag_name_error(target)
            if problem:
                raise ClassifySnapshotError(
                    f"{Path(implications_csv).name} line {line_number}: {problem}"
                )
            implications.append({"antecedent_name": source, "consequent_name": target})

    source_metadata: dict[str, Any] = {}
    if source_url:
        source_metadata["url"] = source_url
    if source_timestamp:
        source_metadata["timestamp"] = source_timestamp
    if note:
        source_metadata["note"] = note

    return {
        "format": CLASSIFY_SNAPSHOT_FORMAT,
        "profile": profile,
        "source": source_metadata,
        "tags": tags,
        "aliases": aliases,
        "implications": implications,
    }


__all__ = [
    "CLASSIFY_SNAPSHOT_FORMAT",
    "DANBOORU_CATEGORIES",
    "E621_CATEGORIES",
    "PROFILE_CATEGORIES",
    "ClassifySnapshotError",
    "ClassifySnapshotReport",
    "build_snapshot_from_official_csv",
    "load_classification_rules",
    "validate_classify_snapshot",
]
