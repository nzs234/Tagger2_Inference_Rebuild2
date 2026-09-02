"""Bilingual tag support: offline dictionaries, annotation, NL translation."""

import csv
import gzip
import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tagger2.security import PathAllowlist
from tagger2.tag_manager.api import create_tag_manager_router
from tagger2.tag_manager.contracts import (
    CreateDatasetRequest,
    ImageFilter,
    NlTranslateRequest,
    TranslationLookupRequest,
)
from tagger2.tag_manager.service import TagManagerError, TagManagerService
from tagger2.tag_manager.storage import TagManagerStore
from tagger2.tag_manager.translations import (
    TagTranslations,
    normalize_lookup_key,
    reset_translation_cache,
)

DANBOORU_ROWS = {
    "1girl": "单人女性",
    "blue_eyes": "蓝瞳",
    "long_hair": "长发",
}
E621_ROWS = {
    "solo": "单人",
    "wolf": "狼",
    "blue_eyes": "蓝瞳",
    "forest": "森林",
    "safe": "全年龄",
}


def write_dictionary(directory: Path, profile: str, rows: dict[str, str]) -> Path:
    """Stage one ``tag,zh`` gzip CSV exactly like the build script emits."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["tag", "zh"])
    for tag in sorted(rows):
        writer.writerow([tag, rows[tag]])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile}-zh.csv.gz"
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        compressed.write(buffer.getvalue().encode("utf-8"))
    path.write_bytes(raw.getvalue())
    return path


def write_manifest(directory: Path) -> None:
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


@pytest.fixture(autouse=True)
def clear_translation_cache():
    reset_translation_cache()
    yield
    reset_translation_cache()


@pytest.fixture()
def dictionary_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "tag_translations"
    write_dictionary(directory, "danbooru", DANBOORU_ROWS)
    write_dictionary(directory, "e621", E621_ROWS)
    write_manifest(directory)
    return directory


# -- dictionary loader ------------------------------------------------------


def test_normalize_lookup_key_folds_case_and_spaces():
    assert normalize_lookup_key("Blue Eyes") == "blue_eyes"
    assert normalize_lookup_key("  LONG_HAIR  ") == "long_hair"


def test_translate_resolves_either_spelling(dictionary_dir: Path):
    translations = TagTranslations(dictionary_dir)

    assert translations.translate("danbooru", "blue_eyes") == "蓝瞳"
    assert translations.translate("danbooru", "Blue Eyes") == "蓝瞳"
    assert translations.translate("danbooru", "unknown_tag") is None
    # The two profiles are independent dictionaries.
    assert translations.translate("e621", "1girl") is None
    assert translations.translate("e621", "wolf") == "狼"


def test_translate_rejects_unknown_profile(dictionary_dir: Path):
    translations = TagTranslations(dictionary_dir)

    assert translations.translate("gelbooru", "blue_eyes") is None
    assert translations.translate_many("gelbooru", ["blue_eyes"]) == {}


def test_translate_many_keys_on_the_input_spelling(dictionary_dir: Path):
    translations = TagTranslations(dictionary_dir)

    result = translations.translate_many("danbooru", ["Blue Eyes", "long_hair", "nope"])

    assert result == {"Blue Eyes": "蓝瞳", "long_hair": "长发"}


def test_info_reports_entry_counts_and_source(dictionary_dir: Path):
    info = TagTranslations(dictionary_dir).info()

    assert info["danbooru"]["entries"] == len(DANBOORU_ROWS)
    assert info["danbooru"]["source"] == "danbooru-zh.csv.gz"
    assert info["danbooru"]["updated"] == "2026-09-02T00:00:00Z"
    assert info["e621"]["entries"] == len(E621_ROWS)


def test_missing_directory_degrades_to_english_only(tmp_path: Path):
    translations = TagTranslations(tmp_path / "absent")

    assert translations.translate("danbooru", "blue_eyes") is None
    info = translations.info()
    assert info["danbooru"] == {
        "entries": 0,
        "loaded": True,
        "source": None,
        "updated": None,
    }


def test_damaged_dictionary_degrades_instead_of_raising(tmp_path: Path):
    directory = tmp_path / "tag_translations"
    directory.mkdir()
    (directory / "danbooru-zh.csv.gz").write_bytes(b"not gzip at all")

    assert TagTranslations(directory).translate("danbooru", "blue_eyes") is None


def test_wrong_header_is_rejected(tmp_path: Path):
    directory = tmp_path / "tag_translations"
    directory.mkdir()
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        compressed.write(b"name,chinese\nblue_eyes,\xe8\x93\x9d\xe7\x9e\xb3\n")
    (directory / "danbooru-zh.csv.gz").write_bytes(raw.getvalue())

    assert TagTranslations(directory).translate("danbooru", "blue_eyes") is None


def test_shipped_dictionaries_are_present_and_indexed():
    """The committed dictionaries are what makes the feature work offline."""

    translations = TagTranslations()
    info = translations.info()

    assert info["danbooru"]["entries"] > 100_000
    assert info["e621"]["entries"] > 10_000
    assert translations.translate("danbooru", "blue_eyes")
    # e621's own vocabulary comes from the curated supplement.
    assert translations.translate("e621", "anthro") == "兽人"
    assert translations.translate("e621", "female") == "雌性"


# -- service integration ----------------------------------------------------


class FakeTagDatabase:
    def __init__(self) -> None:
        self.categories = {"wolf": "general", "rex": "character", "blue_eyes": "general"}

    def is_loaded(self, profile: str) -> bool:
        return True

    def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
        return None

    def lookup(self, profile: str, tag: str, *, resolve_alias: bool = True):
        category = self.categories.get(tag.casefold())
        if category is None:
            return None
        return {"name": tag, "category": category, "post_count": 7, "alias_of": None}

    def autocomplete(self, profile: str, query: str, *, limit: int = 20):
        return [
            {"name": "wolf", "category": "general", "post_count": 7, "alias_of": None},
            {"name": "walrus", "category": "general", "post_count": 3, "alias_of": None},
        ]

    def available_profiles(self) -> dict[str, list[str]]:
        return {"e621": ["classify-e621-test-v1"], "danbooru": ["classify-danbooru-test-v1"]}


class FakeThumbnails:
    def ensure_thumbnail(self, source: Path, *, size: int, mtime: float) -> Path:
        return source.with_suffix(".thumb.jpg")


class FakeProvider:
    """Minimal VisionProvider stand-in for the translation route."""

    model = "fake-model"

    def __init__(self, reply: str = "一只狼站在森林里。", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def generate(self, image, prompt, *, model=None, system_prompt=None, **_kwargs):
        self.calls.append(
            {"image": image, "prompt": prompt, "model": model, "system_prompt": system_prompt}
        )
        if self.error is not None:
            raise self.error
        return self.reply


STANDARD_JSON = {
    "quality": ["safe"],
    "count": "solo",
    "character": "rex",
    "series": "",
    "artist": "",
    "appearance": ["blue_eyes"],
    "tags": ["wolf"],
    "environment": ["forest"],
    "nl": "A wolf stands in a forest.",
}


def _make_image(directory: Path, name: str) -> None:
    Image.new("RGB", (8, 8)).save(directory / name)


@pytest.fixture()
def workspace(tmp_path: Path, dictionary_dir: Path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")
    (dataset / "a.txt").write_text("solo, wolf\n", encoding="utf-8")
    _make_image(dataset, "b.png")
    (dataset / "b.json").write_text(json.dumps(STANDARD_JSON), encoding="utf-8")

    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="test-root", kind="input", writable=True)
    provider = FakeProvider()
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
        translations=TagTranslations(dictionary_dir),
        provider_factory=lambda provider_id: provider,
        provider_ids=lambda: ["p-default", "p-second"],
    )
    session = service.create_session(
        CreateDatasetRequest(root_id="test-root", relative_path="", profile="e621")
    )
    service.index_session(str(session["id"]))
    return service, str(session["id"]), dataset, provider


def test_list_images_carries_translations(workspace):
    service, session_id, _dataset, _provider = workspace

    items = service.list_images(session_id)["items"]
    a = next(item for item in items if item["file_name"] == "a.png")

    assert {tag["tag"]: tag["translation"] for tag in a["tags"]} == {
        "solo": "单人",
        "wolf": "狼",
    }


def test_get_image_translations_cover_every_nine_field_list(workspace):
    service, session_id, _dataset, _provider = workspace
    items = service.list_images(session_id)["items"]
    image_id = int(next(item for item in items if item["file_name"] == "b.png")["id"])

    detail = service.get_image(session_id, image_id)

    # quality/appearance/tags/environment are all tag-like and translated; the
    # free-form nl paragraph is not part of the map.
    assert detail["translations"] == {
        "safe": "全年龄",
        "blue_eyes": "蓝瞳",
        "wolf": "狼",
        "forest": "森林",
    }
    assert {tag["tag"]: tag["translation"] for tag in detail["tags"]} == {"wolf": "狼"}


def test_untranslated_tag_reports_none(workspace):
    service, session_id, dataset, _provider = workspace
    (dataset / "a.txt").write_text("solo, unmapped_tag\n", encoding="utf-8")
    service.index_session(session_id)

    items = service.list_images(session_id)["items"]
    a = next(item for item in items if item["file_name"] == "a.png")

    assert {tag["tag"]: tag["translation"] for tag in a["tags"]} == {
        "solo": "单人",
        "unmapped_tag": None,
    }


def test_tag_stats_and_autocomplete_carry_translations(workspace):
    service, session_id, _dataset, _provider = workspace

    stats = service.tag_stats(session_id)
    assert {row["tag"]: row["translation"] for row in stats}["wolf"] == "狼"

    suggestions = service.autocomplete("e621", "w")["items"]
    assert [(item["name"], item["translation"]) for item in suggestions] == [
        ("wolf", "狼"),
        ("walrus", None),
    ]


def test_tag_db_info_reports_dictionary_state(workspace):
    service, _session_id, _dataset, _provider = workspace

    info = service.tag_db_info()

    assert info["translations"]["e621"]["entries"] == len(E621_ROWS)
    assert info["translations"]["danbooru"]["entries"] == len(DANBOORU_ROWS)


def test_lookup_translations_batch(workspace):
    service, _session_id, _dataset, _provider = workspace

    result = service.lookup_translations(
        TranslationLookupRequest(profile="e621", tags=["wolf", "Blue Eyes", "nope", "  "])
    )

    assert result["profile"] == "e621"
    assert result["translations"] == {"wolf": "狼", "Blue Eyes": "蓝瞳"}


# -- underscore / space insensitive filtering --------------------------------


def test_filters_match_either_tag_spelling(workspace):
    service, session_id, dataset, _provider = workspace
    (dataset / "a.txt").write_text("solo, blue eyes\n", encoding="utf-8")
    service.index_session(session_id)

    underscore = service.list_images(
        session_id, image_filter=ImageFilter(include_tags=["blue_eyes"])
    )
    spaced = service.list_images(
        session_id, image_filter=ImageFilter(include_tags=["Blue Eyes"])
    )
    excluded = service.list_images(
        session_id, image_filter=ImageFilter(exclude_tags=["blue_eyes"])
    )

    assert [item["file_name"] for item in underscore["items"]] == ["a.png"]
    assert [item["file_name"] for item in spaced["items"]] == ["a.png"]
    assert "a.png" not in [item["file_name"] for item in excluded["items"]]


def test_any_mode_filter_normalizes_every_tag(workspace):
    service, session_id, dataset, _provider = workspace
    (dataset / "a.txt").write_text("blue eyes\n", encoding="utf-8")
    service.index_session(session_id)

    payload = service.list_images(
        session_id,
        image_filter=ImageFilter(include_tags=["blue_eyes", "nothing"], include_mode="any"),
    )

    assert [item["file_name"] for item in payload["items"]] == ["a.png"]


# -- NL translation ---------------------------------------------------------


async def test_translate_nl_uses_the_first_provider_by_default(workspace):
    service, _session_id, _dataset, provider = workspace

    result = await service.translate_nl(
        NlTranslateRequest(text="A wolf stands in a forest.")
    )

    assert result == {
        "text": "一只狼站在森林里。",
        "target": "zh",
        "provider_id": "p-default",
        "model": "fake-model",
    }
    call = provider.calls[0]
    assert call["image"] is None
    assert call["prompt"] == "A wolf stands in a forest."
    assert "Simplified Chinese" in str(call["system_prompt"])


async def test_translate_nl_honours_explicit_provider_and_model(workspace):
    service, _session_id, _dataset, provider = workspace

    result = await service.translate_nl(
        NlTranslateRequest(text="一只狼", target="en", provider_id="p-second", model="m-1")
    )

    assert result["provider_id"] == "p-second"
    assert result["model"] == "m-1"
    assert result["target"] == "en"
    assert "natural English" in str(provider.calls[0]["system_prompt"])


async def test_translate_nl_without_a_provider_is_a_setup_state(tmp_path: Path, dictionary_dir: Path):
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=PathAllowlist(),
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
        translations=TagTranslations(dictionary_dir),
    )

    with pytest.raises(TagManagerError) as excinfo:
        await service.translate_nl(NlTranslateRequest(text="A wolf."))

    assert excinfo.value.code == "nl_translate_unavailable"
    assert excinfo.value.status_code == 409


async def test_translate_nl_maps_provider_failure_to_502(tmp_path: Path, dictionary_dir: Path):
    provider = FakeProvider(error=RuntimeError("upstream refused"))
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=PathAllowlist(),
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
        translations=TagTranslations(dictionary_dir),
        provider_factory=lambda provider_id: provider,
        provider_ids=lambda: ["p-default"],
    )

    with pytest.raises(TagManagerError) as excinfo:
        await service.translate_nl(NlTranslateRequest(text="A wolf."))

    assert excinfo.value.code == "nl_translate_failed"
    assert excinfo.value.status_code == 502
    assert excinfo.value.retryable is True


async def test_translate_nl_rejects_an_empty_model_reply(tmp_path: Path, dictionary_dir: Path):
    provider = FakeProvider(reply="   ")
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=PathAllowlist(),
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
        translations=TagTranslations(dictionary_dir),
        provider_factory=lambda provider_id: provider,
        provider_ids=lambda: ["p-default"],
    )

    with pytest.raises(TagManagerError) as excinfo:
        await service.translate_nl(NlTranslateRequest(text="A wolf."))

    assert excinfo.value.code == "nl_translate_failed"


# -- routes -----------------------------------------------------------------


@pytest.fixture()
def client(workspace):
    service, session_id, _dataset, _provider = workspace
    app = FastAPI()
    app.include_router(create_tag_manager_router(service))
    return TestClient(app), session_id


def test_route_images_and_stats_expose_translation(client):
    http, session_id = client

    images = http.get(f"/api/v1/tag-manager/datasets/{session_id}/images")
    stats = http.get(f"/api/v1/tag-manager/datasets/{session_id}/tags/stats")

    assert images.status_code == 200
    a = next(item for item in images.json()["items"] if item["file_name"] == "a.png")
    assert {tag["tag"]: tag["translation"] for tag in a["tags"]}["wolf"] == "狼"
    assert stats.status_code == 200
    assert all("translation" in row for row in stats.json()["items"])


def test_route_tag_db_and_info(client):
    http, _session_id = client

    suggestions = http.get("/api/v1/tag-manager/tag-db?profile=e621&query=w")
    info = http.get("/api/v1/tag-manager/tag-db/info")

    assert suggestions.status_code == 200
    assert suggestions.json()["items"][0]["translation"] == "狼"
    assert info.status_code == 200
    assert info.json()["translations"]["e621"]["entries"] == len(E621_ROWS)


def test_route_translation_lookup(client):
    http, _session_id = client

    response = http.post(
        "/api/v1/tag-manager/translations/lookup",
        json={"profile": "e621", "tags": ["wolf", "nope"]},
    )

    assert response.status_code == 200
    assert response.json() == {"profile": "e621", "translations": {"wolf": "狼"}}


def test_route_translation_lookup_rejects_oversized_batch(client):
    http, _session_id = client

    response = http.post(
        "/api/v1/tag-manager/translations/lookup",
        json={"profile": "e621", "tags": [f"tag_{index}" for index in range(501)]},
    )

    assert response.status_code == 422


def test_route_nl_translate(client):
    http, _session_id = client

    response = http.post(
        "/api/v1/tag-manager/nl/translate",
        json={"text": "A wolf stands in a forest.", "target": "zh"},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "一只狼站在森林里。"


def test_route_nl_translate_reports_missing_provider(dictionary_dir: Path):
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=PathAllowlist(),
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
        translations=TagTranslations(dictionary_dir),
    )
    app = FastAPI()
    app.include_router(create_tag_manager_router(service))

    response = TestClient(app).post(
        "/api/v1/tag-manager/nl/translate", json={"text": "A wolf."}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "nl_translate_unavailable"


def test_route_nl_translate_rejects_blank_and_oversized_text(client):
    http, _session_id = client

    blank = http.post("/api/v1/tag-manager/nl/translate", json={"text": "   "})
    oversized = http.post("/api/v1/tag-manager/nl/translate", json={"text": "a" * 8001})

    assert blank.status_code == 422
    assert oversized.status_code == 422
