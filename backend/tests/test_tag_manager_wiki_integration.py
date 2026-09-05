"""Cross-module integration tests: tag manager and tag wiki on one app.

main.py wires ``TagWikiService`` to the *same* ``TagDatabase`` and
``TagTranslations`` instances the ``TagManagerService`` uses (Runtime lines
368-398). These tests pin that contract from both sides:

- a router-level app mounting both routers over shared services (offline:
  in-memory snapshot stubs, staged gzip dictionaries, fake provider, temp
  SQLite stores), covering dataset indexing, snapshot reloads (alias and
  implication updates), learned translations, profile switching, error codes
  and the response shapes the frontend types rely on;
- the real ``create_app`` Runtime, asserting the shared instances by identity
  and running the manager -> wiki visibility flow through HTTP.

Nothing here downloads models or touches the network, and no test modifies
service code: the ask-recommendation whitelist area is intentionally left to
test_tag_wiki_service.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tagger2.config import AppConfig, reset_settings_cache
from tagger2.main import create_app
from tagger2.security import PathAllowlist
from tagger2.tag_manager.api import create_tag_manager_router
from tagger2.tag_manager.service import TagManagerService
from tagger2.tag_manager.storage import TagManagerStore
from tagger2.tag_manager.tag_db import TagDatabase, TagDatabaseError
from tagger2.tag_manager.translations import TagTranslations, reset_translation_cache
from tagger2.tag_wiki.api import create_tag_wiki_router
from tagger2.tag_wiki.contracts import TranslateRequest
from tagger2.tag_wiki.service import TagWikiError, TagWikiService
from tagger2.tag_wiki.wiki_store import WikiStore
from tagger2.workflow.resources import CLASSIFY_RESOURCE_CATEGORY, WorkflowResourceCatalog


# -- shared offline fixtures -------------------------------------------------

E621_ROWS = {"solo": "单人", "wolf": "狼", "blue_eyes": "蓝瞳"}
DANBOORU_ROWS = {"1girl": "单人女性", "cat_ears": "兽耳"}


def _snapshot(profile: str, *, aliases: list[dict[str, str]], implications: list[dict[str, str]],
              tags: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format": "classify-snapshot-v1",
        "profile": profile,
        "source": {},
        "tags": tags,
        "aliases": aliases,
        "implications": implications,
    }


E621_SNAP_A = _snapshot(
    "e621",
    tags=[
        {"name": "solo", "category": "general", "post_count": 100},
        {"name": "hug", "category": "general", "post_count": 500},
        {"name": "wolf", "category": "general", "post_count": 300},
        {"name": "dog", "category": "species", "post_count": 800},
        {"name": "corgi", "category": "species", "post_count": 150},
    ],
    aliases=[{"antecedent_name": "1girl", "consequent_name": "solo"}],
    implications=[
        {"antecedent_name": "corgi", "consequent_name": "dog"},
        {"antecedent_name": "solo", "consequent_name": "hug"},
    ],
)

# A newer snapshot: the 1girl alias is gone, kitty->cat appears, and the
# corgi implication now points at puppy instead of dog.
E621_SNAP_B = _snapshot(
    "e621",
    tags=[
        {"name": "solo", "category": "general", "post_count": 100},
        {"name": "cat", "category": "general", "post_count": 700},
        {"name": "puppy", "category": "species", "post_count": 200},
        {"name": "corgi", "category": "species", "post_count": 150},
    ],
    aliases=[{"antecedent_name": "kitty", "consequent_name": "cat"}],
    implications=[{"antecedent_name": "corgi", "consequent_name": "puppy"}],
)

DANBOORU_SNAP = _snapshot(
    "danbooru",
    tags=[
        {"name": "cat_ears", "category": "general", "post_count": 300},
        {"name": "animal_ears", "category": "general", "post_count": 500},
        {"name": "touhou", "category": "character", "post_count": 900},
        {"name": "solo", "category": "general", "post_count": 100},
    ],
    aliases=[{"antecedent_name": "nekomimi", "consequent_name": "cat_ears"}],
    implications=[{"antecedent_name": "cat_ears", "consequent_name": "animal_ears"}],
)


class StubTagDatabase(TagDatabase):
    """A real TagDatabase whose snapshot files are in-memory documents.

    Snapshot loading (the only disk-touching step) is replaced, so the alias
    flattening, implication resolution and the process-level index cache all
    run exactly as in production.
    """

    def __init__(self, documents: dict[str, dict[str, Any]], resource_dir: Path) -> None:
        super().__init__(WorkflowResourceCatalog(resource_dir))
        self._documents = documents

    def _load_snapshot(self, profile: str, resource_id: str) -> dict[str, Any]:
        document = self._documents.get(resource_id)
        if document is None or document.get("profile") != profile:
            raise TagDatabaseError(f"unknown snapshot {resource_id!r} for profile {profile!r}")
        return document


class FakeProvider:
    """Offline provider double shared by both services."""

    model = "fake-model"

    def __init__(self, reply: str = "", *, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self, image: Any = None, prompt: str = "", *, model: str | None = None,
        system_prompt: str | None = None, **_: Any,
    ) -> str:
        self.calls.append({"prompt": prompt, "model": model, "system_prompt": system_prompt})
        if self.error is not None:
            raise self.error
        return self.reply


class FakeThumbnails:
    def ensure_thumbnail(self, source: Path, *, size: int, mtime: float) -> Path:
        return source.with_suffix(".thumb.jpg")


def _write_dictionary(directory: Path, profile: str, rows: dict[str, str]) -> None:
    import csv
    import gzip
    import io

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["tag", "zh"])
    for tag in sorted(rows):
        writer.writerow([tag, rows[tag]])
    directory.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        compressed.write(buffer.getvalue().encode("utf-8"))
    (directory / f"{profile}-zh.csv.gz").write_bytes(raw.getvalue())


def _write_manifest(directory: Path) -> None:
    (directory / "MANIFEST.json").write_text(
        json.dumps(
            {
                "format": "tag-translations-v1",
                "generated_at": "2026-09-02T00:00:00Z",
                "profiles": {
                    "danbooru": {"file": "danbooru-zh.csv.gz", "entries": len(DANBOORU_ROWS)},
                    "e621": {"file": "e621-zh.csv.gz", "entries": len(E621_ROWS)},
                },
                "sources": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def seed_page(store: WikiStore, title: str, *, text: str) -> None:
    store.upsert_page(
        {
            "title": title,
            "display_title": title,
            "body_md": f"h2. Usage\n{text} See [[kiss]].",
            "wiki_id": 1,
            "updated_at": "2026-09-01T00:00:00Z",
            "url": f"https://e621.net/wiki_pages/1-{title}",
            "sections": [{"heading": "Usage", "text": f"{text} See [[kiss]]."}],
            "links": ["kiss"],
        }
    )


def _make_image(directory: Path, name: str) -> None:
    Image.new("RGB", (8, 8)).save(directory / name)


@pytest.fixture()
def dual(tmp_path: Path):
    """One FastAPI app hosting both routers over shared offline services.

    Mirrors the Runtime wiring in main.py: the wiki service receives the very
    TagDatabase / TagTranslations instances the manager uses.
    """

    reset_translation_cache()
    dict_dir = tmp_path / "tag_translations"
    _write_dictionary(dict_dir, "e621", E621_ROWS)
    _write_dictionary(dict_dir, "danbooru", DANBOORU_ROWS)
    _write_manifest(dict_dir)

    tag_database = StubTagDatabase(
        {
            "e621-snap-a": E621_SNAP_A,
            "e621-snap-b": E621_SNAP_B,
            "danbooru-snap-a": DANBOORU_SNAP,
        },
        tmp_path / "resources",
    )
    tag_database.ensure_loaded("e621", resource_id="e621-snap-a")
    tag_database.ensure_loaded("danbooru", resource_id="danbooru-snap-a")
    translations = TagTranslations(dict_dir, tmp_path / "user-translations")
    provider = FakeProvider()

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")
    (dataset / "a.txt").write_text("1girl, hug\n", encoding="utf-8")
    _make_image(dataset, "b.png")
    (dataset / "b.txt").write_text("solo, wolf\n", encoding="utf-8")
    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="ds-root", kind="input", writable=True)

    manager = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=tag_database,
        translations=translations,
        provider_factory=lambda _pid: provider,
        provider_ids=lambda: ["fake-provider"],
    )
    e621_store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    danbooru_store = WikiStore(tmp_path / "tag_wiki_danbooru.sqlite3")
    # Pre-imported mirror pages: lookups resolve to pages, searches have prose.
    seed_page(e621_store, "solo", text="Only one character is present.")
    seed_page(e621_store, "hug", text="Use for hugging.")
    seed_page(danbooru_store, "cat_ears", text="Animal ears on a catgirl.")
    wiki = TagWikiService(
        store=e621_store,
        danbooru_store=danbooru_store,
        tag_database=tag_database,
        translations=translations,
        provider_factory=lambda _pid: provider,
        provider_ids=lambda: ["fake-provider"],
        vocab_provider=lambda: ["hug"],
        data_dir=tmp_path,
    )
    app = FastAPI()
    app.include_router(create_tag_manager_router(manager))
    app.include_router(create_tag_wiki_router(wiki))

    bundle = SimpleNamespace(
        client=TestClient(app),
        manager=manager,
        wiki=wiki,
        tag_database=tag_database,
        translations=translations,
        provider=provider,
        dataset=dataset,
        user_dir=tmp_path / "user-translations",
        dict_dir=dict_dir,
    )
    try:
        yield bundle
    finally:
        asyncio.run(wiki.aclose())
        reset_translation_cache()


def _wait_ready(client: TestClient, session_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Wait for the background index thread to flip the session to ready."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/v1/tag-manager/datasets/{session_id}").json()
        if body["status"] == "ready":
            return body
        if body["status"] == "error":
            raise AssertionError(f"dataset indexing failed: {body}")
        time.sleep(0.02)
    raise AssertionError("dataset indexing did not finish in time")


# -- shared wiring -----------------------------------------------------------


def test_both_services_share_one_tag_database_and_translations(dual) -> None:
    """The wiki must see manager-side tag data because it is the same object."""

    assert dual.wiki.tag_database is dual.manager.tag_database
    assert dual.wiki.translations is dual.manager.translations

    info = dual.client.get("/api/v1/tag-manager/tag-db/info").json()
    assert info["loaded"] == {"e621": True, "danbooru": True}
    # Frontend TagDbInfo shape: available/loaded/translations per profile.
    assert set(info) == {"available", "loaded", "translations"}
    assert set(info["translations"]["e621"]) >= {
        "entries", "loaded", "source", "updated", "user_entries",
    }
    assert info["translations"]["e621"]["entries"] == len(E621_ROWS)
    assert info["translations"]["danbooru"]["source"] == "danbooru-zh.csv.gz"


def test_dataset_indexing_categorizes_through_the_shared_tag_database(dual) -> None:
    """Sidecar tags are categorized by the shared index, aliases included."""

    created = dual.client.post(
        "/api/v1/tag-manager/datasets",
        json={"root_id": "ds-root", "relative_path": "", "profile": "e621", "name": "demo"},
    )
    assert created.status_code == 202
    session = created.json()
    # Frontend TagManagerSession required keys.
    assert {
        "id", "name", "root_id", "relative_path", "profile", "recursive",
        "status", "image_count", "created_at", "updated_at",
    } <= set(session)

    ready = _wait_ready(dual.client, session["id"])
    assert ready["image_count"] == 2

    images = dual.client.get(f"/api/v1/tag-manager/datasets/{session['id']}/images").json()
    assert set(images) == {"items", "total"} and images["total"] == 2
    by_name = {item["file_name"]: item for item in images["items"]}
    # "1girl" only exists as an alias antecedent; the category still resolves
    # through the shared database to its canonical target (solo, general).
    assert {t["tag"]: t["category"] for t in by_name["a.png"]["tags"]} == {
        "1girl": "general",
        "hug": "general",
    }
    assert {t["tag"]: t["translation"] for t in by_name["b.png"]["tags"]} == {
        "solo": "单人",
        "wolf": "狼",
    }


def test_snapshot_reload_via_tag_manager_is_visible_in_wiki(dual) -> None:
    """A manager-driven snapshot swap (aliases + implications) reaches the wiki."""

    lookup = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "1girl"})
    assert lookup.status_code == 200
    body = lookup.json()
    assert body["resolved"] is True
    assert body["tag"]["name"] == "solo"
    assert body["tag"]["alias_of"] == "1girl"
    # Frontend TagRef shape on every returned tag.
    assert set(body["tag"]) >= {"name", "category", "post_count", "alias_of", "translation"}
    assert [tag["name"] for tag in body["implications"]] == ["hug"]
    corgi = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "corgi"}).json()
    assert [tag["name"] for tag in corgi["implications"]] == ["dog"]

    # The tag manager reloads the shared index with the newer snapshot...
    reloaded = dual.client.get(
        "/api/v1/tag-manager/tag-db",
        params={"profile": "e621", "query": "ca", "resource_id": "e621-snap-b"},
    )
    assert reloaded.status_code == 200
    assert [item["name"] for item in reloaded.json()["items"]] == ["cat"]

    # ...and the wiki immediately resolves through the new tables.
    kitty = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "kitty"}).json()
    assert kitty["resolved"] is True and kitty["tag"]["name"] == "cat"
    gone = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "1girl"}).json()
    assert gone["resolved"] is False and gone["tag"] is None
    corgi = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "corgi"}).json()
    assert [tag["name"] for tag in corgi["implications"]] == ["puppy"]


def test_translation_learned_in_tag_manager_is_visible_in_wiki(dual) -> None:
    """On-demand translations persist and show up in wiki TagRefs at once."""

    dual.provider.reply = json.dumps({"hug": "拥抱"}, ensure_ascii=False)
    learned = dual.client.post(
        "/api/v1/tag-manager/translations/translate",
        json={"profile": "e621", "tags": ["hug", "hug", "   ", "blue_eyes"]},
    )
    assert learned.status_code == 200
    body = learned.json()
    # Duplicates and blank entries collapse; the dictionary hit skips the model.
    assert body["translations"] == {"blue_eyes": "蓝瞳", "hug": "拥抱"}
    assert body["translated_now"] == 1 and body["from_dictionary"] == 1
    assert body["provider_id"] == "fake-provider" and body["model"] == "fake-model"
    assert len(dual.provider.calls) == 1
    assert dual.provider.calls[0]["prompt"].count("hug") == 1

    # Persisted into the user dictionary for the manager itself...
    lookup = dual.client.post(
        "/api/v1/tag-manager/translations/lookup", json={"profile": "e621", "tags": ["Hug"]}
    )
    assert lookup.json()["translations"] == {"Hug": "拥抱"}
    assert "hug,拥抱" in (dual.user_dir / "e621-zh.csv").read_text(encoding="utf-8")

    # ...and immediately visible through the wiki router (shared instance).
    solo = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "1girl"}).json()
    assert solo["tag"]["translation"] == "单人"
    assert [tag["translation"] for tag in solo["implications"]] == ["拥抱"]

    seed_page(dual.wiki.store, "solo", text="Only one character is present.")
    seed_page(dual.wiki.store, "hug", text="Use for hugging.")
    search = dual.client.post(
        "/api/v1/tag-wiki/search", json={"query": "hugging", "top_k": 5}
    )
    assert search.status_code == 200
    suggested = {tag["name"]: tag["translation"] for tag in search.json()["suggested_tags"]}
    assert suggested.get("hug") == "拥抱"
    # Frontend ChunkHit shape.
    hit = search.json()["items"][0]
    assert set(hit) >= {"page_title", "heading", "text", "score", "matched_by", "summary", "tag"}

    # The manager's own image detail now carries the learned map as well.
    session_id = dual.client.post(
        "/api/v1/tag-manager/datasets",
        json={"root_id": "ds-root", "relative_path": "", "profile": "e621"},
    ).json()["id"]
    _wait_ready(dual.client, session_id)
    images = dual.client.get(f"/api/v1/tag-manager/datasets/{session_id}/images").json()
    image_id = int(images["items"][0]["id"])
    detail = dual.client.get(f"/api/v1/tag-manager/datasets/{session_id}/images/{image_id}").json()
    assert detail["translations"]["hug"] == "拥抱"


def test_profile_switching_keeps_dictionaries_and_stores_separate(dual) -> None:
    """e621 and danbooru resolve through independent dictionaries and mirrors."""

    lookup = dual.client.post(
        "/api/v1/tag-manager/translations/lookup",
        json={"profile": "danbooru", "tags": ["1girl", "solo"]},
    )
    assert lookup.json()["translations"] == {"1girl": "单人女性"}

    tag_db = dual.client.get(
        "/api/v1/tag-manager/tag-db", params={"profile": "danbooru", "query": "cat_e"}
    )
    assert tag_db.status_code == 200
    items = tag_db.json()["items"]
    assert [item["name"] for item in items] == ["cat_ears"]
    assert items[0]["translation"] == "兽耳"

    danbooru = dual.client.get(
        "/api/v1/tag-wiki/lookup", params={"tag": "nekomimi", "profile": "danbooru"}
    )
    body = danbooru.json()
    assert body["resolved"] is True
    assert body["tag"]["name"] == "cat_ears"
    assert body["tag"]["alias_of"] == "nekomimi"
    assert body["tag"]["translation"] == "兽耳"
    assert [tag["name"] for tag in body["implications"]] == ["animal_ears"]

    # The same tag name resolves differently per profile: e621 has the
    # 1girl alias, danbooru does not (and its mirror lacks the page).
    e621 = dual.client.get("/api/v1/tag-wiki/lookup", params={"tag": "1girl"}).json()
    assert e621["resolved"] is True and e621["tag"]["name"] == "solo"
    danbooru_1girl = dual.client.get(
        "/api/v1/tag-wiki/lookup", params={"tag": "1girl", "profile": "danbooru"}
    ).json()
    assert danbooru_1girl["resolved"] is False and danbooru_1girl["page"] is None

    status = dual.client.get("/api/v1/tag-wiki/status").json()
    assert set(status["profiles"]) == {"e621", "danbooru"}
    assert status["profiles"]["e621"]["database"]["pages"] == 2
    assert status["profiles"]["danbooru"]["database"]["pages"] == 1
    assert status["database"] == status["profiles"]["e621"]["database"]


# -- error codes and edge cases ----------------------------------------------


def _error_app(tmp_path: Path, *, fail_tag_db: bool, seed: bool = False) -> SimpleNamespace:
    """A minimal dual-router app for error-path probing.

    ``fail_tag_db=True`` wires a tag database that can never load a snapshot
    (the "initialization failure" state); ``seed`` pre-imports one wiki page.
    """

    class FailingTagDatabase(StubTagDatabase):
        def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
            raise TagDatabaseError("no classification snapshot available")

    tag_database: TagDatabase = (
        FailingTagDatabase({}, tmp_path / "resources-error")
        if fail_tag_db
        else StubTagDatabase({}, tmp_path / "resources-error")
    )
    translations = TagTranslations(tmp_path / "dicts", tmp_path / "user-translations")
    allowlist = PathAllowlist()
    allowlist.register(tmp_path, root_id="any-root", kind="input", writable=False)
    manager = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=tag_database,
        translations=translations,
    )
    wiki = TagWikiService(
        store=WikiStore(tmp_path / "tag_wiki_error.sqlite3"),
        tag_database=tag_database,
        translations=translations,
        data_dir=tmp_path,
    )
    if seed:
        seed_page(wiki.store, "solo", text="Only one character.")
    app = FastAPI()
    app.include_router(create_tag_manager_router(manager))
    app.include_router(create_tag_wiki_router(wiki))
    return SimpleNamespace(client=TestClient(app), wiki=wiki)


def test_error_codes_are_stable_across_both_routers(tmp_path: Path) -> None:
    """Setup-state errors use one code vocabulary on both routers."""

    failing = _error_app(tmp_path / "failing", fail_tag_db=True, seed=True)
    # Manager: a missing snapshot is a 409 setup state, not a 500.
    tag_db = failing.client.get("/api/v1/tag-manager/tag-db", params={"query": "solo"})
    assert tag_db.status_code == 409
    assert tag_db.json()["detail"]["code"] == "tag_db_unavailable"

    # Wiki with data but a broken tag database: the dedicated code surfaces.
    grounded = failing.client.get("/api/v1/tag-wiki/lookup", params={"tag": "solo"})
    assert grounded.status_code == 409
    assert grounded.json()["detail"]["code"] == "wiki_tag_db_unavailable"

    dataset = failing.client.get("/api/v1/tag-manager/datasets/missing")
    assert dataset.status_code == 404
    assert dataset.json()["detail"]["code"] == "dataset_not_found"

    page = failing.client.get("/api/v1/tag-wiki/page/missing")
    assert page.status_code == 404
    assert page.json()["detail"]["code"] == "wiki_page_not_found"

    # Wiki without any data: wiki_not_built wins before the tag db check.
    empty = _error_app(tmp_path / "empty", fail_tag_db=False)
    lookup = empty.client.get("/api/v1/tag-wiki/lookup", params={"tag": "hug"})
    assert lookup.status_code == 409
    assert lookup.json()["detail"]["code"] == "wiki_not_built"


def test_validation_rejects_illegal_payloads_on_both_routers(dual) -> None:
    cases = [
        ("/api/v1/tag-manager/tag-db", None, {"profile": "gelbooru", "query": "x"}),
        ("/api/v1/tag-manager/translations/translate", "post", {"profile": "e621", "tags": []}),
        ("/api/v1/tag-manager/translations/translate", "post", {"profile": "e621", "tags": ["   "]}),
        ("/api/v1/tag-wiki/search", "post", {"query": "x", "profile": "gelbooru"}),
        ("/api/v1/tag-wiki/translate", "post", {"scope": "unknown"}),
    ]
    for path, method, payload in cases:
        if method == "post":
            response = dual.client.post(path, json=payload)
        else:
            response = dual.client.get(path, params=payload)
        assert response.status_code == 422, (path, response.text)


def test_provider_failures_map_to_retryable_502s(dual) -> None:
    seed_page(dual.wiki.store, "hug", text="Use for hugging.")

    # Upstream exception while learning a translation.
    dual.provider.error = RuntimeError("upstream refused")
    failed = dual.client.post(
        "/api/v1/tag-manager/translations/translate", json={"profile": "e621", "tags": ["hug"]}
    )
    assert failed.status_code == 502
    detail = failed.json()["detail"]
    assert detail["code"] == "tag_translate_failed" and detail["retryable"] is True
    assert not (dual.user_dir / "e621-zh.csv").exists()  # nothing half-saved

    # A model that echoes the English tag back yields nothing usable.
    dual.provider.error = None
    dual.provider.reply = json.dumps({"hug": "hug"})
    unusable = dual.client.post(
        "/api/v1/tag-manager/translations/translate", json={"profile": "e621", "tags": ["hug"]}
    )
    assert unusable.status_code == 502
    assert unusable.json()["detail"]["code"] == "tag_translate_failed"

    # Wiki ask surfaces its own code on the same failure mode.
    dual.provider.reply = ""
    dual.provider.error = RuntimeError("boom")
    ask = dual.client.post("/api/v1/tag-wiki/ask", json={"query": "拥抱用什么tag"})
    assert ask.status_code == 502
    assert ask.json()["detail"]["code"] == "wiki_ask_failed"
    assert ask.json()["detail"]["retryable"] is True
    dual.provider.error = None


def test_empty_model_vocab_translates_nothing_without_a_task(dual) -> None:
    """An empty vocab scope is an idle no-op, not a background job or error."""

    seed_page(dual.wiki.store, "hug", text="Use for hugging.")
    dual.wiki._vocab_provider = lambda: []

    started = dual.client.post("/api/v1/tag-wiki/translate", json={"scope": "model_vocab"})
    assert started.status_code == 202
    body = started.json()
    assert body["state"] == "idle"
    assert body["total"] == 0 and body["done"] == 0 and body["failed"] == 0
    assert dual.wiki._translate_task is None
    assert dual.provider.calls == []


def test_wiki_translate_without_provider_is_a_setup_state(tmp_path: Path) -> None:
    """start_translate resolves the provider before the scope, even when empty."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store, "hug", text="Use for hugging.")
    wiki = TagWikiService(
        store=store,
        tag_database=StubTagDatabase(
            {"e621-snap-a": E621_SNAP_A}, tmp_path / "resources-vocab"
        ),
        translations=TagTranslations(tmp_path / "dicts", tmp_path / "user"),
        data_dir=tmp_path,
    )
    wiki.tag_database.ensure_loaded("e621", resource_id="e621-snap-a")
    with pytest.raises(TagWikiError) as excinfo:
        asyncio.run(wiki.start_translate(TranslateRequest(scope="model_vocab")))
    assert excinfo.value.code == "wiki_ask_unavailable"
    assert excinfo.value.status_code == 409
    asyncio.run(wiki.aclose())


# -- full Runtime wiring (create_app) ----------------------------------------


@pytest.fixture()
def runtime_client(tmp_path: Path):
    """The real create_app Runtime, isolated via TAGGER2_PROJECT_ROOT.

    The Runtime resolves several default paths (tag manager store, tag
    database catalog, wiki data dir) through ``get_settings()``; pointing the
    env at the tmp project root keeps every write inside the test sandbox.
    """

    previous = os.environ.get("TAGGER2_PROJECT_ROOT")
    os.environ["TAGGER2_PROJECT_ROOT"] = str(tmp_path)
    reset_settings_cache()
    reset_translation_cache()
    try:
        settings = AppConfig(
            project_root=tmp_path,
            data_dir=tmp_path / "data",
            cache_dir=tmp_path / "cache",
            production=True,
        )
        with TestClient(create_app(settings)) as client:
            yield client
    finally:
        if previous is None:
            os.environ.pop("TAGGER2_PROJECT_ROOT", None)
        else:
            os.environ["TAGGER2_PROJECT_ROOT"] = previous
        reset_settings_cache()
        reset_translation_cache()


def _register_snapshot(runtime: Any, tmp_path: Path) -> None:
    staged = tmp_path / "snapshot.json"
    staged.write_text(json.dumps(E621_SNAP_A), encoding="utf-8")
    runtime.workflow_resources.import_resource(
        source_path=staged,
        resource_id="classify-e621-integration-v1",
        category=CLASSIFY_RESOURCE_CATEGORY,
        profile="e621",
    )


def test_runtime_shares_tag_database_and_translations(runtime_client, tmp_path: Path) -> None:
    """main.py must hand one TagDatabase/TagTranslations to both services."""

    runtime = runtime_client.app.state.runtime
    assert runtime.tag_wiki.tag_database is runtime.tag_manager.tag_database
    assert runtime.tag_wiki.translations is runtime.tag_manager.translations

    # Every default path stays inside the configured data dir.
    data_dir = runtime.settings.data_dir
    assert Path(str(runtime.tag_manager.store.db_path)) == data_dir / "tag_manager" / "tag_manager.sqlite3"
    assert runtime.tag_manager.translations.user_dir == data_dir / "tag_manager" / "translations"
    assert runtime.tag_wiki._data_dir == data_dir

    # The default tag database catalog shares the runtime resource scope, so a
    # snapshot imported through the workflow catalog is immediately visible.
    _register_snapshot(runtime, tmp_path)
    assert runtime.tag_manager.tag_database.available_profiles() == {
        "e621": ["classify-e621-integration-v1"]
    }

    info = runtime_client.get("/api/v1/tag-manager/tag-db/info").json()
    assert info["available"] == {"e621": ["classify-e621-integration-v1"]}
    assert info["loaded"] == {"e621": False, "danbooru": False}
    # No dictionaries ship inside the tmp project root: degraded, not broken.
    assert info["translations"]["e621"]["entries"] == 0

    status = runtime_client.get("/api/v1/tag-wiki/status")
    assert status.status_code == 200
    assert set(status.json()["profiles"]) == {"e621", "danbooru"}
    assert status.json()["database"]["exists"] is False


def test_runtime_routes_exchange_updates_end_to_end(runtime_client, tmp_path: Path, monkeypatch) -> None:
    """Dataset indexing, learned translations and wiki lookups over one Runtime."""

    runtime = runtime_client.app.state.runtime
    _register_snapshot(runtime, tmp_path)
    seed_page(runtime.tag_wiki.store, "solo", text="Only one character is present.")
    seed_page(runtime.tag_wiki.store, "hug", text="Use for hugging.")

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")
    (dataset / "a.txt").write_text("1girl, hug\n", encoding="utf-8")
    runtime.allowlist.register(dataset, root_id="integration-root", kind="input", writable=True)

    created = runtime_client.post(
        "/api/v1/tag-manager/datasets",
        json={"root_id": "integration-root", "relative_path": "", "profile": "e621"},
    )
    assert created.status_code == 202
    session = _wait_ready(runtime_client, created.json()["id"])
    assert session["image_count"] == 1

    images = runtime_client.get(f"/api/v1/tag-manager/datasets/{session['id']}/images").json()
    assert {t["tag"]: t["category"] for t in images["items"][0]["tags"]} == {
        "1girl": "general",
        "hug": "general",
    }

    fake = FakeProvider(reply=json.dumps({"hug": "拥抱"}, ensure_ascii=False))
    monkeypatch.setattr(runtime.tag_manager, "_provider_factory", lambda _pid: fake)
    monkeypatch.setattr(runtime.tag_manager, "_provider_ids", lambda: ["fake-provider"])
    learned = runtime_client.post(
        "/api/v1/tag-manager/translations/translate",
        json={"profile": "e621", "tags": ["hug"]},
    )
    assert learned.status_code == 200
    assert learned.json()["translated_now"] == 1

    lookup = runtime_client.get("/api/v1/tag-wiki/lookup", params={"tag": "1girl"})
    assert lookup.status_code == 200
    payload = lookup.json()
    assert payload["resolved"] is True
    assert payload["tag"]["name"] == "solo"
    assert payload["tag"]["alias_of"] == "1girl"
    # The learned translation crosses the module boundary immediately.
    assert [tag["translation"] for tag in payload["implications"]] == ["拥抱"]

    search = runtime_client.post("/api/v1/tag-wiki/search", json={"query": "hugging", "top_k": 5})
    assert search.status_code == 200
    suggested = {tag["name"]: tag["translation"] for tag in search.json()["suggested_tags"]}
    assert suggested.get("hug") == "拥抱"

    info = runtime_client.get("/api/v1/tag-manager/tag-db/info").json()
    assert info["translations"]["e621"]["user_entries"] == 1


def test_runtime_serves_one_flat_error_envelope(runtime_client, tmp_path: Path) -> None:
    """Every router error passes through the app-wide handler: flat, typed."""

    runtime = runtime_client.app.state.runtime

    def assert_flat(response, status: int, code: str) -> dict[str, Any]:
        assert response.status_code == status, response.text
        body = response.json()
        assert set(body) == {"code", "message", "fields", "request_id", "retryable"}
        assert "detail" not in body
        assert body["code"] == code
        assert body["message"]
        assert body["request_id"]
        assert body["retryable"] is False
        assert response.headers["x-request-id"] == body["request_id"]
        return body

    missing = runtime_client.get(
        "/api/v1/tag-manager/datasets/missing", headers={"x-request-id": "integration-req-1"}
    )
    body = assert_flat(missing, 404, "dataset_not_found")
    assert body["request_id"] == "integration-req-1"

    assert_flat(
        runtime_client.get("/api/v1/tag-wiki/lookup", params={"tag": "hug"}), 409, "wiki_not_built"
    )
    assert_flat(
        runtime_client.get("/api/v1/tag-manager/tag-db", params={"query": "solo"}),
        409,
        "tag_db_unavailable",
    )

    # With wiki data but no snapshot, the tag-db setup state is distinct.
    seed_page(runtime.tag_wiki.store, "solo", text="Only one character.")
    assert_flat(
        runtime_client.get("/api/v1/tag-wiki/lookup", params={"tag": "solo"}),
        409,
        "wiki_tag_db_unavailable",
    )
    page = runtime_client.get("/api/v1/tag-wiki/page/solo")
    assert page.status_code == 200 and page.json()["title"] == "solo"
    assert_flat(
        runtime_client.get("/api/v1/tag-wiki/page/missing"), 404, "wiki_page_not_found"
    )

    validation = runtime_client.post(
        "/api/v1/tag-wiki/search", json={"query": "x", "profile": "gelbooru"}
    )
    body = assert_flat(validation, 422, "validation_error")
    assert body["fields"] and "profile" in body["fields"]

    blank = runtime_client.post(
        "/api/v1/tag-manager/translations/translate", json={"profile": "e621", "tags": []}
    )
    assert_flat(blank, 422, "validation_error")
