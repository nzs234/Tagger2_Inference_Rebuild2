# Ported verbatim from the e621-standard-caption-workflow project
# (workers/classify/src/anima_classify_worker/count.py). The count ranking,
# original-count normalization, sheet-layout conflict and lower-bound rules
# are kept byte-identical. WikiCountResolver takes any sqlite3 connection
# exposing a wiki_catalog(title, body) table; when a tag is absent it emits a
# wiki_missing warning, which is how an unavailable wiki snapshot degrades.
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable


COUNT_RANK = {"": 0, "solo": 1, "duo": 2, "trio": 3, "group": 4}
RANK_COUNT = {value: key for key, value in COUNT_RANK.items()}
E621_COUNT_RULES: dict[str, tuple[str, int]] = {
    "solo": ("solo", 100),
    "duo": ("duo", 100),
    "trio": ("trio", 100),
    "group": ("group", 90),
    "large_group": ("group", 80),
    "crowd": ("group", 70),
}


def _danbooru_family(singular: str, plural: str) -> dict[str, dict[str, int | None]]:
    return {
        f"1{singular}": {"min": 1, "max": 1},
        **{f"{value}{plural}": {"min": value, "max": value} for value in range(2, 6)},
        f"6+{plural}": {"min": 6, "max": None},
    }


EXPECTED_DANBOORU_COUNT_RULES: dict[str, object] = {
    "schemaVersion": 1,
    "profile": "danbooru",
    "families": {
        "girl": _danbooru_family("girl", "girls"),
        "boy": _danbooru_family("boy", "boys"),
        "other": _danbooru_family("other", "others"),
    },
    "lowerBounds": {
        "multiple_girls": {"family": "girl", "min": 2},
        "multiple_boys": {"family": "boy", "min": 2},
        "multiple_others": {"family": "other", "min": 2},
    },
    "fallbacks": {"solo": "solo"},
    "nonDecisive": ["solo_focus"],
}
RELATION_PAIR_TAGS = frozenset({
    "anthro_on_anthro", "anthro_on_feral", "feral_on_feral", "human_on_anthro", "human_on_feral",
    "human_on_human", "human_on_humanoid", "humanoid_on_anthro", "humanoid_on_feral", "humanoid_on_humanoid",
})
SHEET_LAYOUT_TAGS = frozenset({
    "model_sheet", "character_sheet", "reference_sheet", "multiple_angles", "multiple_views", "multiple_poses",
    "expression_sheet", "turnaround",
})
ORIGINAL_COUNT_ALIASES = {
    "1": "solo", "one": "solo", "single": "solo", "alone": "solo", "solo": "solo",
    "2": "duo", "two": "duo", "pair": "duo", "couple": "duo", "duo": "duo",
    "3": "trio", "three": "trio", "triple": "trio", "trio": "trio",
    "4+": "group", "large group": "group", "crowd": "group", "group": "group", "male solo": "solo",
    "female solo": "solo",
}
ORIGINAL_COUNT_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
ORIGINAL_COUNT_SUBJECT_RE = re.compile(
    r"(?P<count>[1-9]\d*\+?|one|two|three|four|five|six|seven|eight|nine|ten)\s*"
    r"(?:girls?|boys?|males?|females?|men|man|women|woman|lady|ladies"
    r"|others?|people|persons?|characters?|animals?)"
)


class WikiCountError(RuntimeError):
    pass


class DanbooruCountRulesError(ValueError):
    pass


@dataclass(frozen=True)
class CountResolution:
    value: str | None
    matched_tags: tuple[str, ...]
    warnings: tuple[str, ...]
    applied_lower_bounds: tuple[str, ...] = ()


@dataclass(frozen=True)
class DanbooruCountRules:
    exact: dict[str, tuple[str, int, int | None]]
    lower_bounds: dict[str, tuple[str, int]]
    fallbacks: frozenset[str]
    non_decisive: frozenset[str]

    @classmethod
    def from_payload(cls, value: object) -> "DanbooruCountRules":
        try:
            actual = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = json.dumps(
                EXPECTED_DANBOORU_COUNT_RULES,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise DanbooruCountRulesError("Danbooru count rules are not JSON-compatible") from exc
        if actual != expected:
            raise DanbooruCountRulesError("Danbooru count rules do not match the frozen v1 rule set")
        exact: dict[str, tuple[str, int, int | None]] = {}
        families = EXPECTED_DANBOORU_COUNT_RULES["families"]
        assert isinstance(families, dict)
        for family, raw_rules in families.items():
            assert isinstance(raw_rules, dict)
            for tag, bounds in raw_rules.items():
                assert isinstance(bounds, dict)
                exact[tag] = (family, int(bounds["min"]), bounds["max"])
        raw_lower = EXPECTED_DANBOORU_COUNT_RULES["lowerBounds"]
        assert isinstance(raw_lower, dict)
        lower = {
            tag: (str(bounds["family"]), int(bounds["min"]))
            for tag, bounds in raw_lower.items()
            if isinstance(bounds, dict)
        }
        return cls(exact, lower, frozenset({"solo"}), frozenset({"solo_focus"}))

    @property
    def wiki_titles(self) -> tuple[str, ...]:
        return tuple(sorted({*self.exact, *self.lower_bounds, *self.fallbacks, *self.non_decisive}))


@dataclass(frozen=True)
class CountDecision:
    value: str
    base_value: str
    selected_source: str
    original_raw: str | int | None
    original_normalized: str | None
    wiki_value: str | None
    matched_tags: tuple[str, ...]
    conflict: bool
    issue_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    applied_lower_bounds: tuple[str, ...]
    blocking_code: str | None = None


def normalize_original_count(value: object) -> str | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 1:
            return None
        return ("solo", "duo", "trio")[value - 1] if value <= 3 else "group"
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", " ", value.strip().casefold().replace("_", " ").replace("-", " "))
    if not normalized:
        return None
    if direct := ORIGINAL_COUNT_ALIASES.get(normalized):
        return direct
    parts = [part.strip() for part in re.split(r"\s*(?:,|&|\band\b)\s*", normalized) if part.strip()]
    if not parts:
        return None
    total = 0
    lower_bound = False
    explicit_values: set[str] = set()
    for part in parts:
        if explicit := ORIGINAL_COUNT_ALIASES.get(part):
            explicit_values.add(explicit)
            continue
        match = ORIGINAL_COUNT_SUBJECT_RE.fullmatch(part)
        if not match:
            return None
        raw_count = match.group("count")
        has_plus = raw_count.endswith("+")
        number_token = raw_count.removesuffix("+")
        number = int(number_token) if number_token.isdigit() else ORIGINAL_COUNT_WORDS[number_token]
        if has_plus and number < 4:
            return None
        total += number
        lower_bound = lower_bound or has_plus
    if len(explicit_values) > 1:
        return None
    numeric = ("solo", "duo", "trio")[total - 1] if 1 <= total <= 3 else "group" if total else None
    if lower_bound:
        numeric = "group"
    explicit = next(iter(explicit_values), None)
    return None if explicit and numeric and explicit != numeric else explicit or numeric


class WikiCountResolver:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.cache: dict[str, str | None] = {}

    def _bodies(self, tags: Iterable[str]) -> dict[str, str | None]:
        requested = tuple(dict.fromkeys(tag for tag in tags if tag))
        missing = [tag for tag in requested if tag not in self.cache]
        if missing:
            placeholders = ",".join("?" for _ in missing)
            try:
                rows = self.connection.execute(
                    f"SELECT title, body FROM wiki_catalog WHERE title IN ({placeholders})", missing
                ).fetchall()
            except sqlite3.Error as exc:
                raise WikiCountError("Wiki projection query failed") from exc
            found = {str(title): str(body) if body else None for title, body in rows}
            self.cache.update({tag: found.get(tag) for tag in missing})
        return {tag: self.cache[tag] for tag in requested}

    def resolve_e621(self, tags: Iterable[str]) -> CountResolution:
        normalized = tuple(dict.fromkeys(tag for tag in tags if tag))
        candidates = [tag for tag in normalized if tag in E621_COUNT_RULES]
        bodies = self._bodies(candidates)
        values: list[tuple[int, int, str, str]] = []
        warnings: list[str] = []
        for position, tag in enumerate(normalized):
            rule = E621_COUNT_RULES.get(tag)
            if rule is None:
                continue
            if not bodies.get(tag):
                warnings.append(f"wiki_missing:e621:{tag}")
                continue
            count, priority = rule
            values.append((priority, position, count, tag))
        if not values:
            return CountResolution(None, (), tuple(warnings))
        values.sort(key=lambda item: (-item[0], item[1]))
        _, _, selected, matched = values[0]
        incompatible = {value for _, _, value, _ in values if value != selected and not {value, selected} <= {"trio", "group"}}
        if incompatible:
            warnings.append("count_conflict:e621:" + ",".join(tag for _, _, _, tag in values))
        return CountResolution(selected, (matched,), tuple(warnings))

    def verified_relationship_tags(self, tags: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        candidates = tuple(tag for tag in dict.fromkeys(tags) if tag in RELATION_PAIR_TAGS)
        bodies = self._bodies(candidates)
        matched = tuple(tag for tag in candidates if bodies.get(tag))
        warnings = tuple(f"wiki_missing:e621:{tag}" for tag in candidates if not bodies.get(tag))
        return matched, warnings

    def resolve_danbooru(
        self,
        tags: Iterable[str],
        rules: DanbooruCountRules,
    ) -> CountResolution:
        normalized = tuple(dict.fromkeys(tag for tag in tags if tag))
        candidates = tuple(
            tag
            for tag in normalized
            if tag in rules.exact or tag in rules.lower_bounds or tag in rules.fallbacks or tag in rules.non_decisive
        )
        bodies = self._bodies(candidates)
        matched = tuple(tag for tag in candidates if bodies.get(tag))
        warnings = [f"wiki_missing:danbooru:{tag}" for tag in candidates if not bodies.get(tag)]

        exact_by_family: dict[str, list[str]] = {"girl": [], "boy": [], "other": []}
        for tag in matched:
            if exact := rules.exact.get(tag):
                exact_by_family[exact[0]].append(tag)
        exact_values: dict[str, tuple[str, int, int | None]] = {}
        unresolved = False
        for family in ("girl", "boy", "other"):
            family_tags = exact_by_family[family]
            if len(family_tags) > 1:
                warnings.append(f"count_conflict:danbooru:{family}:" + ",".join(family_tags))
                unresolved = True
            elif family_tags:
                tag = family_tags[0]
                _, minimum, maximum = rules.exact[tag]
                exact_values[family] = (tag, minimum, maximum)

        applied_bounds: list[str] = []
        for tag in matched:
            lower = rules.lower_bounds.get(tag)
            if lower is None:
                continue
            family, minimum = lower
            warnings.append(f"count_lower_bound:danbooru:{tag}")
            bound_name = f"danbooru_{family}"
            if bound_name not in applied_bounds:
                applied_bounds.append(bound_name)
            exact = exact_values.get(family)
            if exact is None:
                unresolved = True
            elif exact[1] < minimum:
                warnings.append(f"count_conflict:danbooru:{family}:{exact[0]},{tag}")
                unresolved = True

        if any(tag in rules.non_decisive for tag in matched):
            focus_tags = [tag for tag in matched if tag in rules.non_decisive]
            warnings.extend(f"count_non_decisive:danbooru:{tag}" for tag in focus_tags)
            unresolved = True

        value: str | None = None
        if exact_values and not unresolved:
            if any(maximum is None for _, _, maximum in exact_values.values()):
                value = "group"
            else:
                total = sum(minimum for _, minimum, _ in exact_values.values())
                value = ("solo", "duo", "trio")[total - 1] if 1 <= total <= 3 else "group"

        has_solo = any(tag in rules.fallbacks for tag in matched)
        if has_solo and value is not None and value != "solo":
            warnings.append(
                "count_conflict:danbooru:solo:solo," + ",".join(item[0] for item in exact_values.values())
            )
            value = None
        elif has_solo and value is None and not unresolved and not applied_bounds:
            value = "solo"
        return CountResolution(
            value,
            matched,
            tuple(dict.fromkeys(warnings)),
            tuple(applied_bounds),
        )


def _append_once(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def decide_count(
    original_raw: str | int | None,
    tags: Iterable[str],
    canonical_character_ids: Iterable[str],
    evidence_tags: Iterable[str],
    resolver: WikiCountResolver,
    overwrite_count: bool,
) -> CountDecision:
    normalized_tags = tuple(dict.fromkeys(tag for tag in tags if tag))
    resolution = resolver.resolve_e621(normalized_tags)
    original_normalized = normalize_original_count(original_raw)
    has_original = original_raw is not None and (not isinstance(original_raw, str) or bool(original_raw.strip()))
    issue_codes: list[str] = []
    warnings = list(resolution.warnings)
    if has_original and original_normalized is None:
        _append_once(issue_codes, "original_count_invalid")
    conflict = bool(original_normalized and resolution.value and original_normalized != resolution.value)
    if conflict:
        _append_once(issue_codes, "count_source_conflict")
        _append_once(warnings, "count_source_conflict")
    if overwrite_count and resolution.value:
        base_value, selected_source = resolution.value, "wiki_tags"
    elif original_normalized:
        base_value, selected_source = original_normalized, "original_json"
    elif resolution.value:
        base_value, selected_source = resolution.value, "wiki_tags"
    else:
        base_value, selected_source = "", "none"
    if has_original and original_normalized is None and resolution.value is None:
        _append_once(issue_codes, "original_count_invalid_unresolved")
        return CountDecision(
            value="", base_value="", selected_source="none", original_raw=original_raw,
            original_normalized=None, wiki_value=None, matched_tags=resolution.matched_tags, conflict=False,
            issue_codes=tuple(issue_codes), warnings=tuple(warnings), applied_lower_bounds=(),
            blocking_code="classify_original_count_unresolved",
        )

    evidence = frozenset(evidence_tags)
    character_ids = tuple(dict.fromkeys(canonical_character_ids))
    base_rank = COUNT_RANK[base_value]
    strong_layout = bool(evidence & SHEET_LAYOUT_TAGS)
    # Only a single canonical identity contradicts a duo/trio/group base. e621 posts that
    # express characters through species carry no character tag at all, so 0 identities is
    # normal and must not block the sample.
    if strong_layout and len(character_ids) == 1 and base_rank >= COUNT_RANK["duo"]:
        _append_once(issue_codes, "count_sheet_multi_conflict")
        return CountDecision(
            value=base_value, base_value=base_value, selected_source=selected_source, original_raw=original_raw,
            original_normalized=original_normalized, wiki_value=resolution.value, matched_tags=resolution.matched_tags,
            conflict=conflict, issue_codes=tuple(issue_codes), warnings=tuple(warnings), applied_lower_bounds=(),
            blocking_code="count_sheet_multi_conflict",
        )

    character_rank = min(len(character_ids), 4)
    matched_relationships, relation_warnings = resolver.verified_relationship_tags(normalized_tags)
    for warning in relation_warnings:
        _append_once(warnings, warning)
    # ROADMAP.md:537 强布局 + <=1 canonical character：同一角色的多姿势/多视角重复出现，
    # 关系 tag 不再建立 duo 下界，基础 count 为空/solo 时固定为 solo。
    relation_rank = COUNT_RANK["duo"] if matched_relationships and not (strong_layout and len(character_ids) <= 1) else 0
    final_rank = max(base_rank, character_rank, relation_rank)
    lower_bounds: list[str] = []
    if character_rank > base_rank:
        _append_once(lower_bounds, "character")
        _append_once(issue_codes, "count_character_lower_bound")
        _append_once(warnings, "count_character_lower_bound")
    if relation_rank > base_rank:
        _append_once(lower_bounds, "e621_relationship")
        _append_once(issue_codes, "count_relationship_lower_bound")
        _append_once(warnings, "count_relationship_lower_bound")
    return CountDecision(
        value=RANK_COUNT[final_rank], base_value=base_value, selected_source=selected_source, original_raw=original_raw,
        original_normalized=original_normalized, wiki_value=resolution.value,
        matched_tags=tuple(dict.fromkeys((*resolution.matched_tags, *matched_relationships))), conflict=conflict,
        issue_codes=tuple(issue_codes), warnings=tuple(warnings), applied_lower_bounds=tuple(lower_bounds),
    )


def decide_danbooru_count(
    original_raw: str | int | None,
    tags: Iterable[str],
    resolver: WikiCountResolver,
    rules: DanbooruCountRules,
    overwrite_count: bool,
) -> CountDecision:
    normalized_tags = tuple(dict.fromkeys(tag for tag in tags if tag))
    resolution = resolver.resolve_danbooru(normalized_tags, rules)
    original_normalized = normalize_original_count(original_raw)
    has_original = original_raw is not None and (
        not isinstance(original_raw, str) or bool(original_raw.strip())
    )
    issue_codes: list[str] = []
    warnings = list(resolution.warnings)
    if has_original and original_normalized is None:
        _append_once(issue_codes, "original_count_invalid")
    source_conflict = bool(
        original_normalized and resolution.value and original_normalized != resolution.value
    )
    if source_conflict:
        _append_once(issue_codes, "count_source_conflict")
        _append_once(warnings, "count_source_conflict")
    if overwrite_count and resolution.value:
        base_value, selected_source = resolution.value, "wiki_tags"
    elif original_normalized:
        base_value, selected_source = original_normalized, "original_json"
    elif resolution.value:
        base_value, selected_source = resolution.value, "wiki_tags"
    else:
        base_value, selected_source = "", "none"
    resolver_conflict = any(warning.startswith("count_conflict:") for warning in warnings)
    return CountDecision(
        value=base_value,
        base_value=base_value,
        selected_source=selected_source,
        original_raw=original_raw,
        original_normalized=original_normalized,
        wiki_value=resolution.value,
        matched_tags=resolution.matched_tags,
        conflict=source_conflict or resolver_conflict,
        issue_codes=tuple(issue_codes),
        warnings=tuple(warnings),
        applied_lower_bounds=resolution.applied_lower_bounds,
    )
