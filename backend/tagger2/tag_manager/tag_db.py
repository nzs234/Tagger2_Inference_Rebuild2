"""Process-level booru tag database over classify-snapshot resources.

The tag manager needs autocomplete and category lookup against the official
e621/Danbooru tag tables. Those tables already exist as ``classify-snapshot-v1``
resources in the workflow resource catalog, so this module indexes them instead
of introducing a second copy of the official data.

Snapshots are large (100k+ tags), so each profile's index is built once per
process on first use and reused across :class:`TagDatabase` instances that point
at the same catalog; loading is serialized by a module-level lock. All lookup
structures are immutable after construction, so reads need no lock. An explicit
``resource_id`` on :meth:`TagDatabase.ensure_loaded` replaces the cached index,
which keeps a deliberate reload (a newer snapshot, a test fixture) possible.
"""

from __future__ import annotations

import bisect
import json
import os
import threading
from typing import Any, TypedDict

from ..workflow.resources import CLASSIFY_RESOURCE_CATEGORY, WorkflowResourceCatalog

# e621 alias chains are flattened to their final target exactly like
# ``workflow.stages.classify.build_classification_rules`` so both consumers
# agree on what "canonical" means.
_MAX_ALIAS_CHAIN = 1000

# Casefolding is the same insensitive-equality rule the workflow's tag
# normalization uses; the cache key is normalized so a catalog reached through
# a differently cased path still shares the process-level index.
_CACHE: dict[tuple[str, str], "_ProfileIndex"] = {}
_CACHE_LOCK = threading.Lock()


class TagDatabaseError(ValueError):
    """Raised when no valid classification snapshot is available for a profile."""


class TagInfo(TypedDict):
    name: str
    category: str
    post_count: int | None
    alias_of: str | None


class _ProfileIndex:
    """Immutable lookup structures for one loaded snapshot."""

    __slots__ = ("aliases", "resource_id", "sorted_names", "tags")

    def __init__(
        self,
        resource_id: str,
        tags: dict[str, TagInfo],
        aliases: dict[str, tuple[str, str]],
    ) -> None:
        # casefolded canonical name -> TagInfo (``alias_of`` is always None here).
        self.tags = tags
        # casefolded alias antecedent -> (display antecedent, casefolded canonical).
        self.aliases = aliases
        # Sorted casefolded canonical names; bisects the autocomplete range.
        self.sorted_names = tuple(sorted(tags))
        self.resource_id = resource_id


def _default_catalog() -> WorkflowResourceCatalog:
    """Construct the same default catalog the workflow runtime uses."""

    from ..config import get_settings

    settings = get_settings()
    data_dir = settings.data_dir or settings.project_root / "data"
    return WorkflowResourceCatalog(data_dir / "workflows" / "resources")


class TagDatabase:
    """In-memory tag index over classify-snapshot resources.

    Snapshots are large (100k+ tags), so each profile's index is loaded once
    per process on first use and reused; loading is serialized by a lock.
    """

    def __init__(self, catalog: WorkflowResourceCatalog | None = None) -> None:
        self._catalog = catalog if catalog is not None else _default_catalog()

    # -- resource discovery -------------------------------------------------

    def available_profiles(self) -> dict[str, list[str]]:
        """Map profile -> available classify resource ids, newest first."""

        grouped: dict[str, list[tuple[str, str]]] = {}
        for manifest in self._catalog.list_resources(CLASSIFY_RESOURCE_CATEGORY):
            profile = manifest.profile
            if not profile:
                # A manifest without profile metadata cannot be attributed to a
                # snapshot profile; the import script always records it.
                continue
            grouped.setdefault(profile, []).append((manifest.created_at, manifest.resource_id))
        return {
            profile: [resource_id for _created_at, resource_id in sorted(entries, reverse=True)]
            for profile, entries in grouped.items()
        }

    def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
        """Load (once) the newest snapshot for ``profile`` or the given resource.

        Calling this again for an already loaded profile without an explicit
        ``resource_id`` is a no-op; an explicit id that differs from the loaded
        one replaces the index.
        """

        from ..workflow.classify_snapshot import PROFILE_CATEGORIES

        if profile not in PROFILE_CATEGORIES:
            raise TagDatabaseError(f"unsupported tag profile: {profile!r}")
        key = self._cache_key(profile)
        with _CACHE_LOCK:
            index = _CACHE.get(key)
            if index is not None and (resource_id is None or index.resource_id == resource_id):
                return
            resolved_id = resource_id
            if resolved_id is None:
                available = self.available_profiles().get(profile, ())
                resolved_id = available[0] if available else None
            if resolved_id is None:
                raise TagDatabaseError(
                    f"no classification snapshot resource is available for profile {profile!r}"
                )
            document = self._load_snapshot(profile, resolved_id)
            _CACHE[key] = _build_index(resolved_id, document)

    def is_loaded(self, profile: str) -> bool:
        """Return whether ``profile`` already has a loaded index in this process."""

        return self._cache_key(profile) in _CACHE

    # -- lookups ------------------------------------------------------------

    def lookup(
        self, profile: str, tag: str, *, resolve_alias: bool = True
    ) -> TagInfo | None:
        """Case-insensitively look up one tag.

        When ``tag`` names a known alias antecedent and ``resolve_alias`` is
        true, the canonical tag is returned with ``alias_of`` set to the
        antecedent. An unloaded profile is loaded first; unknown tags return
        None. Raises :class:`TagDatabaseError` when no snapshot is available.
        """

        self.ensure_loaded(profile)
        index = _CACHE[self._cache_key(profile)]
        key = tag.casefold()
        if resolve_alias:
            alias = index.aliases.get(key)
            if alias is not None:
                antecedent, canonical = alias
                entry = index.tags.get(canonical)
                if entry is None:
                    return None
                return _copy_tag_info(entry, alias_of=antecedent)
        entry = index.tags.get(key)
        return _copy_tag_info(entry) if entry is not None else None

    def autocomplete(self, profile: str, query: str, *, limit: int = 20) -> list[TagInfo]:
        """Prefix search over canonical names, best post_count first.

        Alias antecedents never produce their own rows. An empty or whitespace
        query returns an empty list. Raises :class:`TagDatabaseError` when no
        snapshot is available.
        """

        self.ensure_loaded(profile)
        text = (query or "").strip()
        if not text or limit <= 0:
            return []
        index = _CACHE[self._cache_key(profile)]
        prefix = text.casefold()
        names = index.sorted_names
        start = bisect.bisect_left(names, prefix)
        matches: list[TagInfo] = []
        for position in range(start, len(names)):
            name = names[position]
            if not name.startswith(prefix):
                break
            matches.append(index.tags[name])
        matches.sort(key=_autocomplete_order)
        return [_copy_tag_info(entry) for entry in matches[:limit]]

    # -- loading ------------------------------------------------------------

    def _cache_key(self, profile: str) -> tuple[str, str]:
        scope = os.path.normcase(str(self._catalog.resource_dir))
        return (scope, profile)

    def _load_snapshot(self, profile: str, resource_id: str) -> dict[str, Any]:
        """Read one snapshot document through the resource catalog.

        Subclass/override point for tests that want to index an in-memory
        snapshot without staging a resource file.
        """

        from ..workflow.classify_snapshot import CLASSIFY_SNAPSHOT_FORMAT

        path = self._catalog.get_resource_path(resource_id)
        if path is None:
            raise TagDatabaseError(
                f"classification snapshot resource {resource_id!r} is not available"
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise TagDatabaseError(
                f"classification snapshot resource {resource_id!r} could not be read"
            ) from exc
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            document = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise TagDatabaseError(
                f"classification snapshot resource {resource_id!r} is not valid UTF-8"
            ) from exc
        except json.JSONDecodeError as exc:
            raise TagDatabaseError(
                f"classification snapshot resource {resource_id!r} is not valid JSON"
            ) from exc
        if (
            not isinstance(document, dict)
            or document.get("format") != CLASSIFY_SNAPSHOT_FORMAT
            or document.get("profile") != profile
        ):
            raise TagDatabaseError(
                f"classification snapshot resource {resource_id!r} is not a"
                f" {CLASSIFY_SNAPSHOT_FORMAT} document for profile {profile!r}"
            )
        return document


def _build_index(resource_id: str, document: dict[str, Any]) -> _ProfileIndex:
    """Build the immutable lookup structures from a snapshot document."""

    tags: dict[str, TagInfo] = {}
    for row in document.get("tags", ()):
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        post_count = row.get("post_count")
        if not isinstance(post_count, int) or isinstance(post_count, bool):
            post_count = None
        category = row.get("category")
        # The snapshot builder already validated categories against the
        # profile's declared table, so unknown strings are kept as-is.
        tags[name.casefold()] = {
            "name": name,
            "category": category if isinstance(category, str) else "",
            "post_count": post_count,
            "alias_of": None,
        }

    raw_aliases: dict[str, tuple[str, str]] = {}
    for row in document.get("aliases", ()):
        if not isinstance(row, dict):
            continue
        antecedent = row.get("antecedent_name")
        consequent = row.get("consequent_name")
        if not isinstance(antecedent, str) or not isinstance(consequent, str):
            continue
        raw_aliases[antecedent.casefold()] = (antecedent, consequent.casefold())

    aliases: dict[str, tuple[str, str]] = {}
    for antecedent_key, (display, first_target) in raw_aliases.items():
        current = first_target
        visited = {antecedent_key}
        for _step in range(_MAX_ALIAS_CHAIN):
            nxt = raw_aliases.get(current)
            if nxt is None:
                break
            if nxt[1] in visited:
                raise TagDatabaseError(
                    f"alias cycle detected while indexing snapshot {resource_id!r}"
                )
            visited.add(nxt[1])
            current = nxt[1]
        aliases[antecedent_key] = (display, current)

    return _ProfileIndex(resource_id, tags, aliases)


def _copy_tag_info(entry: TagInfo, *, alias_of: str | None = None) -> TagInfo:
    """Copy a stored entry so callers can never mutate the shared index."""

    return {
        "name": entry["name"],
        "category": entry["category"],
        "post_count": entry["post_count"],
        "alias_of": entry["alias_of"] if alias_of is None else alias_of,
    }


def _autocomplete_order(entry: TagInfo) -> tuple[int, str]:
    post_count = entry["post_count"]
    return (-(post_count if post_count is not None else 0), entry["name"].casefold())


__all__ = [
    "TagDatabase",
    "TagDatabaseError",
    "TagInfo",
]
