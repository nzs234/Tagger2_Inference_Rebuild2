"""Tag wiki service: build pipeline, the read-only catalog and the translate job.

The service owns the whole feature surface behind a small API:

- ``start_build`` downloads the official e621 ``wiki_pages`` db_export dump
  and imports it into :class:`WikiStore`, then prunes unsearchable chunks —
  as one background asyncio task with phase progress reported through
  :meth:`status`. The danbooru mirror ships pre-imported
  (scripts/fetch_danbooru_wiki.py). There is no vector pass: the browse UI
  is the read-only tag catalog, which ``scripts/build_tag_wiki_catalog.py``
  maintains separately.
- ``catalog_categories`` / ``catalog_browse`` / ``catalog_tag_detail`` serve
  the read-only booru-style tag directory (high-frequency tags only, grouped
  by the deterministic two-level taxonomy). Their data lives in the catalog
  tables written exclusively by that maintainer CLI.
- ``lookup`` / ``page`` resolve one tag or page for the shared wiki drawer
  and other consumers.
- ``start_translate`` batch-produces structured Chinese summaries for the
  most useful pages (model vocabulary by default) with the configured
  online providers.

Errors use the app-wide conventions: a 409 with a stable ``code`` for setup
states (no wiki data, no provider) and a retryable 502 for upstream
provider failures.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ..tag_manager.tag_db import TagDatabase, TagDatabaseError, TagInfo
from ..tag_manager.translations import TagTranslations
from .contracts import (
    CATALOG_DEFAULT_PAGE_SIZE,
    CATALOG_MAX_PAGE_SIZE,
    CATALOG_SEARCH_CANDIDATE_CAP,
    ERROR_WIKI_ASK_UNAVAILABLE,
    ERROR_WIKI_BUILD_FAILED,
    ERROR_WIKI_BUSY,
    ERROR_WIKI_CATALOG_MISSING,
    ERROR_WIKI_CATALOG_TAG_NOT_FOUND,
    ERROR_WIKI_FROZEN,
    ERROR_WIKI_LOOKUP_FAILED,
    ERROR_WIKI_NOT_BUILT,
    ERROR_WIKI_PAGE_NOT_FOUND,
    ERROR_WIKI_TAG_DB_UNAVAILABLE,
    BuildRequest,
    TagRef,
    TranslateRequest,
)
from .importer import (
    ImporterError,
    download_dump,
    dump_filename_for_url,
    import_dump,
    latest_dump_html,
    latest_dump_url,
)
from .danbooru_importer import default_danbooru_store_path
from .taxonomy import (
    GROUP_ORDER,
    category_label,
    group_label,
    group_sort_key,
    split_tokens,
)
from .translator import translate_pages
from .wiki_store import WikiStore, default_tag_wiki_database_path, normalize_title

logger = logging.getLogger("tagger2.tag_wiki")

_WIKI_PROFILE = "e621"

# Every wiki mirror the service serves. Stores live in one SQLite file per
# profile; queries, builds and translate jobs all take an explicit profile.
WIKI_PROFILES: tuple[str, ...] = ("e621", "danbooru")

# Page categories whose wiki bodies are link lists / reference stubs, not
# prose. Their chunks are removed at build time and their pages stay out of
# the translate scope; the pages themselves remain for exact lookup.
EXCLUDED_SEARCH_CATEGORIES = frozenset({"artist", "character", "contributor", "invalid"})


class TagWikiError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _page_public(page: dict[str, Any]) -> dict[str, Any]:
    """Trim a store page to the documented ``WikiPageInfo`` shape."""

    return {
        "title": str(page.get("title", "")),
        "wiki_id": page.get("wiki_id"),
        "updated_at": page.get("updated_at"),
        "url": page.get("url"),
        "summary": page.get("summary"),
        "sections": page.get("sections", []),
        "related_tags": page.get("related_tags", []),
    }


# Fuzzy-search tiers for catalog results, best first: the exact canonical
# name, the canonical tag behind an exact alias, a name-prefix hit, then
# token-prefix / contained substring hits.
CATALOG_MATCH_TIERS: tuple[str, ...] = ("exact", "alias", "prefix", "token", "contained")

# Relation buckets are sorted by target popularity, so the first entries are
# the useful ones; popular tags otherwise drag hundreds of wiki links into
# one detail response (pokemon_(species) has 800+), which floods the UI.
_CATALOG_MAX_RELATIONS_PER_BUCKET = 30


def _catalog_match_tier(q: str, tokens: Sequence[str], name: str) -> int:
    """Rank one candidate name against the normalized query.

    Tier order matches ``CATALOG_MATCH_TIERS``; the store's LIKE fetch only
    guarantees "contained", so this refines the store ordering (post_count
    desc) into relevance order without losing it inside a tier.
    """

    if name == q:
        return 0
    if name.startswith(q):
        return 2
    name_tokens = split_tokens(name)
    for token in tokens:
        if any(name_token.startswith(token) for name_token in name_tokens):
            return 3
    return 4


def _catalog_row_matches_filters(row: Mapping[str, Any], category: str | None, group: str | None) -> bool:
    """Whether one catalog row satisfies the browse sidebar filters."""

    if category is not None and str(row.get("category", "")) != category:
        return False
    if group is not None and str(row.get("group_key", "")) != group:
        return False
    return True


class TagWikiService:
    """Facade over the wiki store, the tag catalog and the providers."""

    def __init__(
        self,
        *,
        store: WikiStore | None = None,
        danbooru_store: WikiStore | None = None,
        tag_database: TagDatabase | None = None,
        translations: TagTranslations | None = None,
        provider_factory: Callable[[str], Any] | None = None,
        provider_ids: Callable[[], list[str]] | None = None,
        vocab_provider: Callable[[], Sequence[str]] | None = None,
        data_dir: Path | None = None,
        default_min_post_count: int = 1000,
        frozen: bool = False,
    ) -> None:
        if data_dir is None:
            from ..config import get_settings

            settings = get_settings()
            data_dir = settings.data_dir or settings.project_root / "data"
        self._data_dir = Path(data_dir)
        # One store per profile; files are created lazily so a fresh checkout
        # (or a test that never touches danbooru) does not touch the disk.
        self._stores: dict[str, WikiStore] = {}
        if store is not None:
            self._stores["e621"] = store
        if danbooru_store is not None:
            self._stores["danbooru"] = danbooru_store
        self.tag_database = tag_database if tag_database is not None else TagDatabase()
        # The tag-name dictionaries ship with the app; tests inject their own.
        self.translations = translations if translations is not None else TagTranslations()
        self._provider_factory = provider_factory
        self._provider_ids = provider_ids
        self._vocab_provider = vocab_provider
        self._default_min_post_count = max(0, int(default_min_post_count))
        # Frozen ships as a finished product: the bundled wiki databases are
        # maintained by the packager, so build and translate entry points are
        # rejected with a 403 instead of racing the bundle.
        self._frozen = bool(frozen)
        self._downloads_dir = self._data_dir / "tag_wiki" / "downloads"
        self._build_state: dict[str, Any] = {
            "state": "idle",
            "phase": "idle",
            "message": "",
            "started_at": None,
            "updated_at": None,
            "error": None,
        }
        self._build_task: asyncio.Task[None] | None = None
        self._translate_state: dict[str, Any] = {
            "state": "idle",
            "done": 0,
            "failed": 0,
            "total": 0,
            "provider_id": "",
            "model": "",
            "message": "",
            "started_at": None,
            "updated_at": None,
            "error": None,
            "profile": "",
        }
        self._translate_task: asyncio.Task[None] | None = None

    # -- stores ---------------------------------------------------------------

    @property
    def store(self) -> WikiStore:
        """The e621 store (the module's original single-profile database)."""

        return self._store_for("e621")

    def _store_for(self, profile: str) -> WikiStore:
        """Return the per-profile wiki store, creating it on first use.

        Profiles live in separate SQLite files (e621: ``tag_wiki.sqlite3``,
        danbooru: ``tag_wiki_danbooru.sqlite3``). An absent file is created
        empty so status can report a not-yet-built profile instead of failing.
        """

        store = self._stores.get(profile)
        if store is not None:
            return store
        if profile == "e621":
            store = WikiStore(default_tag_wiki_database_path())
        elif profile == "danbooru":
            store = WikiStore(default_danbooru_store_path())
        else:
            raise TagWikiError(
                f"未知的 Wiki profile：{profile}", code=ERROR_WIKI_LOOKUP_FAILED, status_code=400
            )
        self._stores[profile] = store
        return store

    # -- status -------------------------------------------------------------

    def _profile_status(self, profile: str) -> dict[str, Any]:
        store = self._store_for(profile)
        meta = store.page_meta()
        catalog = self.catalog_status(profile)
        return {
            "database": meta,
            "catalog": {
                "built": catalog["built"],
                "tag_count": catalog["tag_count"],
                "relation_count": catalog["relation_count"],
                "min_post_count": catalog["min_post_count"],
                "generated_at": catalog["generated_at"],
            },
        }

    def status(self) -> dict[str, Any]:
        profiles = {name: self._profile_status(name) for name in WIKI_PROFILES}
        e621 = profiles["e621"]
        return {
            "profiles": profiles,
            # Backward-compatible top-level view of the e621 profile.
            "database": e621["database"],
            # True in packaged builds: the bundled databases are read-only and
            # the maintenance entry points below return 403.
            "frozen": self._frozen,
            "build": dict(self._build_state),
            "translate": dict(self._translate_state),
        }

    # -- build pipeline -----------------------------------------------------

    def _require_not_frozen(self) -> None:
        """Reject maintenance entry points in packaged (frozen) builds."""

        if self._frozen:
            raise TagWikiError(
                "成品包模式下 wiki 数据为只读，构建/重建/翻译由发布者完成后随包分发",
                code=ERROR_WIKI_FROZEN,
                status_code=403,
            )

    async def start_build(self, request: BuildRequest) -> dict[str, Any]:
        self._require_not_frozen()
        if self._build_task is not None and not self._build_task.done():
            raise TagWikiError("已有一次构建在进行中", code=ERROR_WIKI_BUSY, status_code=409)
        self._set_build_state(
            state="running",
            # e621 starts at the dump download; the danbooru corpus ships
            # pre-imported, so its pipeline starts at the pruning stage.
            phase="download" if request.profile == "e621" else "parse",
            message="开始构建",
            started_at=_now(),
            error=None,
            profile=request.profile,
        )
        self._build_task = asyncio.create_task(self._run_build(request))
        return self.status()

    async def _run_build(self, request: BuildRequest) -> None:
        try:
            profile = request.profile
            store = self._store_for(profile)
            if profile == "e621":
                dump_path = await self._ensure_dump(request.download_dump)
                if request.reindex:
                    self._set_build_state(phase="parse", message="解析 wiki dump 并入库")
                    counts = await asyncio.to_thread(import_dump, store, dump_path)
                    logger.info("tag wiki import finished: %s", counts)
            self._set_build_state(phase="parse", message="剔除不可检索页面的章节")
            pruned = await asyncio.to_thread(self._prune_unsearchable_chunks_sync, profile)
            if pruned:
                logger.info("tag wiki pruned %d unsearchable chunks (%s)", pruned, profile)
            self._set_build_state(
                state="idle",
                phase="done",
                message=f"构建完成：导入/更新 {store.page_count()} 页，剔除 {pruned} 个章节",
            )
        except asyncio.CancelledError:
            self._set_build_state(state="idle", phase="idle", message="构建已取消")
            raise
        except Exception as exc:  # noqa: BLE001 - every failure lands in status
            logger.exception("tag wiki build failed")
            self._set_build_state(state="error", message="构建失败", error=str(exc))

    def _set_build_state(self, **changes: Any) -> None:
        self._build_state.update(changes)
        self._build_state["updated_at"] = _now()

    async def _ensure_dump(self, download: bool) -> Path:
        """Return the newest cached dump, refreshing from e621 when asked."""

        self._downloads_dir.mkdir(parents=True, exist_ok=True)
        cached = sorted(self._downloads_dir.glob("wiki_pages-*.csv.gz"))
        if not download and cached:
            return cached[-1]
        try:
            html = await asyncio.to_thread(latest_dump_html)
            url = latest_dump_url(html)
            latest_name = dump_filename_for_url(url)
            for path in cached:
                if path.name == latest_name:
                    self._set_build_state(message=f"dump 已是最新：{latest_name}")
                    return path
            self._set_build_state(message=f"下载 {latest_name}")
            return await asyncio.to_thread(download_dump, url, self._downloads_dir)
        except ImporterError as exc:
            if cached:
                logger.warning("tag wiki dump refresh failed, reusing %s", cached[-1].name)
                self._set_build_state(message=f"在线获取失败，使用本地缓存 {cached[-1].name}")
                return cached[-1]
            raise TagWikiError(
                f"获取 wiki 数据失败：{exc}", code=ERROR_WIKI_BUILD_FAILED, status_code=502, retryable=True
            ) from exc

    def _prune_unsearchable_chunks_sync(self, profile: str = _WIKI_PROFILE) -> int:
        """Delete chunks that are useless for semantic search.

        Two idempotent sweeps, both cheap enough for every build: category
        based (artist/character/contributor/invalid pages, per the tag
        database) and shape based (chunks that are nothing but external-URL
        lines, which also catches stub pages missing from the tag database).
        The category sweep is skipped when the tag database is unavailable;
        the shape sweep never needs it.
        """

        store = self._store_for(profile)
        excluded: list[str] = []
        for title in store.iter_page_titles():
            try:
                info = self.tag_database.lookup(profile, title)
            except TagDatabaseError:
                excluded = []
                break
            if info is not None and str(info["category"]) in EXCLUDED_SEARCH_CATEGORIES:
                excluded.append(title)
        pruned = store.delete_chunks_for_pages(excluded) if excluded else 0
        return pruned + store.delete_link_soup_chunks()

    # -- lookup -------------------------------------------------------------

    async def lookup(self, tag: str, *, profile: str = _WIKI_PROFILE) -> dict[str, Any]:
        """Resolve one tag to its meaning: info, implications and wiki page."""

        query = tag.strip()
        if not query:
            raise TagWikiError("请输入要查询的 tag", code=ERROR_WIKI_LOOKUP_FAILED, status_code=400)
        if len(query) > 128:
            raise TagWikiError("tag 过长", code=ERROR_WIKI_LOOKUP_FAILED, status_code=400)
        store = self._store_for(profile)
        self._require_data(profile)
        info = self._tag_info(profile, query, required=True)
        canonical = info["name"] if info else normalize_title(query)
        implications: list[TagRef] = []
        if info is not None:
            try:
                imp_infos = self.tag_database.implications_of(profile, canonical)
            except TagDatabaseError:
                imp_infos = []
            implications = [self._ref_from_info(profile, item) for item in imp_infos]
        page = store.get_page(canonical)
        return {
            "query": query,
            "resolved": info is not None,
            "tag": self._ref_from_info(profile, info) if info is not None else None,
            "implications": implications,
            "page": _page_public(page) if page is not None else None,
        }

    async def page(self, title: str, *, profile: str = _WIKI_PROFILE) -> dict[str, Any]:
        """Return one full wiki page (trimmed to the documented shape)."""

        store = self._store_for(profile)
        self._require_data(profile)
        page = store.get_page(title)
        if page is None:
            raise TagWikiError(f"Wiki 页面不存在：{title}", code=ERROR_WIKI_PAGE_NOT_FOUND, status_code=404)
        return _page_public(page)

    # -- tag catalog (read-only booru-style directory) -----------------------

    def _require_catalog(self, profile: str = _WIKI_PROFILE) -> WikiStore:
        """Return the profile store, raising 409 when the catalog is absent.

        The catalog tables are written only by
        ``scripts/build_tag_wiki_catalog.py``; until then every ``/catalog``
        endpoint answers with a stable setup error instead of an empty page.
        """

        store = self._store_for(profile)
        if not store.catalog_built():
            raise TagWikiError(
                "标签目录尚未生成：请先运行 scripts/build_tag_wiki_catalog.py",
                code=ERROR_WIKI_CATALOG_MISSING,
                status_code=409,
            )
        return store

    def _catalog_item(
        self,
        profile: str,
        row: Mapping[str, Any],
        *,
        alias_of: str | None = None,
        match: str | None = None,
    ) -> dict[str, Any]:
        """Shape one ``catalog_tags`` row into the documented item dict."""

        item: dict[str, Any] = {
            "name": str(row["name"]),
            "translation": self.translations.translate(profile, str(row["name"])),
            "category": str(row["category"]),
            "group_key": str(row["group_key"]),
            "group_label": group_label(str(row["group_key"])),
            "post_count": int(row.get("post_count") or 0),
            "has_wiki": bool(row.get("has_wiki")),
            "alias_of": alias_of if alias_of is not None else row.get("alias_of"),
        }
        if match is not None:
            item["match"] = match
        return item

    def catalog_status(self, profile: str = _WIKI_PROFILE) -> dict[str, Any]:
        """Catalog generation metadata for one profile (never raises 409)."""

        store = self._store_for(profile)
        built = store.catalog_built()
        meta = store.catalog_meta() if built else {}
        return {
            "built": built,
            "profile": profile,
            "generated_at": meta.get("generated_at"),
            "taxonomy_version": int(meta.get("taxonomy_version") or 0) or None,
            "min_post_count": int(meta.get("min_post_count") or 0) or None,
            "tag_count": store.catalog_tag_count(),
            "relation_count": store.catalog_relation_count(),
        }

    async def catalog_categories(self, profile: str = _WIKI_PROFILE) -> dict[str, Any]:
        """Category + group tree for the browse sidebar."""

        self._require_catalog(profile)
        status = self.catalog_status(profile)
        stats = await asyncio.to_thread(self._store_for(profile).catalog_categories_stats)
        categories: dict[str, dict[str, Any]] = {}
        for row in stats:
            category = str(row["category"])
            entry = categories.get(category)
            if entry is None:
                entry = {
                    "category": category,
                    "label": category_label(category),
                    "tag_count": 0,
                    "groups": [],
                }
                categories[category] = entry
            entry["tag_count"] += int(row["tag_count"])
            entry["groups"].append(
                {
                    "key": str(row["group_key"]),
                    "label": group_label(str(row["group_key"])),
                    "tag_count": int(row["tag_count"]),
                }
            )
        ordered = sorted(
            categories.values(),
            key=lambda entry: (group_sort_key(entry["category"] if entry["category"] in GROUP_ORDER
                                              else "other_general"), entry["category"]),
        )
        for entry in ordered:
            entry["groups"].sort(key=lambda group: (-group["tag_count"], group["key"]))
        return {
            "profile": profile,
            "built": True,
            "generated_at": status["generated_at"],
            "taxonomy_version": status["taxonomy_version"] or 0,
            "min_post_count": status["min_post_count"] or 0,
            "tag_count": status["tag_count"],
            "relation_count": status["relation_count"],
            "categories": ordered,
        }

    async def catalog_browse(
        self,
        *,
        profile: str = _WIKI_PROFILE,
        category: str | None = None,
        group: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = CATALOG_DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """Browse/search the catalog: directory pages or fuzzy ranked search.

        Without ``q`` this is a plain directory page (post_count desc, name
        asc, SQL pagination). With ``q`` the store provides the substring
        candidate set and this method ranks it: exact canonical name, then
        the canonical tag behind an exact alias, then prefix matches, then
        token-prefix / contained matches — always post_count then name inside
        a tier, and always restricted to catalog (high-frequency) tags.
        """

        store = self._require_catalog(profile)
        clean_category = (category or "").strip() or None
        clean_group = (group or "").strip() or None
        clean_q = normalize_title(q or "")
        limit = max(1, min(int(limit), CATALOG_MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        if not clean_q:
            total, rows = await asyncio.to_thread(
                store.catalog_browse,
                category=clean_category,
                group=clean_group,
                q=None,
                offset=offset,
                limit=limit,
            )
            items = [self._catalog_item(profile, row) for row in rows]
            return {
                "profile": profile,
                "category": clean_category,
                "group": clean_group,
                "q": None,
                "total": total,
                "offset": offset,
                "limit": limit,
                "items": items,
            }
        result = await asyncio.to_thread(
            self._catalog_search_sync,
            profile,
            clean_q,
            clean_category,
            clean_group,
            offset,
            limit,
        )
        result.update(
            {
                "profile": profile,
                "category": clean_category,
                "group": clean_group,
                "q": clean_q,
            }
        )
        return result

    def _catalog_search_sync(
        self,
        profile: str,
        q: str,
        category: str | None,
        group: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """Rank substring candidates for one fuzzy catalog query.

        Runs in a worker thread (called from :meth:`catalog_browse`): the
        candidate fetch is one indexed LIKE query, ranking is pure Python.
        """

        store = self._store_for(profile)
        candidate_cap = CATALOG_SEARCH_CANDIDATE_CAP
        # Candidate set: the full query as one substring, UNION the same
        # browse per query token. The full-string LIKE alone would miss a
        # name like "blue_dragon" for "drag blue" (no single name contains
        # the whole phrase); token queries catch those, ranking below does
        # the ordering. Merged by name, both endpoints already filtered.
        candidates: dict[str, dict[str, Any]] = {}
        _total, rows = store.catalog_browse(
            category=category, group=group, q=q, offset=0, limit=candidate_cap
        )
        for row in rows:
            candidates[str(row["name"])] = row
        tokens = [token for token in split_tokens(q) if len(token) >= 2]
        if len(tokens) > 1 and len(candidates) < candidate_cap:
            for token in tokens:
                _token_total, token_rows = store.catalog_browse(
                    category=category, group=group, q=token, offset=0, limit=candidate_cap
                )
                for row in token_rows:
                    candidates.setdefault(str(row["name"]), row)

        # Exact alias: the canonical tag behind the query may be absent from
        # the substring candidate list (different spelling), so resolve it
        # through the shared tag database and merge it in. Only a query that
        # resolved THROUGH an alias (alias_of set) counts as an alias match;
        # a canonical query is tier 0 via the ordinary loop below.
        alias_info = self._tag_info(profile, q, required=False)
        alias_canonical = ""
        alias_display = ""
        if alias_info is not None and alias_info.get("alias_of"):
            alias_canonical = normalize_title(str(alias_info["name"]))
            alias_display = str(alias_info["alias_of"])

        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for name, row in candidates.items():
            if alias_canonical and name == alias_canonical:
                tier = 1  # exact alias
                item = self._catalog_item(profile, row, alias_of=alias_display)
            else:
                tier = _catalog_match_tier(q, tokens, name)
                item = self._catalog_item(profile, row)
            item["match"] = CATALOG_MATCH_TIERS[tier]
            ranked.append((tier, name, item))
        if alias_canonical and alias_canonical not in candidates:
            alias_row = store.catalog_get_tag(alias_canonical)
            if alias_row is not None and _catalog_row_matches_filters(alias_row, category, group):
                item = self._catalog_item(profile, alias_row, alias_of=alias_display)
                item["match"] = "alias"
                ranked.append((1, alias_canonical, item))
        ranked.sort(key=lambda entry: (entry[0], -entry[2]["post_count"], entry[1]))
        total = len(ranked)
        items = [entry[2] for entry in ranked[offset : offset + limit]]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
        }

    async def catalog_tag_detail(self, title: str, *, profile: str = _WIKI_PROFILE) -> dict[str, Any]:
        """One catalog tag with its wiki page, summary and grouped relations.

        Relations come from the catalog tables, so every returned name is
        itself a catalog (post_count >= threshold) tag and clickable in the
        UI. Legacy ``/lookup`` and ``/page`` stay untouched.
        """

        store = self._require_catalog(profile)
        row = await asyncio.to_thread(store.catalog_get_tag, title)
        if row is None:
            raise TagWikiError(
                f"标签目录中不存在：{title}",
                code=ERROR_WIKI_CATALOG_TAG_NOT_FOUND,
                status_code=404,
            )
        page, relation_groups = await asyncio.to_thread(
            self._catalog_detail_sync, store, row, profile
        )
        return {
            "tag": self._catalog_item(profile, row),
            "page": _page_public(page) if page is not None else None,
            "implications": relation_groups["implications"],
            "wiki_links": relation_groups["wiki_links"],
            "cooccurrences": relation_groups["cooccurrences"],
        }

    def _catalog_detail_sync(
        self, store: WikiStore, row: Mapping[str, Any], profile: str
    ) -> tuple[dict[str, Any] | None, dict[str, list[dict[str, Any]]]]:
        """Blocking half of the catalog detail view, run in a worker thread.

        The wiki page fetch, the relation query and the per-relation
        tag-database/translation lookups are all SQLite or first-load work,
        so they share one thread hop instead of stalling the event loop.
        """

        name = str(row["name"])
        page = store.get_page(name) if row["has_wiki"] else None
        relation_rows = store.catalog_relations_for(name)
        groups: dict[str, list[dict[str, Any]]] = {
            "implications": [],
            "wiki_links": [],
            "cooccurrences": [],
        }
        for relation in relation_rows:
            relation_type = str(relation["relation_type"])
            bucket_key = {"implication": "implications", "wiki_link": "wiki_links"}.get(
                relation_type, "cooccurrences"
            )
            other = (
                relation["related_name"]
                if relation["direction"] == "forward"
                else relation["tag_name"]
            )
            info = self._tag_info(profile, other, required=False)
            groups[bucket_key].append(
                {
                    "name": other,
                    "relation_type": relation_type,
                    "direction": str(relation["direction"]),
                    "score": float(relation["score"] or 0.0),
                    "tag": self._ref_from_info(profile, info) if info is not None else None,
                }
            )
        for relation_bucket in groups.values():
            relation_bucket.sort(key=lambda item: (-item["score"], item["name"]))
            del relation_bucket[_CATALOG_MAX_RELATIONS_PER_BUCKET:]
        return page, groups

    # -- translate ----------------------------------------------------------

    async def start_translate(self, request: TranslateRequest) -> dict[str, Any]:
        self._require_not_frozen()
        if self._translate_task is not None and not self._translate_task.done():
            raise TagWikiError("已有一次翻译任务在进行中", code=ERROR_WIKI_BUSY, status_code=409)
        store = self._store_for(request.profile)
        self._require_data(request.profile)
        provider_id, provider = self._resolve_provider(request.provider_id)
        titles = self._translate_scope(request)
        # Stop filtering as soon as one run's worth of pages is found; a
        # large scope must not cost a full-scan per start.
        pending = store.missing_summary_titles(titles, limit=request.max_pages)
        self._translate_state.update(
            state="running",
            done=0,
            failed=0,
            total=len(pending),
            provider_id=provider_id,
            model=request.model or str(getattr(provider, "model", "")),
            message="",
            started_at=_now(),
            updated_at=_now(),
            error=None,
            profile=request.profile,
        )
        if not pending:
            self._translate_state.update(state="idle", message="范围内页面均已有中文摘要")
            return self.translate_progress()
        self._translate_task = asyncio.create_task(
            self._run_translate(provider, provider_id, pending, request.model, request.profile, request.concurrency)
        )
        return self.translate_progress()

    def _translate_scope(self, request: TranslateRequest) -> list[str]:
        """Resolve the requested scope into concrete page titles."""

        store = self._store_for(request.profile)
        page_titles = set(store.iter_page_titles())
        if request.scope == "all":
            names = sorted(page_titles)
        else:
            try:
                if request.scope == "popular":
                    infos = self.tag_database.top_tags(
                        request.profile, min_post_count=request.min_post_count
                    )
                    names = [str(info["name"]) for info in infos]
                else:  # model_vocab
                    vocab = list(self._vocab_provider()) if self._vocab_provider is not None else []
                    names = []
                    for raw in vocab:
                        info = self._tag_info(request.profile, str(raw), required=False)
                        if info is not None:
                            names.append(str(info["name"]))
            except TagDatabaseError as exc:
                raise TagWikiError(
                    f"标签库未就绪：{exc}", code=ERROR_WIKI_TAG_DB_UNAVAILABLE, status_code=409
                ) from exc
        # Canonical names only, deduped, restricted to pages we actually have
        # and to pages that carry summarizable prose (not link-list bodies).
        return [
            name
            for name in dict.fromkeys(names)
            if name in page_titles and not self._is_excluded_category(name, request.profile)
        ]

    def _is_excluded_category(self, title: str, profile: str = _WIKI_PROFILE) -> bool:
        """Whether one page's tag category is excluded from search/translate."""

        info = self._tag_info(profile, title, required=False)
        return info is not None and str(info["category"]) in EXCLUDED_SEARCH_CATEGORIES

    async def _run_translate(
        self, provider: Any, provider_id: str, titles: list[str], model: str | None, profile: str, concurrency: int = 1
    ) -> None:
        def on_progress(done: int, failed: int) -> None:
            self._translate_state.update(done=done, failed=failed, updated_at=_now())

        try:
            result = await translate_pages(
                self._store_for(profile),
                provider,
                titles,
                model=model,
                provider_id=provider_id,
                on_progress=on_progress,
                concurrency=concurrency,
            )
            self._translate_state.update(
                state="idle",
                message=f"翻译完成：成功 {result['done']}，失败 {result['failed']}",
            )
        except asyncio.CancelledError:
            self._translate_state.update(state="idle", message="翻译已取消")
            raise
        except Exception as exc:  # noqa: BLE001 - every failure lands in status
            logger.exception("tag wiki translate job failed")
            self._translate_state.update(state="error", message="翻译任务失败", error=str(exc))

    def translate_progress(self) -> dict[str, Any]:
        return dict(self._translate_state)

    # -- public job handles ---------------------------------------------------

    def build_task(self) -> asyncio.Task[None] | None:
        """The background build task, or ``None`` when nothing is running."""
        return self._build_task

    def translate_task(self) -> asyncio.Task[None] | None:
        """The background translate task, or ``None`` when nothing is running."""
        return self._translate_task

    async def wait_build(self) -> dict[str, Any]:
        """Wait until the current build settles and return its final status.

        Public counterpart of the private ``_build_task`` for ops tooling
        (``scripts/build_tag_wiki.py``): joins the background task without
        propagating its cancellation or failure — the outcome is read from
        the returned status document. Returns immediately when no build is
        running.
        """
        task = self._build_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return dict(self._build_state)

    async def wait_translate(self) -> dict[str, Any]:
        """Wait until the current translate job settles (see :meth:`wait_build`)."""
        task = self._translate_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        return dict(self._translate_state)

    # -- shared helpers -----------------------------------------------------

    def _require_data(self, profile: str = _WIKI_PROFILE) -> None:
        if not self._store_for(profile).has_data():
            raise TagWikiError(
                "本地 Wiki 还没有数据：请先在构建面板下载并构建",
                code=ERROR_WIKI_NOT_BUILT,
                status_code=409,
            )

    def _tag_info(self, profile: str, name: str, *, required: bool) -> TagInfo | None:
        """Resolve one tag via the shared tag database (alias-aware)."""

        try:
            self.tag_database.ensure_loaded(profile)
            return self.tag_database.lookup(profile, name)
        except TagDatabaseError as exc:
            if required:
                raise TagWikiError(
                    f"标签库未就绪：{exc}", code=ERROR_WIKI_TAG_DB_UNAVAILABLE, status_code=409
                ) from exc
            return None

    def _ref_from_info(self, profile: str, info: TagInfo) -> TagRef:
        return {
            "name": str(info["name"]),
            "category": str(info["category"]),
            "post_count": info["post_count"],
            "alias_of": info["alias_of"],
            "translation": self.translations.translate(profile, str(info["name"])),
        }

    def _resolve_provider(self, explicit_provider_id: str | None) -> tuple[str, Any]:
        """Resolve the online provider, or raise the 409 setup state."""

        provider_id = (explicit_provider_id or "").strip() or self._first_provider_id()
        if not provider_id or self._provider_factory is None:
            raise TagWikiError(
                "没有可用的在线模型：请先在「在线模型」中添加并启用一个 Provider",
                code=ERROR_WIKI_ASK_UNAVAILABLE,
                status_code=409,
            )
        try:
            provider = self._provider_factory(provider_id)
        except Exception as exc:  # noqa: BLE001 - provider errors are sanitized
            raise TagWikiError(
                f"在线模型不可用：{exc}", code=ERROR_WIKI_ASK_UNAVAILABLE, status_code=409
            ) from exc
        return provider_id, provider

    def _first_provider_id(self) -> str:
        if self._provider_ids is None:
            return ""
        try:
            candidates = list(self._provider_ids())
        except Exception:  # noqa: BLE001 - a broken registry must not 500 here
            return ""
        return str(candidates[0]) if candidates else ""

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel background jobs and release the stores."""

        for task in (self._build_task, self._translate_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._build_task, self._translate_task):
            if task is not None and not task.done():
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        for store in self._stores.values():
            store.close()


__all__ = [
    "TagWikiError",
    "TagWikiService",
]
