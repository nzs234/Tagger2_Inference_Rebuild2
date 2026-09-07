"""Catalog feature tests: store migration, taxonomy, build CLI, service, API.

The tag catalog (booru-style read-only tag directory) is written only by
``scripts/build_tag_wiki_catalog.py``; these tests exercise every layer
offline with an in-memory/temp SQLite store and a duck-typed tag database.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tagger2.tag_manager.tag_db import TagDatabaseError
from tagger2.tag_wiki.api import create_tag_wiki_router
from tagger2.tag_wiki.contracts import ERROR_WIKI_CATALOG_MISSING
from tagger2.tag_wiki.service import TagWikiError, TagWikiService
from tagger2.tag_wiki.taxonomy import (
    TAXONOMY_VERSION,
    category_label,
    group_for_tag,
    group_label,
    group_sort_key,
    split_tokens,
)
from tagger2.tag_wiki.wiki_store import WikiStore

ROOT = Path(__file__).resolve().parents[2]
CATALOG_SCRIPT = ROOT / "scripts" / "build_tag_wiki_catalog.py"


def _load_script(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- shared fakes ------------------------------------------------------------


def _info(name: str, *, category: str = "general", post_count: int = 100) -> dict[str, Any]:
    return {"name": name, "category": category, "post_count": post_count, "alias_of": None}


class FakeTagDatabase:
    """Duck-typed TagDatabase covering the catalog's read surface."""

    def __init__(
        self,
        tags: dict[str, dict[str, Any]],
        *,
        aliases: dict[str, str] | None = None,
        implications: dict[str, list[str]] | None = None,
        fail: bool = False,
        per_profile: dict[str, tuple[dict[str, dict[str, Any]], dict[str, str]]] | None = None,
    ) -> None:
        self._tags = tags
        self._aliases = aliases or {}
        self._implications = implications or {}
        self._fail = fail
        self._per_profile = per_profile or {}

    def _vocab_for(self, profile: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        if profile in self._per_profile:
            return self._per_profile[profile]
        return self._tags, self._aliases

    def ensure_loaded(self, profile: str) -> None:
        if self._fail:
            raise TagDatabaseError("no classification snapshot available")

    def lookup(self, profile: str, tag: str, *, resolve_alias: bool = True) -> dict[str, Any] | None:
        tags, aliases = self._vocab_for(profile)
        key = tag.casefold()
        if resolve_alias:
            canonical = aliases.get(key)
            if canonical is not None:
                resolved = tags.get(canonical)
                if resolved is None:
                    return None
                return {**resolved, "alias_of": key}
        return tags.get(key)

    def top_tags(
        self, profile: str, *, min_post_count: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        tags, _aliases = self._vocab_for(profile)
        infos = [info for info in tags.values() if (info["post_count"] or 0) >= min_post_count]
        infos.sort(key=lambda info: (-(info["post_count"] or 0), info["name"]))
        return infos[:limit] if limit is not None else infos

    def implications_of(
        self, profile: str, tag: str, *, reverse: bool = False
    ) -> list[dict[str, Any]]:
        tags, _aliases = self._vocab_for(profile)
        names = [] if reverse else self._implications.get(tag.casefold(), [])
        return [tags[name] for name in names if name in tags]


CATALOG_TAGS: dict[str, dict[str, Any]] = {
    "solo": _info("solo", post_count=2000),
    "wolf": _info("wolf", category="species", post_count=5000),
    "hug": _info("hug", post_count=500),
    "blue_eyes": _info("blue_eyes", post_count=800),
    "long_ears": _info("long_ears", post_count=600),
    "kiss": _info("kiss", post_count=300),
    "some_artist": _info("some_artist", category="artist", post_count=900),
    "blue_dragon": _info("blue_dragon", post_count=400),
    "rare": _info("rare", post_count=50),  # below threshold, never in catalog
}


class FakeTranslations:
    def translate(self, profile: str, tag: str) -> str | None:
        return {"hug": "拥抱", "kiss": "亲吻"}.get(tag)


def _page(store: WikiStore, title: str, links: list[str] | None = None) -> None:
    store.upsert_page(
        {
            "title": title,
            "display_title": title,
            "body_md": f"body of {title}",
            "sections": [{"heading": "", "text": f"text of {title}"}],
            "links": links or [],
        }
    )


def make_catalog_service(
    tmp_path: Path,
    *,
    tag_database: FakeTagDatabase | None = None,
    frozen: bool = False,
) -> TagWikiService:
    e621 = WikiStore(tmp_path / "tag_wiki.sqlite3")
    danbooru = WikiStore(tmp_path / "tag_wiki_danbooru.sqlite3")
    db = tag_database or FakeTagDatabase(CATALOG_TAGS, aliases={"smooch": "kiss"},
                                         implications={"hug": ["kiss", "rare"], "long_ears": ["rabbit"]})
    return TagWikiService(
        store=e621,
        danbooru_store=danbooru,
        tag_database=db,
        translations=FakeTranslations(),
        data_dir=tmp_path,
        frozen=frozen,
    )


def seed_e621_catalog(store: WikiStore) -> None:
    """Seed wiki pages + a catalog via the real build function."""

    _page(store, "hug", links=["kiss", "rare"])
    _page(store, "kiss")
    _page(store, "solo")
    _page(store, "rare")  # wiki page exists but tag is below the threshold
    _page(store, "missing_page")
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test")
    module.build_catalog(
        "e621",
        store=store,
        tag_database=FakeTagDatabase(CATALOG_TAGS, aliases={"smooch": "kiss"},
                                     implications={"hug": ["kiss", "rare"], "long_ears": ["rabbit"]}),
        min_post_count=100,
        now="2026-09-07T00:00:00+00:00",
    )


# -- store: schema migration -------------------------------------------------


def test_fresh_store_gets_v3_and_catalog_tables(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "wiki.sqlite3")
    with store.connection() as conn:
        version = int(conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"])
        chunk_columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == 3
    assert "embedding" not in chunk_columns
    assert "chunks_fts" not in tables
    assert store.catalog_built() is False
    assert store.catalog_tag_count() == 0
    assert store.catalog_relation_count() == 0
    store.close()


def test_v1_database_migrates_to_v3_without_losing_pages(tmp_path: Path) -> None:
    """A v1 file upgrades in steps: pages survive, catalog tables appear and
    the retired vector structures are dropped."""

    path = tmp_path / "wiki.sqlite3"
    store = WikiStore(path)
    _page(store, "hug", links=["kiss"])
    store.upsert_summary("hug", {"meaning": "拥抱"})
    # Simulate a real v1 database: drop the v2 tables, restore the retired
    # vector structures and rewind the marker.
    with store.connection() as conn:
        conn.execute("DROP TABLE catalog_relations")
        conn.execute("DROP TABLE catalog_tags")
        conn.execute("DROP TABLE catalog_meta")
        conn.execute("DROP TRIGGER IF EXISTS chunks_ai")
        conn.execute("DROP TRIGGER IF EXISTS chunks_ad")
        conn.execute("DROP TRIGGER IF EXISTS chunks_au")
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.execute("ALTER TABLE chunks ADD COLUMN embedding BLOB")
        conn.execute("INSERT INTO meta (key, value) VALUES ('embedding_dim', '1024')")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum, applied_at)"
            " VALUES (1, 'schema-v1', '2026-01-01')"
        )
    store.close()

    reopened = WikiStore(path)
    with reopened.connection() as conn:
        version = int(conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()["version"])
        remaining = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
        ).fetchone()[0]
        chunk_columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        meta_keys = {row[0] for row in conn.execute("SELECT key FROM meta")}
    assert version == 3
    assert remaining == 1  # the v1 marker row is preserved history
    assert "embedding" not in chunk_columns
    assert "chunks_fts" not in tables
    assert "embedding_dim" not in meta_keys
    assert reopened.catalog_built() is False
    page = reopened.get_page("hug")
    assert page is not None and page["summary"]["meaning"] == "拥抱"
    assert page["related_tags"] == ["kiss"]
    reopened.close()


def test_future_schema_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "wiki.sqlite3"
    store = WikiStore(path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, checksum, applied_at)"
            " VALUES (999, 'schema-v999', '2026-01-01')"
        )
    store.close()
    with pytest.raises(Exception, match="newer than supported"):
        WikiStore(path)


# -- store: catalog write/read -----------------------------------------------


def seeded_store(tmp_path: Path) -> WikiStore:
    store = WikiStore(tmp_path / "wiki.sqlite3")
    seed_e621_catalog(store)
    return store


def test_replace_catalog_writes_and_clears(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    assert store.catalog_built() is True
    assert store.catalog_tag_count() == 8  # rare (50 posts) is excluded
    names = {row["name"] for row in store.catalog_browse(limit=100)[1]}
    assert "rare" not in names
    assert "missing_page" not in names  # wiki page without tag-database entry
    tag = store.catalog_get_tag("HUG")
    assert tag == {
        "name": "hug", "category": "general", "group_key": "action_pose",
        "post_count": 500, "has_wiki": True, "alias_of": None,
    }
    assert store.catalog_get_tag("does_not_exist") is None
    meta = store.catalog_meta()
    assert meta["min_post_count"] == 100
    assert meta["taxonomy_version"] == TAXONOMY_VERSION
    assert meta["tag_count"] == 8
    store.close()


def test_replace_catalog_is_atomic_rebuild(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    first = store.catalog_tag_count()
    # A second generation with fewer tags must replace, not append.
    store.replace_catalog(
        [{"name": "hug", "category": "general", "group_key": "action_pose", "post_count": 500}],
        [],
        {"tag_count": "1"},
    )
    assert store.catalog_tag_count() == 1
    assert store.catalog_meta()["tag_count"] == 1
    assert first == 8
    store.close()


def test_catalog_relations_both_directions(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    # hug implies kiss (catalog member) — hug->rare was dropped (below threshold).
    rows = store.catalog_relations_for("hug")
    assert {row["related_name"] for row in rows if row["direction"] == "forward"} == {"kiss"}
    types = {row["relation_type"] for row in rows}
    assert types == {"implication", "wiki_link"}
    # Reverse view from kiss: hug --implication--> kiss.
    reverse = [row for row in store.catalog_relations_for("kiss") if row["direction"] == "reverse"]
    assert {row["tag_name"] for row in reverse} == {"hug"}
    # wiki_link came from the hug page's [[kiss]] link (rare link dropped).
    links = {row["related_name"] for row in rows if row["relation_type"] == "wiki_link"}
    assert "rare" not in links
    store.close()


def test_catalog_browse_filters_and_pagination(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    # Category filter.
    total, rows = store.catalog_browse(category="species")
    assert total == 1 and rows[0]["name"] == "wolf"
    # Group filter: body_part covers long_ears and blue_eyes (eye anatomy).
    total, rows = store.catalog_browse(group="body_part")
    assert total == 2
    assert {row["name"] for row in rows} == {"long_ears", "blue_eyes"}
    # Substring query; underscores in the query are literal, not wildcards.
    total, rows = store.catalog_browse(q="blue")
    assert total == 2
    assert [row["name"] for row in rows] == ["blue_eyes", "blue_dragon"]  # post_count desc
    # Escaping: '%' and '_' never act as wildcards.
    total, _rows = store.catalog_browse(q="%")
    assert total == 0
    total, _rows = store.catalog_browse(q="blue_eyes")
    assert total == 1
    # Pagination is stable (post_count desc, name asc).
    total, page_one = store.catalog_browse(offset=0, limit=3)
    _total, page_two = store.catalog_browse(offset=3, limit=3)
    assert total == 8
    assert [row["name"] for row in page_one + page_two] == [
        row["name"] for row in store.catalog_browse(limit=100)[1][:6]
    ]
    store.close()


def test_catalog_categories_stats(tmp_path: Path) -> None:
    store = seeded_store(tmp_path)
    stats = store.catalog_categories_stats()
    species = [row for row in stats if row["category"] == "species"]
    assert species and species[0]["tag_count"] == 1 and species[0]["wiki_count"] == 0
    general = [row for row in stats if row["category"] == "general"]
    assert sum(row["tag_count"] for row in general) == 6
    store.close()


# -- taxonomy ----------------------------------------------------------------


def test_split_tokens_normalization() -> None:
    assert split_tokens("Long_Ears") == ["long", "ears"]
    assert split_tokens("blue-eyes!") == ["blue", "eyes"]
    assert split_tokens("a__b") == ["a", "b"]
    assert split_tokens("") == []


def test_group_for_tag_rules_are_deterministic() -> None:
    # Official categories map onto their stable groups verbatim.
    assert group_for_tag("e621", "species", "wolf") == "species"
    assert group_for_tag("e621", "artist", "some_dude") == "artist"
    assert group_for_tag("danbooru", "character", "hatsune_miku") == "character"
    assert group_for_tag("danbooru", "copyright", "touhou") == "copyright"
    assert group_for_tag("e621", "meta", "absurd_res") == "meta"
    # Explicit overrides win over the token rules.
    assert group_for_tag("e621", "general", "solo") == "action_pose"
    assert group_for_tag("e621", "general", "long_hair") == "appearance_body"
    # Token rules in precedence order: sexual beats body_part.
    assert group_for_tag("e621", "general", "erect_nipples") == "sexual"
    assert group_for_tag("e621", "general", "long_ears") == "body_part"
    assert group_for_tag("e621", "general", "bikini") == "clothing"
    assert group_for_tag("e621", "general", "muscular_male") == "appearance_body"
    assert group_for_tag("e621", "general", "sitting_on_lap") == "action_pose"
    # Unknown general tags fall back to other_general; unknown categories too.
    assert group_for_tag("e621", "general", "digital_media_(artwork)") == "other_general"
    assert group_for_tag("e621", "weird_category", "wolf") == "other_general"
    # Same input, same output.
    assert group_for_tag("e621", "general", "bikini") == group_for_tag("e621", "general", "bikini")


def test_taxonomy_labels_and_ordering() -> None:
    assert group_label("sexual") == "性内容"
    assert group_label("unknown_group") == "unknown_group"
    assert category_label("species") == "物种"
    assert group_sort_key("appearance_body") < group_sort_key("invalid")


# -- build script ------------------------------------------------------------


def test_build_catalog_filters_low_frequency_and_writes_relations(tmp_path: Path) -> None:
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test2")
    store = WikiStore(tmp_path / "wiki.sqlite3")
    _page(store, "hug", links=["kiss", "rare"])
    _page(store, "kiss")
    db = FakeTagDatabase(CATALOG_TAGS, implications={"hug": ["kiss", "rare"], "long_ears": ["rabbit"]})

    stats = module.build_catalog("e621", store=store, tag_database=db, min_post_count=100)

    assert stats["tag_count"] == 8
    assert "rare" not in {t["name"] for t in store.catalog_browse(limit=100)[1]}
    assert stats["category_counts"]["species"] == 1
    assert stats["group_counts"]["body_part"] == 2  # long_ears + blue_eyes
    # Only one implication survives: hug->kiss (hug->rare and
    # long_ears->rabbit point outside the catalog and are dropped).
    assert stats["relation_type_counts"]["implication"] == 1
    relations = store.catalog_relations_for("hug")
    forward = {r["related_name"] for r in relations if r["direction"] == "forward"}
    assert "rare" not in forward
    assert stats["dropped_relation_endpoints"] == 2  # rare implication + rare wiki link
    assert stats["with_wiki_page"] == 2  # hug, kiss (no solo page seeded here)
    store.close()


def test_build_catalog_is_idempotent(tmp_path: Path) -> None:
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test3")
    store = WikiStore(tmp_path / "wiki.sqlite3")
    db = FakeTagDatabase(CATALOG_TAGS)
    module.build_catalog("e621", store=store, tag_database=db, min_post_count=100)
    _page(store, "late_page")  # wiki pages added later do not corrupt the catalog
    stats = module.build_catalog("e621", store=store, tag_database=db, min_post_count=100)
    assert stats["tag_count"] == store.catalog_tag_count() == 8
    store.close()


def test_build_catalog_dry_run_writes_nothing(tmp_path: Path) -> None:
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test4")
    store = WikiStore(tmp_path / "wiki.sqlite3")
    db = FakeTagDatabase(CATALOG_TAGS)
    stats = module.build_catalog(
        "e621", store=store, tag_database=db, min_post_count=100, dry_run=True
    )
    assert stats["tag_count"] == 8
    assert store.catalog_built() is False
    store.close()


def test_build_catalog_missing_snapshot_surfaces_error(tmp_path: Path) -> None:
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test5")
    store = WikiStore(tmp_path / "wiki.sqlite3")
    with pytest.raises(TagDatabaseError):
        module.build_catalog("e621", store=store, tag_database=FakeTagDatabase({}, fail=True))
    store.close()


def test_default_store_for_profile_paths(tmp_path: Path) -> None:
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test6")
    e621 = module.default_store_for_profile("e621", data_dir=tmp_path)
    assert e621.db_path == tmp_path / "tag_wiki" / "tag_wiki.sqlite3"
    danbooru = module.default_store_for_profile("danbooru", data_dir=tmp_path)
    assert danbooru.db_path == tmp_path / "tag_wiki" / "tag_wiki_danbooru.sqlite3"
    with pytest.raises(ValueError):
        module.default_store_for_profile("gelbooru", data_dir=tmp_path)
    e621.close()
    danbooru.close()


# -- service -----------------------------------------------------------------


async def test_service_catalog_requires_built_catalog(tmp_path: Path) -> None:
    service = make_catalog_service(tmp_path)
    with pytest.raises(TagWikiError) as excinfo:
        await service.catalog_categories("e621")
    assert excinfo.value.code == ERROR_WIKI_CATALOG_MISSING
    assert excinfo.value.status_code == 409
    await service.aclose()


async def test_service_catalog_categories_tree(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_e621_catalog(store)
    service = make_catalog_service(tmp_path)
    result = await service.catalog_categories("e621")
    assert result["built"] is True
    assert result["tag_count"] == 8
    assert result["min_post_count"] == 100
    assert result["taxonomy_version"] == TAXONOMY_VERSION
    by_category = {entry["category"]: entry for entry in result["categories"]}
    assert by_category["species"]["label"] == "物种"
    assert by_category["species"]["groups"][0]["key"] == "species"
    assert by_category["general"]["groups"][0]["label"]  # Chinese group labels present
    await service.aclose()


async def test_service_catalog_browse_directory_pagination(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_e621_catalog(store)
    service = make_catalog_service(tmp_path)
    page = await service.catalog_browse(profile="e621", category="general", offset=0, limit=3)
    assert page["total"] == 6
    assert len(page["items"]) == 3
    assert page["items"][0]["name"] == "solo"  # 2000 posts first
    assert page["items"][0]["group_label"] == group_label("action_pose")
    assert page["items"][0]["translation"] is None or isinstance(page["items"][0]["translation"], str)
    kiss_page = await service.catalog_browse(profile="e621", q="kiss")
    assert kiss_page["items"][0]["translation"] == "亲吻"
    await service.aclose()


async def test_service_catalog_search_ranking(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_e621_catalog(store)
    service = make_catalog_service(tmp_path)

    # Exact canonical first.
    result = await service.catalog_browse(profile="e621", q="solo")
    assert result["items"][0]["name"] == "solo"
    assert result["items"][0]["match"] == "exact"

    # Exact alias resolves to the canonical catalog tag and marks the alias.
    result = await service.catalog_browse(profile="e621", q="smooch")
    assert result["items"][0]["name"] == "kiss"
    assert result["items"][0]["match"] == "alias"
    assert result["items"][0]["alias_of"] == "smooch"

    # Prefix beats contained regardless of post_count: "huggers" style case —
    # query "ki" (prefix of kiss, contained in nothing else here).
    result = await service.catalog_browse(profile="e621", q="ki")
    assert result["items"][0]["name"] == "kiss"
    assert result["items"][0]["match"] == "prefix"

    # Token-substring beats pure contained: query "rag eyes" matches
    # blue_eyes by its exact "eyes" token (tier "token") and blue_dragon only
    # by "rag" inside "dragon" (tier "contained"), so the token hit ranks
    # first even though blue_dragon has more posts.
    result = await service.catalog_browse(profile="e621", q="rag eyes")
    names = [item["name"] for item in result["items"]]
    assert names.index("blue_eyes") < names.index("blue_dragon")
    matches = {item["name"]: item["match"] for item in result["items"]}
    assert matches["blue_eyes"] == "token"
    assert matches["blue_dragon"] == "contained"

    # A single token that only prefixes a name token still matches.
    result = await service.catalog_browse(profile="e621", q="drag")
    names = [item["name"] for item in result["items"]]
    assert "blue_dragon" in names
    assert {item["name"]: item["match"] for item in result["items"]}["blue_dragon"] == "token"

    # The below-threshold tag never appears, even on an exact query.
    result = await service.catalog_browse(profile="e621", q="rare")
    assert all(item["name"] != "rare" for item in result["items"])
    await service.aclose()


async def test_service_catalog_detail_groups_relations(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_e621_catalog(store)
    service = make_catalog_service(tmp_path)
    detail = await service.catalog_tag_detail("hug", profile="e621")
    assert detail["tag"]["name"] == "hug"
    assert detail["tag"]["has_wiki"] is True
    assert detail["tag"]["translation"] == "拥抱"
    assert detail["page"] is not None
    assert detail["page"]["title"] == "hug"
    assert detail["page"]["summary"] is None
    assert [rel["name"] for rel in detail["implications"]] == ["kiss"]
    assert detail["implications"][0]["direction"] == "forward"
    assert detail["implications"][0]["tag"]["translation"] == "亲吻"
    assert [rel["name"] for rel in detail["wiki_links"]] == ["kiss"]
    assert detail["cooccurrences"] == []
    # Reverse implication view from kiss.
    detail = await service.catalog_tag_detail("kiss", profile="e621")
    reverse = [rel for rel in detail["implications"] if rel["direction"] == "reverse"]
    assert [rel["name"] for rel in reverse] == ["hug"]
    await service.aclose()


async def test_service_catalog_detail_404_and_wiki_less_tag(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_e621_catalog(store)
    service = make_catalog_service(tmp_path)
    with pytest.raises(TagWikiError) as excinfo:
        await service.catalog_tag_detail("unknown_tag", profile="e621")
    assert excinfo.value.code == "wiki_catalog_tag_not_found"
    assert excinfo.value.status_code == 404
    # wolf is in the catalog but has no wiki page: detail works, page=None.
    detail = await service.catalog_tag_detail("wolf", profile="e621")
    assert detail["page"] is None
    assert detail["tag"]["has_wiki"] is False
    await service.aclose()


async def test_service_catalog_detail_caps_relation_buckets(tmp_path: Path) -> None:
    """A hub tag's relation buckets are popularity-truncated, not unbounded."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    tags: dict[str, dict[str, Any]] = {"hub": _info("hub", post_count=10_000)}
    for idx in range(34):
        tags[f"link_{idx:02d}"] = _info(f"link_{idx:02d}", post_count=100 + idx)
    store.upsert_page(
        {
            "title": "hub",
            "display_title": "hub",
            "body_md": "hub body",
            "sections": [{"heading": "", "text": "hub body text"}],
            "links": [f"link_{idx:02d}" for idx in range(34)],
        }
    )
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test")
    module.build_catalog(
        "e621",
        store=store,
        tag_database=FakeTagDatabase(tags),
        min_post_count=100,
        now="2026-09-07T00:00:00+00:00",
    )
    service = make_catalog_service(tmp_path, tag_database=FakeTagDatabase(tags))
    detail = await service.catalog_tag_detail("hub", profile="e621")
    assert len(detail["wiki_links"]) == 30
    # Sorted by target popularity: the highest-post links survive the cap.
    assert detail["wiki_links"][0]["name"] == "link_33"
    assert detail["wiki_links"][-1]["name"] == "link_04"
    assert "link_00" not in [rel["name"] for rel in detail["wiki_links"]]
    await service.aclose()


async def test_service_catalog_reads_work_in_frozen_mode(tmp_path: Path) -> None:
    """The catalog is read-only: frozen packaged builds must keep serving it."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_e621_catalog(store)
    service = make_catalog_service(tmp_path, frozen=True)
    result = await service.catalog_browse(profile="e621", q="hug")
    assert result["items"][0]["name"] == "hug"
    detail = await service.catalog_tag_detail("hug", profile="e621")
    assert detail["tag"]["name"] == "hug"
    await service.aclose()


async def test_service_catalog_profile_isolation(tmp_path: Path) -> None:
    """e621 and danbooru catalogs never leak tags across profiles."""

    db = FakeTagDatabase(
        {},
        per_profile={
            "e621": (CATALOG_TAGS, {}),
            "danbooru": (
                {"twintails": _info("twintails", post_count=1500)},
                {},
            ),
        },
    )
    service = make_catalog_service(tmp_path, tag_database=db)
    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test7")
    module.build_catalog("e621", store=service._store_for("e621"), tag_database=db, min_post_count=100)
    module.build_catalog("danbooru", store=service._store_for("danbooru"), tag_database=db, min_post_count=100)

    e621_page = await service.catalog_browse(profile="e621")
    danbooru_page = await service.catalog_browse(profile="danbooru")
    assert {item["name"] for item in e621_page["items"]} == {
        "solo", "wolf", "some_artist", "blue_eyes", "long_ears", "hug", "blue_dragon", "kiss",
    }
    assert [item["name"] for item in danbooru_page["items"]] == ["twintails"]
    with pytest.raises(TagWikiError):
        await service.catalog_tag_detail("hug", profile="danbooru")
    await service.aclose()


# -- API contract ------------------------------------------------------------


def _make_api_app(tmp_path: Path) -> FastAPI:
    service = make_catalog_service(tmp_path)
    store = service._store_for("e621")
    seed_e621_catalog(store)
    app = FastAPI()
    app.include_router(create_tag_wiki_router(service))
    return app


def test_api_catalog_routes_contract(tmp_path: Path) -> None:
    app = _make_api_app(tmp_path)
    client = TestClient(app)

    categories = client.get("/api/v1/tag-wiki/catalog/categories").json()
    assert categories["built"] is True
    assert categories["profile"] == "e621"
    assert any(c["category"] == "species" for c in categories["categories"])

    browse = client.get(
        "/api/v1/tag-wiki/catalog/tags",
        params={"category": "general", "limit": 2, "offset": 0},
    ).json()
    assert browse["total"] == 6
    assert len(browse["items"]) == 2
    assert browse["limit"] == 2

    search = client.get("/api/v1/tag-wiki/catalog/tags", params={"q": "smooch"}).json()
    assert search["items"][0]["name"] == "kiss"
    assert search["items"][0]["match"] == "alias"

    detail = client.get("/api/v1/tag-wiki/catalog/tags/hug").json()
    assert detail["tag"]["name"] == "hug"
    assert detail["implications"][0]["name"] == "kiss"

    missing = client.get("/api/v1/tag-wiki/catalog/tags/definitely_missing")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "wiki_catalog_tag_not_found"


def test_api_catalog_profile_query_param_switches_store(tmp_path: Path) -> None:
    app = _make_api_app(tmp_path)
    client = TestClient(app)
    # danbooru has no catalog built -> stable 409, not e621 data leaking.
    response = client.get("/api/v1/tag-wiki/catalog/categories", params={"profile": "danbooru"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ERROR_WIKI_CATALOG_MISSING
    # The e621 default still answers after the failed danbooru call.
    ok = client.get("/api/v1/tag-wiki/catalog/tags", params={"q": "hug"})
    assert ok.status_code == 200
    assert ok.json()["items"][0]["name"] == "hug"


def test_api_catalog_route_order_before_page_route(tmp_path: Path) -> None:
    """/catalog/tags/{title} must win over any generic route registration."""

    app = _make_api_app(tmp_path)
    client = TestClient(app)
    # Behavioral check first: the catalog detail route answers with catalog data.
    detail = client.get("/api/v1/tag-wiki/catalog/tags/hug")
    assert detail.status_code == 200
    assert detail.json()["tag"]["name"] == "hug"
    # Registration order inside the router: catalog routes before /page.
    router = create_tag_wiki_router(make_catalog_service(tmp_path))
    router_paths = [route.path for route in router.routes]
    assert "/api/v1/tag-wiki/catalog/categories" in router_paths
    assert "/api/v1/tag-wiki/catalog/tags" in router_paths
    assert (
        router_paths.index("/api/v1/tag-wiki/catalog/tags/{title}")
        < router_paths.index("/api/v1/tag-wiki/page/{title}")
    )
    # A page title that looks like a catalog path stays unambiguous.
    response = client.get("/api/v1/tag-wiki/page/hug")
    assert response.status_code == 200
    assert response.json()["title"] == "hug"


def test_api_legacy_lookup_and_page_still_work_alongside_catalog(tmp_path: Path) -> None:
    app = _make_api_app(tmp_path)
    client = TestClient(app)
    lookup = client.get("/api/v1/tag-wiki/lookup", params={"tag": "hug"}).json()
    assert lookup["resolved"] is True
    assert lookup["page"]["related_tags"] == ["caress", "kiss"] or lookup["page"] is not None
    page = client.get("/api/v1/tag-wiki/page/kiss")
    assert page.status_code == 200


def test_store_rejects_underscore_wildcards_in_catalog_query(tmp_path: Path) -> None:
    """LIKE injection through tag names with wildcards must not widen results."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    store.replace_catalog(
        [
            {"name": "abXcd", "category": "general", "group_key": "other_general", "post_count": 500},
            {"name": "abcd", "category": "general", "group_key": "other_general", "post_count": 400},
        ]
    )
    # SQLite LIKE is case-insensitive; a literal '_' query must match nothing.
    total, rows = store.catalog_browse(q="ab_cd")
    assert total == 0 and rows == []
    total, rows = store.catalog_browse(q="ab%")
    assert total == 0 and rows == []
    # Lookup normalization casefolds, so a differently-cased query still hits.
    assert store.catalog_get_tag("abXcd")["name"] == "abxcd"
    store.close()


def test_store_direct_sqlite_catalog_schema(tmp_path: Path) -> None:
    """The raw schema carries the documented columns and indexes."""

    store = WikiStore(tmp_path / "wiki.sqlite3")
    with store.connection() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(catalog_tags)")}
        relations = {row[1] for row in conn.execute("PRAGMA table_info(catalog_relations)")}
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM pragma_index_list('catalog_relations')")
        }
    assert columns == {"name", "category", "group_key", "post_count", "has_wiki", "alias_of"}
    assert relations == {"tag_name", "related_name", "relation_type", "score", "source"}
    assert "idx_catalog_relations_related" in indexes
    store.close()


def test_sqlite3_row_factory_not_required_for_catalog_reads(tmp_path: Path) -> None:
    """catalog_browse works against a real file DB (not just :memory:)."""

    store = WikiStore(tmp_path / "file_db.sqlite3")
    store.replace_catalog(
        [{"name": "wolf", "category": "species", "group_key": "species", "post_count": 5000}]
    )
    store.close()
    reopened = WikiStore(tmp_path / "file_db.sqlite3")
    total, rows = reopened.catalog_browse(category="species")
    assert total == 1 and rows[0]["name"] == "wolf"
    reopened.close()


def test_group_label_of_every_written_group_exists(tmp_path: Path) -> None:
    """Every group_key the builder writes must have a display label."""

    module = _load_script(CATALOG_SCRIPT, "build_tag_wiki_catalog_test8")
    store = WikiStore(tmp_path / "wiki.sqlite3")
    db = FakeTagDatabase(CATALOG_TAGS)
    module.build_catalog("e621", store=store, tag_database=db, min_post_count=100)
    _total, rows = store.catalog_browse(limit=100)
    for row in rows:
        assert group_label(row["group_key"]) != row["group_key"]
    store.close()
