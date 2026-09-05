"""Input-safety tests for the tag manager.

Covers the P1 hardening work: lossless comma-tag transmission (escaped and
repeated query parameters), per-tag length limits, regex pattern guards and
provider-error sanitisation.  Tag canonicalization (spaces, underscores,
case) is covered alongside so the shared rules stay pinned.
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from tagger2.security import PathAllowlist, sanitize_provider_error
from tagger2.tag_manager.api import _split_tag_query, create_tag_manager_router
from tagger2.tag_manager.contracts import (
    BATCH_TAG_MAX_LENGTH,
    FILTER_TAG_MAX_LENGTH,
    BatchOperationRequest,
    CreateDatasetRequest,
    ImageFilter,
    validate_regex_pattern,
)
from tagger2.tag_manager.service import TagManagerError, TagManagerService
from tagger2.tag_manager.storage import TagManagerStore
from tagger2.tag_text import canonical_tag_key, canonical_tag_name


# -- shared fixtures (kept local so this file stays independently portable) --


class FakeTagDatabase:
    def is_loaded(self, profile: str) -> bool:
        return True

    def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
        return None

    def lookup(self, profile: str, tag: str, *, resolve_alias: bool = True):
        return None

    def autocomplete(self, profile: str, query: str, *, limit: int = 20):
        return []

    def available_profiles(self) -> dict[str, list[str]]:
        return {"e621": [], "danbooru": []}


class FakeThumbnails:
    def ensure_thumbnail(self, source: Path, *, size: int, mtime: float) -> Path:
        return source.with_suffix(".thumb.jpg")


COMMA_TAG = "1girl, smile"  # one tag that itself contains a comma
ESCAPED_COMMA_TAG = "1girl\\, smile"  # the query spelling of the tag above


def _make_image(directory: Path, name: str) -> None:
    Image.new("RGB", (8, 8)).save(directory / name)


@pytest.fixture()
def comma_dataset(tmp_path: Path):
    """Dataset with one tags_json sidecar holding a tag containing a comma."""

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    _make_image(dataset, "a.png")
    (dataset / "a.json").write_text(
        json.dumps({"schema": "local-tags-v2", "tags": [{"text": COMMA_TAG}, {"text": "wolf"}]}),
        encoding="utf-8",
    )
    _make_image(dataset, "b.png")
    (dataset / "b.txt").write_text("solo, wolf\n", encoding="utf-8")

    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="test-root", kind="input", writable=True)
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
    )
    session = service.create_session(
        CreateDatasetRequest(root_id="test-root", relative_path="", profile="e621")
    )
    service.index_session(str(session["id"]))
    return service, str(session["id"])


@pytest.fixture()
def client(comma_dataset):
    service, session_id = comma_dataset
    app = FastAPI()
    app.include_router(create_tag_manager_router(service))
    return TestClient(app), session_id


# -- tag query parsing (lossless comma transmission) -------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["solo,long_hair"], ["solo", "long_hair"]),  # legacy joined form
        (["a\\,b"], ["a,b"]),  # escaped comma = one literal comma
        (["a\\\\b"], ["a\\b"]),  # escaped backslash
        (["a\\,b,c"], ["a,b", "c"]),  # escapes inside a joined value
        (["a,b", "c,d"], ["a", "b", "c", "d"]),  # repeated params
        (["  solo ,  wolf  "], ["solo", "wolf"]),  # trims like before
        (["Long_Hair", "BLUE EYES"], ["Long_Hair", "BLUE EYES"]),  # case kept
        (["a\\"], ["a\\"]),  # trailing lone backslash stays verbatim
        ([], []),
        ([""], []),
    ],
)
def test_split_tag_query(raw, expected):
    assert _split_tag_query(raw) == expected


def test_list_images_matches_comma_tag_via_escape(client):
    http, session_id = client
    response = http.get(
        f"/api/v1/tag-manager/datasets/{session_id}/images",
        params={"include_tags": ESCAPED_COMMA_TAG},  # literal: 1girl\, smile
    )
    assert response.status_code == 200
    names = [item["file_name"] for item in response.json()["items"]]
    assert names == ["a.png"]


def test_list_images_legacy_comma_still_splits(client):
    http, session_id = client
    response = http.get(
        f"/api/v1/tag-manager/datasets/{session_id}/images",
        params={"include_tags": "1girl,smile"},  # legacy: two tags
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_list_images_repeated_params_lossless(client):
    http, session_id = client
    response = http.get(
        f"/api/v1/tag-manager/datasets/{session_id}/images",
        params=[("include_tags", ESCAPED_COMMA_TAG), ("include_tags", "wolf")],
    )
    assert response.status_code == 200
    names = [item["file_name"] for item in response.json()["items"]]
    assert names == ["a.png"]


def test_list_images_space_underscore_and_case_canonicalize(client):
    http, session_id = client
    for spelling in ("wolf", "WOLF", " wolf "):
        response = http.get(
            f"/api/v1/tag-manager/datasets/{session_id}/images",
            params={"include_tags": spelling},
        )
        assert response.status_code == 200
        assert [item["file_name"] for item in response.json()["items"]] == ["a.png", "b.png"]


# -- per-tag and total length limits -----------------------------------------


def test_filter_tag_per_item_limit():
    ok = ImageFilter(include_tags=["a" * FILTER_TAG_MAX_LENGTH])
    assert ok.include_tags == ["a" * FILTER_TAG_MAX_LENGTH]
    with pytest.raises(ValidationError):
        ImageFilter(include_tags=["a" * (FILTER_TAG_MAX_LENGTH + 1)])
    with pytest.raises(ValidationError):
        ImageFilter(exclude_tags=["b" * (FILTER_TAG_MAX_LENGTH + 1)])


def test_filter_tag_total_limit():
    # Two lists of 64 tags x 100 chars exceed the combined budget of 8192.
    with pytest.raises(ValidationError):
        ImageFilter(
            include_tags=["a" * FILTER_TAG_MAX_LENGTH] * 64,
            exclude_tags=["b" * FILTER_TAG_MAX_LENGTH] * 64,
        )


def test_batch_tag_per_item_limit():
    with pytest.raises(ValidationError):
        BatchOperationRequest(op="add", tags=["a" * (BATCH_TAG_MAX_LENGTH + 1)], image_ids=[1])
    ok = BatchOperationRequest(op="add", tags=["a" * BATCH_TAG_MAX_LENGTH], image_ids=[1])
    assert ok.tags == ["a" * BATCH_TAG_MAX_LENGTH]


def test_oversized_filter_tag_returns_422(client):
    http, session_id = client
    response = http.get(
        f"/api/v1/tag-manager/datasets/{session_id}/images",
        params={"include_tags": "a" * (FILTER_TAG_MAX_LENGTH + 1)},
    )
    assert response.status_code == 422
    assert "at most 100 characters" in response.text


# -- regex pattern guards -----------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [r"\d+", "a|b", "(?:cat|dog)+", "(a+)?x", "colou?r", r"(?P<name>wolf)_eyes", "a{2,3}", r"^(blue|green)_eyes$"],
)
def test_safe_regex_patterns_accepted(pattern):
    assert validate_regex_pattern(pattern) == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        "(a+)+",  # nested quantifier
        "(?:.*)*",  # nested quantifier, non-capturing
        r"(\w+)+x",  # nested quantifier
        "(a?)+",  # nested quantifier via ?
        "(a{2,})+",  # nested quantifier via bound
        "a{100000}",  # oversized single bound
        "a{10,100000}",  # oversized upper bound
        "(a",  # does not compile
        "a**",  # does not compile
        "x" * 257,  # over the length cap
    ],
)
def test_dangerous_regex_patterns_rejected(pattern):
    with pytest.raises(ValueError):
        validate_regex_pattern(pattern)


def test_batch_regex_guards_return_4xx(client):
    http, session_id = client
    for payload in (
        {"op": "remove", "tags": ["(a+)+"], "use_regex": True, "image_ids": [1]},
        {"op": "replace", "tags": ["(a"], "replacement": "x", "use_regex": True, "image_ids": [1]},
        {"op": "remove", "tags": ["x" * 257], "use_regex": True, "image_ids": [1]},
    ):
        response = http.post(f"/api/v1/tag-manager/datasets/{session_id}/batch", json=payload)
        assert response.status_code == 422, payload


def test_batch_regex_remove_still_works(comma_dataset):
    service, session_id = comma_dataset
    result = service.batch_operation(
        session_id,
        BatchOperationRequest(op="remove", tags=[r"^1girl"], use_regex=True, image_ids=[1]),
    )
    assert result["affected"] == 1


def test_batch_regex_replace_still_works(comma_dataset):
    service, session_id = comma_dataset
    result = service.batch_operation(
        session_id,
        BatchOperationRequest(
            op="replace", tags=["^wolf"], replacement="fox", use_regex=True, image_ids=[2]
        ),
    )
    assert result["affected"] == 1


# -- provider error sanitisation ---------------------------------------------


def test_sanitize_provider_error_redacts_credentials():
    leaking = (
        "HTTP 400 url=https://api.example.com/v1?key=AIzaSyABCDEF1234567890&foo=bar "
        "Authorization: Bearer abcdefghijklmnop sk-abcdef1234567890 xai-abcdef1234567890 "
        "https://user:secretpw@api.example.com"
    )
    text = sanitize_provider_error(RuntimeError(leaking))
    assert "AIzaSyABCDEF1234567890" not in text
    assert "abcdefghijklmnop" not in text
    assert "sk-abcdef1234567890" not in text
    assert "xai-abcdef1234567890" not in text
    assert "secretpw" not in text
    assert "key=***" in text
    assert "Bearer ***" in text
    assert "[redacted]" in text
    assert "api.example.com" in text  # host stays so the log stays useful


def test_sanitize_provider_error_truncates():
    text = sanitize_provider_error(RuntimeError("x" * 5000))
    assert len(text) <= 300


class _ExplodingProvider:
    model = "test-model"

    async def generate(self, **_kwargs):
        raise RuntimeError(
            "429 from https://api.example.com/v1?key=AIzaSyTOPSECRET123456789 - quota exceeded"
        )


def _service_with_provider(provider_factory, tmp_path: Path) -> tuple[TagManagerService, str]:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    allowlist = PathAllowlist()
    allowlist.register(dataset, root_id="p-root", kind="input", writable=False)
    service = TagManagerService(
        store=TagManagerStore(":memory:"),
        allowlist=allowlist,
        thumbnails=FakeThumbnails(),
        tag_database=FakeTagDatabase(),
        provider_factory=provider_factory,
        provider_ids=lambda: ["prov-1"],
    )
    session = service.create_session(
        CreateDatasetRequest(root_id="p-root", relative_path="", profile="e621")
    )
    return service, str(session["id"])


async def test_translate_nl_failure_is_sanitized(tmp_path, caplog):
    from tagger2.tag_manager.contracts import NlTranslateRequest

    def factory(_provider_id):
        raise RuntimeError("connect failed for https://api.example.com?key=AIzaSyTOPSECRET123456789")

    service, _session_id = _service_with_provider(factory, tmp_path)
    with caplog.at_level(logging.WARNING, logger="tagger2.tag_manager"):
        with pytest.raises(TagManagerError) as excinfo:
            await service.translate_nl(NlTranslateRequest(text="hello"))
    assert excinfo.value.code == "nl_translate_unavailable"
    assert excinfo.value.status_code == 409
    message = str(excinfo.value)
    assert message == "在线模型不可用：请检查「Provider 配置」中的地址与密钥，或稍后重试"
    assert "AIzaSyTOPSECRET123456789" not in message
    # details reach the operator log, but not the credential
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "connect failed" in joined
    assert "AIzaSyTOPSECRET123456789" not in joined


async def test_translate_tags_failure_is_sanitized(tmp_path, caplog):
    from tagger2.tag_manager.contracts import TagTranslateRequest

    service, _session_id = _service_with_provider(lambda _pid: _ExplodingProvider(), tmp_path)
    # A tag the offline dictionary cannot resolve forces the model call.
    with caplog.at_level(logging.WARNING, logger="tagger2.tag_manager"):
        with pytest.raises(TagManagerError) as excinfo:
            await service.translate_tags(TagTranslateRequest(tags=["zzxq_no_such_tag_123"]))
    assert excinfo.value.code == "tag_translate_failed"
    message = str(excinfo.value)
    assert message == "翻译失败：在线模型暂时不可用，请稍后重试"
    assert "AIzaSyTOPSECRET123456789" not in message
    assert "AIzaSyTOPSECRET123456789" not in " ".join(r.getMessage() for r in caplog.records)
    assert "quota exceeded" in " ".join(r.getMessage() for r in caplog.records)


# -- shared canonicalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Long Hair", "long_hair"),
        ("long_hair", "long_hair"),
        ("  long_hair  ", "long_hair"),
        ("BLUE EYES", "blue_eyes"),
        ("long  hair", "long__hair"),  # only literal spaces fold, per the SQL mirror
    ],
)
def test_canonical_tag_key(raw, key):
    assert canonical_tag_key(raw) == key
    from tagger2.tag_manager.storage import normalize_tag_key
    from tagger2.tag_manager.translations import normalize_lookup_key

    assert normalize_tag_key(raw) == normalize_lookup_key(raw) == key


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("long_hair", "long hair"),
        ("Long  Hair", "long hair"),
        ("  LONG_hair ", "long hair"),
        ("blue_eyes", "blue eyes"),
    ],
)
def test_canonical_tag_name(raw, name):
    from tagger2.local_inference import normalize_tag_name

    assert canonical_tag_name(raw) == normalize_tag_name(raw) == name
