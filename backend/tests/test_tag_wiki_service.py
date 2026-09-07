"""Service and API layer tests for the tag wiki.

Everything runs offline: the store is a real :class:`WikiStore` on a temp
file, the tag database / translations / provider are fakes, and the build
pipeline test uses a synthesized dump.
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tagger2.tag_manager.tag_db import TagDatabaseError
from tagger2.tag_wiki.api import create_tag_wiki_router
from tagger2.tag_wiki.contracts import BuildRequest, TranslateRequest
from tagger2.tag_wiki.service import TagWikiError, TagWikiService
from tagger2.tag_wiki.wiki_store import WikiStore


def _info(name: str, *, category: str = "general", post_count: int = 100) -> dict[str, Any]:
    return {"name": name, "category": category, "post_count": post_count, "alias_of": None}


class FakeTagDatabase:
    """Duck-typed stand-in for TagDatabase (the service only needs four calls).

    ``aliases`` maps alias -> canonical name and mirrors the real database's
    alias resolution: a lookup of the alias returns the canonical entry (with
    ``alias_of`` set) or ``None`` when the canonical tag is missing. Optional
    ``per_profile`` vocabularies ``{profile: (tags, aliases)}`` keep profiles
    apart, like the real snapshot-per-profile database.
    """

    def __init__(
        self,
        tags: dict[str, dict[str, Any]] | None = None,
        implications: dict[str, list[str]] | None = None,
        *,
        fail: bool = False,
        aliases: dict[str, str] | None = None,
        per_profile: dict[str, tuple[dict[str, dict[str, Any]], dict[str, str]]] | None = None,
    ) -> None:
        self._tags = tags or {}
        self._implications = implications or {}
        self._fail = fail
        self._aliases = aliases or {}
        self._per_profile = per_profile or {}

    def _vocab_for(self, profile: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        if profile in self._per_profile:
            return self._per_profile[profile]
        return self._tags, self._aliases

    def ensure_loaded(self, profile: str, resource_id: str | None = None) -> None:
        if self._fail:
            raise TagDatabaseError("no classification snapshot available")

    def lookup(self, profile: str, tag: str, *, resolve_alias: bool = True) -> dict[str, Any] | None:
        tags, aliases = self._vocab_for(profile)
        key = tag.casefold()
        if resolve_alias:
            canonical = aliases.get(key)
            if canonical is not None:
                # The real database returns the canonical entry with the alias
                # antecedent in ``alias_of``; a dangling alias (canonical tag
                # missing) resolves to nothing.
                resolved = tags.get(canonical)
                if resolved is None:
                    return None
                return {**resolved, "alias_of": key}
        return tags.get(key)

    def implications_of(
        self, profile: str, tag: str, *, reverse: bool = False
    ) -> list[dict[str, Any]]:
        names = [] if reverse else self._implications.get(tag.casefold(), [])
        return [self._tags[name] for name in names if name in self._tags]

    def top_tags(self, profile: str, *, min_post_count: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        infos = [info for info in self._tags.values() if (info["post_count"] or 0) >= min_post_count]
        infos.sort(key=lambda info: (-(info["post_count"] or 0), info["name"]))
        return infos[:limit] if limit is not None else infos


class FakeTranslations:
    def translate(self, profile: str, tag: str) -> str | None:
        return {"hug": "拥抱", "kiss": "亲吻"}.get(tag)


class FakeProvider:
    def __init__(self, reply: str = "", *, model: str = "fake-model", error: str | None = None) -> None:
        self.reply = reply
        self._model = model
        self.error = error
        self.calls: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self._model

    async def generate(self, image: Any = None, prompt: str = "", *, model: str | None = None, system_prompt: str | None = None, **_: Any) -> str:
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt, "model": model})
        if self.error is not None:
            raise RuntimeError(self.error)
        return self.reply


def make_service(
    tmp_path: Path,
    *,
    store: WikiStore | None = None,
    tag_database: FakeTagDatabase | None = None,
    provider: FakeProvider | None = None,
    vocab: list[str] | None = None,
) -> TagWikiService:
    return TagWikiService(
        store=store or WikiStore(tmp_path / "tag_wiki.sqlite3"),
        tag_database=tag_database or FakeTagDatabase(),
        translations=FakeTranslations(),
        provider_factory=(lambda pid: provider) if provider is not None else None,
        provider_ids=(lambda: ["fake"]) if provider is not None else None,
        vocab_provider=(lambda: vocab) if vocab is not None else None,
        data_dir=tmp_path,
    )


def seed_page(store: WikiStore, title: str = "hug", *, heading: str = "Usage", text: str = "Use for hugging.") -> None:
    store.upsert_page(
        {
            "title": title,
            "display_title": title,
            "body_md": f"h2. {heading}\n{text} See [[kiss]] and {{caress}}.",
            "wiki_id": 1,
            "updated_at": "2026-01-01T00:00:00Z",
            "url": "https://e621.net/wiki_pages/1",
            "sections": [{"heading": heading, "text": text}],
            "links": ["kiss", "caress"],
        }
    )


HUG_TAG_DB = FakeTagDatabase(
    tags={
        "hug": _info("hug", post_count=500),
        "kiss": _info("kiss", post_count=300),
        "rare": _info("rare", post_count=50),
        "some_artist": _info("some_artist", category="artist", post_count=900),
        "some_char": _info("some_char", category="character", post_count=800),
        "some_modeler": _info("some_modeler", category="contributor", post_count=700),
        "dead_tag": _info("dead_tag", category="invalid", post_count=10),
    },
    implications={"hug": ["kiss"]},
)


async def test_prune_unsearchable_chunks(tmp_path: Path) -> None:
    """Link-list category pages (artist/character/contributor/invalid) lose
    their chunks; general pages stay searchable."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store, "hug")
    seed_page(store, "kiss")
    seed_page(store, "some_artist")
    seed_page(store, "some_char")
    seed_page(store, "some_modeler")
    seed_page(store, "dead_tag")
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    pruned = service._prune_unsearchable_chunks_sync()
    assert pruned == 4
    assert store.get_page("some_artist")["sections"] == []
    assert store.get_page("some_modeler")["sections"] == []
    assert store.get_page("dead_tag")["sections"] == []
    assert store.get_page("hug")["sections"] != []


async def test_prune_drops_url_list_chunks(tmp_path: Path) -> None:
    """Uncategorized stub pages whose bodies are pure link lists are pruned
    by shape, without needing a tag-database category."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    url_list = (
        '* "FurAffinity":https://www.furaffinity.net/user/stub\n'
        '* "Twitter":https://x.com/stub\n'
        '* "Bluesky":https://bsky.app/profile/stub'
    )
    bare_url = "https://www.pixiv.net/member_illust.php"
    two_links = "See the artist's https://example.com/a and https://example.com/b pages."
    seed_page(store, "some_stub", heading="", text=url_list)
    seed_page(store, "bare_url_stub", heading="", text=bare_url)
    seed_page(store, "two_links", heading="", text=two_links)
    seed_page(store, "hug")
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    pruned = service._prune_unsearchable_chunks_sync()
    assert pruned == 2
    # The stub pages stay for exact lookup, but their link-soup chunks are gone.
    assert store.get_page("some_stub")["sections"] == []
    assert store.get_page("bare_url_stub")["sections"] == []
    assert store.get_page("two_links")["sections"] != []
    assert store.get_page("hug")["sections"] != []


async def test_start_build_returns_409_while_running(tmp_path: Path) -> None:
    """A second build while one runs is a 409 wiki_busy, not a queue."""

    service = make_service(tmp_path)

    async def slow_build(request: BuildRequest) -> None:
        await asyncio.sleep(60)

    service._run_build = slow_build  # type: ignore[method-assign]
    await service.start_build(BuildRequest())
    with pytest.raises(TagWikiError) as excinfo:
        await service.start_build(BuildRequest())
    assert excinfo.value.code == "wiki_busy"
    assert excinfo.value.status_code == 409
    await service.aclose()
    assert service._build_task is not None
    assert service._build_task.cancelled()


async def test_start_translate_returns_409_while_running(tmp_path: Path) -> None:
    """A second translate while one runs is a 409 wiki_busy."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store, "hug")
    provider = FakeProvider(reply=SUMMARY_REPLY)
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB, provider=provider)

    async def slow_translate(
        provider: Any, provider_id: str, titles: list[str], model: str | None, profile: str, concurrency: int
    ) -> None:
        await asyncio.sleep(60)

    service._run_translate = slow_translate  # type: ignore[method-assign]
    await service.start_translate(TranslateRequest(scope="popular", min_post_count=0))
    with pytest.raises(TagWikiError) as excinfo:
        await service.start_translate(TranslateRequest(scope="popular", min_post_count=0))
    assert excinfo.value.code == "wiki_busy"
    assert excinfo.value.status_code == 409
    await service.aclose()
    assert service._translate_task is not None
    assert service._translate_task.cancelled()


async def test_translate_scope_excludes_link_list_pages(tmp_path: Path) -> None:
    """The translate job never spends model calls on artist/character pages."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store, "hug")
    seed_page(store, "some_artist")
    provider = FakeProvider(reply=SUMMARY_REPLY)
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB, provider=provider)
    progress = await service.start_translate(TranslateRequest(scope="popular", min_post_count=200))
    assert progress["total"] == 1  # some_artist (900) is excluded, hug (500) remains
    assert service._translate_task is not None
    await service._translate_task
    assert service.translate_progress()["done"] == 1

SUMMARY_REPLY = json.dumps(
    {
        "meaning": "角色之间拥抱的动作。",
        "usage": "两人或多人相拥时使用。",
        "pairing": "常与 couple、kiss 搭配。",
        "notes": "",
        "tags": ["couple", "kiss"],
    },
    ensure_ascii=False,
)


# -- status / lookup ---------------------------------------------------------


async def test_status_empty(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    status = service.status()
    assert status["database"]["exists"] is False
    assert status["profiles"]["e621"]["catalog"]["built"] is False
    assert status["build"]["state"] == "idle"
    assert status["translate"]["state"] == "idle"


async def test_lookup_requires_data(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(TagWikiError) as excinfo:
        await service.lookup("hug")
    assert excinfo.value.code == "wiki_not_built"
    assert excinfo.value.status_code == 409


async def test_lookup_success(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    result = await service.lookup("hug")
    assert result["resolved"] is True
    assert result["tag"] is not None
    assert result["tag"]["name"] == "hug"
    assert result["tag"]["translation"] == "拥抱"
    assert [tag["name"] for tag in result["implications"]] == ["kiss"]
    assert result["page"] is not None
    assert result["page"]["sections"][0]["heading"] == "Usage"
    assert result["page"]["related_tags"] == ["caress", "kiss"]


async def test_lookup_tag_db_unavailable(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    service = make_service(tmp_path, store=store, tag_database=FakeTagDatabase(fail=True))
    with pytest.raises(TagWikiError) as excinfo:
        await service.lookup("hug")
    assert excinfo.value.code == "wiki_tag_db_unavailable"


async def test_page_not_found(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    with pytest.raises(TagWikiError) as excinfo:
        await service.page("missing")
    assert excinfo.value.code == "wiki_page_not_found"
    assert excinfo.value.status_code == 404


# -- search ------------------------------------------------------------------


# -- ask ---------------------------------------------------------------------


# -- ask tag whitelist ---------------------------------------------------------


# -- ask prompt template --------------------------------------------------------


# -- translate ---------------------------------------------------------------


async def test_translate_requires_data_and_provider(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(TagWikiError) as excinfo:
        await service.start_translate(TranslateRequest(scope="model_vocab"))
    assert excinfo.value.code == "wiki_not_built"

    store = WikiStore(tmp_path / "tw2.sqlite3")
    seed_page(store)
    service2 = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    with pytest.raises(TagWikiError) as excinfo:
        await service2.start_translate(TranslateRequest(scope="model_vocab"))
    assert excinfo.value.code == "wiki_ask_unavailable"


async def test_translate_flow(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    provider = FakeProvider(reply=SUMMARY_REPLY)
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB, provider=provider, vocab=["hug"])
    progress = await service.start_translate(TranslateRequest(scope="model_vocab"))
    assert progress["state"] == "running"
    assert progress["total"] == 1
    assert service._translate_task is not None
    await service._translate_task
    final = service.translate_progress()
    assert final["state"] == "idle"
    assert final["done"] == 1
    assert final["failed"] == 0
    summary = store.get_summary("hug")
    assert summary is not None
    assert summary["meaning"].startswith("角色之间")
    assert summary["provider_id"] == "fake"
    assert summary["tags"] == ["couple", "kiss"]
    assert "拥抱" in json.dumps(provider.calls[0]["prompt"], ensure_ascii=False) or "hug" in provider.calls[0]["prompt"]
    # Resumable: a second run finds nothing left and never starts a task.
    again = await service.start_translate(TranslateRequest(scope="model_vocab"))
    assert again["total"] == 0
    assert again["state"] == "idle"
    assert service._translate_task is None or service._translate_task.done()


async def test_translate_scope_filters_unknown_pages(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)  # only "hug" has a page; kiss/rare do not
    provider = FakeProvider(reply=SUMMARY_REPLY)
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB, provider=provider)
    progress = await service.start_translate(TranslateRequest(scope="popular", min_post_count=200))
    assert progress["total"] == 1  # hug (500) and kiss (300) qualify, only hug has a page
    assert service._translate_task is not None
    await service._translate_task
    assert service.translate_progress()["done"] == 1


async def test_translate_counts_failures(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    provider = FakeProvider(reply="模型胡言乱语，没有 JSON")
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB, provider=provider, vocab=["hug"])
    await service.start_translate(TranslateRequest(scope="model_vocab"))
    assert service._translate_task is not None
    await service._translate_task
    final = service.translate_progress()
    assert final["done"] == 0
    assert final["failed"] == 1
    assert store.get_summary("hug") is None


class OverlapTrackingProvider(FakeProvider):
    """FakeProvider that records how many generate calls overlap in time."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.in_flight = 0
        self.max_in_flight = 0

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            return await super().generate(*args, **kwargs)
        finally:
            self.in_flight -= 1


async def test_translate_runs_pages_in_parallel(tmp_path: Path) -> None:
    """Concurrency workers overlap model calls and still persist every summary."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    titles = [f"tag_{i}" for i in range(6)]
    for title in titles:
        seed_page(store, title)
    tag_db = FakeTagDatabase(tags={name: _info(name, post_count=500) for name in titles})
    provider = OverlapTrackingProvider(reply=SUMMARY_REPLY)
    service = make_service(tmp_path, store=store, tag_database=tag_db, provider=provider)
    progress = await service.start_translate(
        TranslateRequest(scope="popular", min_post_count=0, concurrency=3)
    )
    assert progress["total"] == 6
    assert service._translate_task is not None
    await service._translate_task
    final = service.translate_progress()
    assert final["done"] == 6
    assert final["failed"] == 0
    # The pool ran 3 calls at once — and never more than requested.
    assert provider.max_in_flight == 3
    for title in titles:
        summary = store.get_summary(title)
        assert summary is not None
        assert summary["meaning"].startswith("角色之间")


# -- build pipeline ----------------------------------------------------------


def _write_dump(path: Path) -> None:
    rows = [
        {"id": "1", "title": "hug", "body": "h2. Usage\nUse for hugging. See [[kiss]].", "updated_at": "2026-09-01T00:00:00Z"},
        {"id": "2", "title": "Kiss", "body": "Mouth contact. {{hug}} related.", "updated_at": "2026-09-01T00:00:00Z"},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


async def test_build_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    downloads = tmp_path / "tag_wiki" / "downloads"
    _write_dump(downloads / "wiki_pages-2026-09-01.csv.gz")

    status = await service.start_build(BuildRequest(download_dump=False, reindex=True))
    assert status["build"]["state"] == "running"
    assert service._build_task is not None
    await service._build_task

    final = service.status()
    assert final["database"]["pages"] == 2
    assert final["database"]["chunks"] > 0
    assert final["database"]["dump_date"] == "2026-09-01"
    assert final["build"]["phase"] == "done"
    assert final["build"]["state"] == "idle"

    # The imported pages are retrievable end-to-end via lookup.
    lookup = await service.lookup("hug")
    assert lookup["page"] is not None


async def test_build_rejects_concurrent_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tagger2.tag_wiki import service as service_module

    service = make_service(tmp_path)

    def never_return() -> None:
        raise AssertionError("should not reach the pipeline")

    monkeypatch.setattr(service_module, "latest_dump_html", never_return)
    started = await service.start_build(BuildRequest(download_dump=True, reindex=False))
    assert started["build"]["state"] == "running"
    with pytest.raises(TagWikiError) as excinfo:
        await service.start_build(BuildRequest(download_dump=False))
    assert excinfo.value.code == "wiki_busy"
    # Let the first task fail (network is stubbed to raise) and clean up.
    assert service._build_task is not None
    await service._build_task
    assert service.status()["build"]["state"] == "error"


# -- public job handles -------------------------------------------------------


async def test_public_job_handles_when_idle(tmp_path: Path) -> None:
    """Accessors and waiters are usable when nothing is running at all."""
    service = make_service(tmp_path)
    assert service.build_task() is None
    assert service.translate_task() is None
    assert (await service.wait_build())["state"] == "idle"
    assert (await service.wait_translate())["state"] == "idle"


async def test_wait_build_joins_background_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait_build() is the public way to join the build: it returns the final
    status document instead of exposing the private task."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    service = make_service(tmp_path, store=store, tag_database=HUG_TAG_DB)
    downloads = tmp_path / "tag_wiki" / "downloads"
    _write_dump(downloads / "wiki_pages-2026-09-01.csv.gz")

    await service.start_build(BuildRequest(download_dump=False, reindex=True))
    assert service.build_task() is not None
    final = await service.wait_build()
    assert final["phase"] == "done"
    assert final["state"] == "idle"
    assert service.build_task() is not None
    assert service.build_task().done()
    assert service.status()["database"]["pages"] == 2


async def test_wait_translate_joins_background_job(tmp_path: Path) -> None:
    """wait_translate() is the public way to join the translate job."""
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    provider = FakeProvider(reply=SUMMARY_REPLY)
    service = make_service(
        tmp_path, store=store, tag_database=HUG_TAG_DB, provider=provider, vocab=["hug"]
    )
    progress = await service.start_translate(TranslateRequest(scope="model_vocab"))
    assert progress["state"] == "running"
    assert service.translate_task() is not None
    final = await service.wait_translate()
    assert final["state"] == "idle"
    assert final["done"] == 1
    assert final["failed"] == 0
    assert service.translate_task() is not None
    assert service.translate_task().done()


# -- API layer ---------------------------------------------------------------


def _make_app(service: TagWikiService) -> TestClient:
    app = FastAPI()
    app.include_router(create_tag_wiki_router(service))
    return TestClient(app)


def test_api_status_and_lookup_error_shape(tmp_path: Path) -> None:
    client = _make_app(make_service(tmp_path))
    response = client.get("/api/v1/tag-wiki/status")
    assert response.status_code == 200
    assert response.json()["database"]["exists"] is False
    lookup = client.get("/api/v1/tag-wiki/lookup", params={"tag": "hug"})
    assert lookup.status_code == 409
    assert lookup.json()["detail"]["code"] == "wiki_not_built"


def test_api_translate_and_validation(tmp_path: Path) -> None:
    client = _make_app(make_service(tmp_path))
    empty = client.post("/api/v1/tag-wiki/translate", json={"scope": "model_vocab"})
    assert empty.status_code == 409
    assert empty.json()["detail"]["code"] == "wiki_not_built"

    invalid = client.post("/api/v1/tag-wiki/translate", json={"scope": "unknown"})
    assert invalid.status_code == 422

    progress = client.get("/api/v1/tag-wiki/translate/progress")
    assert progress.status_code == 200
    assert progress.json()["state"] == "idle"


# -- danbooru profile ---------------------------------------------------------


async def test_danbooru_profile_lookup_and_search(tmp_path: Path) -> None:
    """The danbooru mirror resolves through its own store; profiles stay apart."""

    e621_store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(e621_store, "hug")
    danbooru_store = WikiStore(tmp_path / "tag_wiki_danbooru.sqlite3")
    seed_page(danbooru_store, "hug")
    seed_page(danbooru_store, "straight_hair")
    tag_database = FakeTagDatabase(
        {
            "hug": _info("hug", post_count=500),
            "straight_hair": _info("straight_hair", post_count=900),
        }
    )
    service = TagWikiService(
        store=e621_store,
        danbooru_store=danbooru_store,
        tag_database=tag_database,
        translations=FakeTranslations(),
        data_dir=tmp_path,
    )

    status = service.status()
    assert status["profiles"]["e621"]["database"]["pages"] == 1
    assert status["profiles"]["danbooru"]["database"]["pages"] == 2
    assert status["database"]["pages"] == 1  # backward-compatible e621 view

    lookup = await service.lookup("hug", profile="danbooru")
    assert lookup["resolved"] is True
    assert lookup["page"] is not None
    assert lookup["tag"] is not None and lookup["tag"]["name"] == "hug"

    page = await service.page("straight_hair", profile="danbooru")
    assert page["title"] == "straight_hair"
    with pytest.raises(TagWikiError):
        await service.page("straight_hair", profile="e621")
    await service.aclose()


async def test_frozen_mode_rejects_build_and_translate(tmp_path: Path) -> None:
    """Packaged (frozen) builds answer 403 wiki_frozen on maintenance entry
    points while lookups stay usable."""

    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    seed_page(store)
    service = TagWikiService(
        store=store,
        tag_database=HUG_TAG_DB,
        translations=FakeTranslations(),
        data_dir=tmp_path,
        frozen=True,
    )

    status = service.status()
    assert status["frozen"] is True

    with pytest.raises(TagWikiError) as build_exc:
        await service.start_build(BuildRequest(profile="e621", download_dump=False, reindex=False))
    assert build_exc.value.code == "wiki_frozen"
    assert build_exc.value.status_code == 403

    with pytest.raises(TagWikiError) as translate_exc:
        await service.start_translate(TranslateRequest(profile="e621", scope="model_vocab"))
    assert translate_exc.value.code == "wiki_frozen"
    assert translate_exc.value.status_code == 403

    # Read paths are untouched: lookup works on a seeded page.
    result = await service.lookup("hug")
    assert result["query"] == "hug"
    await service.aclose()


async def test_unfrozen_mode_exposes_frozen_false(tmp_path: Path) -> None:
    store = WikiStore(tmp_path / "tag_wiki.sqlite3")
    service = make_service(tmp_path, store=store)
    assert service.status()["frozen"] is False
    await service.aclose()
