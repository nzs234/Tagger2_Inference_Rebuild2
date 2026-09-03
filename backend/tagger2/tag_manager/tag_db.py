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

    __slots__ = ("aliases", "implications", "resource_id", "sorted_names", "tags")

    def __init__(
        self,
        resource_id: str,
        tags: dict[str, TagInfo],
        aliases: dict[str, tuple[str, str]],
        implications: tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]] = ({}, {}),
    ) -> None:
        # casefolded canonical name -> TagInfo (``alias_of`` is always None here).
        self.tags = tags
        # casefolded alias antecedent -> (display antecedent, casefolded canonical).
        self.aliases = aliases
        # (forward, reverse) maps of implications between canonical keys.
        self.implications = implications
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

    def implications_of(
        self, profile: str, tag: str, *, reverse: bool = False
    ) -> list[TagInfo]:
        """Resolve the tag through aliases, returning implied or implying tags.

        Resolves ``tag`` through aliases (casefold), returning the :class:`TagInfo`
        for each mapped key (skipping keys not present in canonical tags;
        sorted by post_count desc, then name). ``reverse=True`` answers
        which tags imply this one. Unknown tags return an empty list.
        """

        self.ensure_loaded(profile)
        index = _CACHE[self._cache_key(profile)]
        key = tag.casefold()
        alias = index.aliases.get(key)
        canonical_key = alias[1] if alias is not None else key

        forward_map, reverse_map = index.implications
        target_map = reverse_map if reverse else forward_map
        mapped_keys = target_map.get(canonical_key, ())
        results: list[TagInfo] = []
        for target_key in mapped_keys:
            entry = index.tags.get(target_key)
            if entry is not None:
                results.append(entry)
        results.sort(key=_autocomplete_order)
        return [_copy_tag_info(entry) for entry in results]

    def top_tags(
        self, profile: str, *, min_post_count: int = 0, limit: int | None = None
    ) -> list[TagInfo]:
        """All canonical tags with post_count >= min_post_count sorted by post_count desc then name."""

        self.ensure_loaded(profile)
        index = _CACHE[self._cache_key(profile)]
        matches: list[TagInfo] = []
        for entry in index.tags.values():
            count = entry["post_count"] or 0
            if count >= min_post_count:
                matches.append(entry)
        matches.sort(key=_autocomplete_order)
        if limit is not None and limit >= 0:
            matches = matches[:limit]
        return [_copy_tag_info(entry) for entry in matches]

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
            # Model-class blobs are not packaged: the first consumer starts the
            # background fetch and callers surface progress until it lands.
            from ..workflow.resource_fetch import manager_for

            state = manager_for(self._catalog).get_or_start(resource_id)
            if state.state != "ready" or state.path is None:
                raise TagDatabaseError(
                    f"分类快照资源 {resource_id!r} 尚不可用（{state.progress_text()}）"
                    "；下载完成后重试，或手动导入资源文件"
                )
            path = state.path
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

    # Implications: antecedent_name -> consequent_name.
    # Resolve both antecedent and consequent through aliases if needed,
    # mapping canonical antecedent -> tuple of canonical consequents,
    # and canonical consequent -> tuple of canonical antecedents.
    forward_implications: dict[str, list[str]] = {}
    reverse_implications: dict[str, list[str]] = {}

    def _resolve_canonical(key: str) -> str:
        alias_entry = aliases.get(key)
        return alias_entry[1] if alias_entry is not None else key

    for row in document.get("implications", ()):
        if not isinstance(row, dict):
            continue
        antecedent = row.get("antecedent_name")
        consequent = row.get("consequent_name")
        if not isinstance(antecedent, str) or not isinstance(consequent, str):
            continue
        ant_key = antecedent.casefold()
        con_key = consequent.casefold()
        ant_canonical = _resolve_canonical(ant_key)
        con_canonical = _resolve_canonical(con_key)
        if ant_canonical == con_canonical:
            continue
        # Only record if canonical antecedent and consequent are valid / distinct
        forward_list = forward_implications.setdefault(ant_canonical, [])
        if con_canonical not in forward_list:
            forward_list.append(con_canonical)
        reverse_list = reverse_implications.setdefault(con_canonical, [])
        if ant_canonical not in reverse_list:
            reverse_list.append(ant_canonical)

    frozen_forward = {k: tuple(v) for k, v in forward_implications.items()}
    frozen_reverse = {k: tuple(v) for k, v in reverse_implications.items()}

    return _ProfileIndex(resource_id, tags, aliases, (frozen_forward, frozen_reverse))


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
