"""Classify stage: map caption tags to the nine-field structure.

This stage consumes the flat tag list from Caption and produces the structured
nine-field projection: quality, count, character, series, artist, appearance,
tags, environment, nl.

It relies on an official e621 or Danbooru snapshot (tags table, tag_aliases,
tag_implications) to determine each tag's category and normalize aliases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


class ClassifyError(RuntimeError):
    """Raised when classification cannot proceed."""


@dataclass(frozen=True)
class TagRecord:
    """One tag from the official tags table."""
    name: str
    category: str  # "general", "artist", "copyright", "character", "species", "meta"
    post_count: int = 0


@dataclass(frozen=True)
class ClassificationRules:
    """Classification resources for one profile (e621 or danbooru)."""
    
    profile: str
    tags: Mapping[str, TagRecord]
    aliases: Mapping[str, str]  # source -> target (flattened, no chains)
    implications: Mapping[str, tuple[str, ...]]  # tag -> implied_tags (for tracing only)
    
    # Category name mappings for each profile
    # e621: general, artist, copyright, character, species, meta
    # danbooru: general, artist, copyright, character, meta
    
    def normalize(self, raw_tag: str) -> str:
        """Resolve alias chain and return canonical tag name."""
        canonical = self.aliases.get(raw_tag, raw_tag)
        return canonical
    
    def category(self, tag: str) -> str | None:
        """Return the category for a canonical tag, or None if unknown."""
        record = self.tags.get(tag)
        return record.category if record else None


def build_classification_rules(
    profile: str,
    tags_data: list[dict],
    aliases_data: list[dict],
    implications_data: list[dict] | None = None,
) -> ClassificationRules:
    """Build classification rules from official snapshot data.
    
    Args:
        profile: "e621" or "danbooru"
        tags_data: List of tag records with keys: name, category, post_count
        aliases_data: List of alias records with keys: antecedent_name, consequent_name
        implications_data: Optional list of implication records (for tracing)
    
    Returns:
        ClassificationRules with flattened alias chains and indexed tags
    
    Raises:
        ClassifyError: If data is invalid or contains cycles
    """
    
    if profile not in {"e621", "danbooru"}:
        raise ClassifyError(f"unsupported classification profile: {profile!r}")
    
    # Build tags index
    tags: dict[str, TagRecord] = {}
    for row in tags_data:
        try:
            name = str(row["name"])
            category = str(row["category"])
            post_count = int(row.get("post_count", 0))
            tags[name] = TagRecord(name, category, post_count)
        except (KeyError, ValueError, TypeError) as exc:
            raise ClassifyError(f"invalid tag record: {row!r}") from exc
    
    # Build raw alias map
    raw_aliases: dict[str, str] = {}
    for row in aliases_data:
        try:
            source = str(row["antecedent_name"])
            target = str(row["consequent_name"])
            raw_aliases[source] = target
        except (KeyError, ValueError, TypeError) as exc:
            raise ClassifyError(f"invalid alias record: {row!r}") from exc
    
    # Flatten alias chains and detect cycles
    aliases: dict[str, str] = {}
    for source in raw_aliases:
        visited = {source}
        current = source
        for _ in range(1000):  # Arbitrary limit to prevent infinite loops
            next_target = raw_aliases.get(current)
            if next_target is None:
                aliases[source] = current
                break
            if next_target in visited:
                raise ClassifyError(
                    f"alias cycle detected: {source} -> {' -> '.join(visited)} -> {next_target}"
                )
            visited.add(next_target)
            current = next_target
        else:
            raise ClassifyError(f"alias chain too long: {source}")
    
    # Build implications index (for tracing, not auto-expansion)
    implications: dict[str, tuple[str, ...]] = {}
    if implications_data:
        for row in implications_data:
            try:
                source = str(row["antecedent_name"])
                target = str(row["consequent_name"])
                implications.setdefault(source, ())
                implications[source] = implications[source] + (target,)
            except (KeyError, ValueError, TypeError) as exc:
                raise ClassifyError(f"invalid implication record: {row!r}") from exc
    
    return ClassificationRules(
        profile=profile,
        tags=tags,
        aliases=aliases,
        implications=implications,
    )


def classify_tags(
    raw_tags: list[str],
    rules: ClassificationRules,
) -> dict[str, list[str]]:
    """Classify a flat tag list into the nine-field structure.
    
    Args:
        raw_tags: Flat list of tags from Caption stage
        rules: Classification rules for the target profile
    
    Returns:
        Dictionary with keys: quality, character, artist, appearance, tags,
        environment. ``count``, ``series`` and ``nl`` are decided elsewhere:
        ``count`` comes from the count rules, ``nl`` from the NL stage, and
        ``series`` stays empty for e621 by frozen source behaviour.
    """
    
    # Normalize all tags through alias resolution
    canonical_tags: list[str] = []
    for raw_tag in raw_tags:
        canonical = rules.normalize(raw_tag)
        if canonical not in canonical_tags:  # Basic dedup
            canonical_tags.append(canonical)
    
    # Classify by category
    quality: list[str] = []
    character: list[str] = []
    artist: list[str] = []
    appearance: list[str] = []
    tags: list[str] = []
    environment: list[str] = []
    
    for tag in canonical_tags:
        category = rules.category(tag)
        
        # e621/danbooru category mapping
        if category == "character":
            character.append(tag)
        elif category == "artist":
            # Returned separately so the caller can merge it into the `artist`
            # string field; dropping it here would lose the tag entirely.
            artist.append(tag)
        elif category == "copyright":
            # `series` stays empty for e621 by frozen source behaviour, so a
            # copyright tag is kept in `tags` rather than being discarded.
            tags.append(tag)
        elif category == "meta":
            # Meta tags like ratings go to quality
            if tag.startswith("rating_"):
                quality.append(tag)
            else:
                tags.append(tag)
        elif category == "species":
            # Species goes to appearance for e621
            appearance.append(tag)
        else:
            # A general tag that cannot be reliably subdivided lands in `tags`
            # deterministically. `appearance` and `environment` are only filled
            # from categories that state the distinction (for example species),
            # never from a guess about a general tag.
            tags.append(tag)
    
    return {
        "quality": quality,
        "character": character,
        "artist": artist,
        "appearance": appearance,
        "tags": tags,
        "environment": environment,
    }


__all__ = [
    "ClassifyError",
    "TagRecord",
    "ClassificationRules",
    "build_classification_rules",
    "classify_tags",
]
