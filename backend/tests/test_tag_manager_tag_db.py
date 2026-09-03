"""Tests for the tag manager tag database over classify-snapshot resources."""

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from tagger2.tag_manager.tag_db import TagDatabase, TagDatabaseError
from tagger2.workflow.resources import WorkflowResourceCatalog


def _document(profile: str = "e621") -> dict[str, Any]:
    """A small synthetic classify-snapshot-v1 bundle (no network, no CSVs)."""

    return {
        "format": "classify-snapshot-v1",
        "profile": profile,
        "source": {},
        "tags": [
            {"name": "solo", "category": "general", "post_count": 100},
            {"name": "horse", "category": "general", "post_count": 500},
            {"name": "horn", "category": "general", "post_count": 500},
            {"name": "hot_dog", "category": "general", "post_count": 900},
            {"name": "house", "category": "general", "post_count": 10},
            {"name": "hatsune_miku", "category": "character", "post_count": 80},
        ],
        "aliases": [{"antecedent_name": "1girl", "consequent_name": "solo"}],
        "implications": [],
    }


def _memory_db(
    tmp_path: Path,
    document: dict[str, Any],
    *,
    resource_id: str = "test-snapshot-v1",
    scope: str = "main",
    load_log: list[str] | None = None,
) -> TagDatabase:
    """A TagDatabase whose snapshot loading is replaced by an in-memory dict.

    Each stub gets its own catalog directory so the process-level cache cannot
    leak an index between stubs inside one test.
    """

    class _StubDatabase(TagDatabase):
        def __init__(self) -> None:
            super().__init__(WorkflowResourceCatalog(tmp_path / f"resources-{scope}"))
            self._document = document

        def _load_snapshot(self, profile: str, resource_id: str) -> dict[str, Any]:
            if load_log is not None:
                load_log.append(resource_id)
            return self._document

    return _StubDatabase()


def _register_snapshot(
    catalog: WorkflowResourceCatalog,
    tmp_path: Path,
    resource_id: str,
    profile: str,
    document: dict[str, Any],
) -> None:
    staged = tmp_path / f"{resource_id}.json"
    staged.write_text(json.dumps(document), encoding="utf-8")
    catalog.import_resource(
        source_path=staged, resource_id=resource_id, category="classify", profile=profile
    )


def test_lookup_returns_tag_info(tmp_path: Path):
    """A canonical tag resolves to its name, category and post count."""

    db = _memory_db(tmp_path, _document())
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    info = db.lookup("e621", "hatsune_miku")
    assert info == {
        "name": "hatsune_miku",
        "category": "character",
        "post_count": 80,
        "alias_of": None,
    }
    assert db.lookup("e621", "totally_unknown_tag") is None


def test_lookup_resolves_aliases_with_alias_of(tmp_path: Path):
    """An alias antecedent resolves to the canonical tag and records itself."""

    db = _memory_db(tmp_path, _document())
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    info = db.lookup("e621", "1girl")
    assert info is not None
    assert info["name"] == "solo"
    assert info["alias_of"] == "1girl"
    assert info["post_count"] == 100

    # The canonical tag itself is not marked as an alias, and alias resolution
    # can be switched off (an antecedent alone has no canonical entry).
    assert db.lookup("e621", "solo")["alias_of"] is None
    assert db.lookup("e621", "1girl", resolve_alias=False) is None


def test_lookup_and_autocomplete_are_case_insensitive(tmp_path: Path):
    """Case only matters for display; matching is casefolded."""

    db = _memory_db(tmp_path, _document())
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    assert db.lookup("e621", "SOLO") is not None
    assert db.lookup("e621", "Hatsune_Miku")["name"] == "hatsune_miku"
    assert db.lookup("e621", "1GIRL")["name"] == "solo"
    assert [info["name"] for info in db.autocomplete("e621", "HO")] == [
        "hot_dog",
        "horn",
        "horse",
        "house",
    ]


def test_autocomplete_orders_by_post_count_then_name(tmp_path: Path):
    """Best post_count first; equal counts are ordered alphabetically."""

    db = _memory_db(tmp_path, _document())
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    infos = db.autocomplete("e621", "ho")
    assert [(info["name"], info["post_count"]) for info in infos] == [
        ("hot_dog", 900),
        ("horn", 500),
        ("horse", 500),
        ("house", 10),
    ]


def test_autocomplete_limit_and_empty_query(tmp_path: Path):
    """A limit truncates the ranked list; blank queries match nothing."""

    db = _memory_db(tmp_path, _document())
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    assert [info["name"] for info in db.autocomplete("e621", "ho", limit=2)] == [
        "hot_dog",
        "horn",
    ]
    assert db.autocomplete("e621", "", limit=5) == []
    assert db.autocomplete("e621", "   ") == []
    assert db.autocomplete("e621", "zzz_no_prefix_match") == []


def test_autocomplete_hides_alias_antecedents(tmp_path: Path):
    """Alias antecedents resolve into their canonical row, never a second one."""

    db = _memory_db(tmp_path, _document())
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    assert db.autocomplete("e621", "1g") == []
    assert [info["name"] for info in db.autocomplete("e621", "so")] == ["solo"]


def test_alias_chains_flatten_to_the_final_target(tmp_path: Path):
    """Chained aliases resolve like the classify stage's flattened rules."""

    document = _document()
    document["tags"] = [{"name": "cc", "category": "general", "post_count": 7}]
    document["aliases"] = [
        {"antecedent_name": "aa", "consequent_name": "bb"},
        {"antecedent_name": "bb", "consequent_name": "cc"},
    ]
    db = _memory_db(tmp_path, document, scope="chain")
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    info = db.lookup("e621", "aa")
    assert info is not None
    assert info["name"] == "cc"
    assert info["alias_of"] == "aa"
    assert db.lookup("e621", "bb")["name"] == "cc"


def test_ensure_loaded_without_resource_raises(tmp_path: Path):
    """No registered snapshot for the profile is a fail-closed error."""

    db = TagDatabase(WorkflowResourceCatalog(tmp_path / "resources-empty"))
    with pytest.raises(TagDatabaseError) as excinfo:
        db.ensure_loaded("e621")
    assert isinstance(excinfo.value, ValueError)
    assert "e621" in str(excinfo.value)


def test_ensure_loaded_rejects_unknown_profile(tmp_path: Path):
    """Only profiles with a declared category table are accepted."""

    db = TagDatabase(WorkflowResourceCatalog(tmp_path / "resources-profile"))
    with pytest.raises(TagDatabaseError):
        db.ensure_loaded("not_a_profile")


def test_ensure_loaded_is_lazy_and_cached(tmp_path: Path):
    """The snapshot loads on first use only; later calls reuse the index."""

    load_log: list[str] = []
    db = _memory_db(tmp_path, _document(), load_log=load_log)

    assert db.is_loaded("e621") is False
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")
    assert db.is_loaded("e621") is True
    db.ensure_loaded("e621")
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    assert load_log == ["test-snapshot-v1"]


def test_ensure_loaded_with_different_resource_id_reloads(tmp_path: Path):
    """An explicit resource id that differs from the loaded one replaces it."""

    documents = {
        "snap-a": _document(),
        "snap-b": {
            **_document(),
            "tags": [{"name": "solo", "category": "general", "post_count": 555}],
        },
    }

    class _SwitchingDatabase(TagDatabase):
        def __init__(self) -> None:
            super().__init__(WorkflowResourceCatalog(tmp_path / "resources-reload"))

        def _load_snapshot(self, profile: str, resource_id: str) -> dict[str, Any]:
            return documents[resource_id]

    db = _SwitchingDatabase()
    db.ensure_loaded("e621", resource_id="snap-a")
    assert db.lookup("e621", "solo")["post_count"] == 100

    db.ensure_loaded("e621", resource_id="snap-b")
    assert db.lookup("e621", "solo")["post_count"] == 555


def test_concurrent_ensure_loaded_loads_once(tmp_path: Path):
    """The loading lock serializes threads so the snapshot is read once."""

    load_log: list[str] = []
    db = _memory_db(tmp_path, _document(), load_log=load_log)
    barrier = threading.Barrier(4)
    failures: list[Exception] = []

    def _worker() -> None:
        barrier.wait()
        try:
            db.ensure_loaded("e621", resource_id="test-snapshot-v1")
        except Exception as exc:  # pragma: no cover - collected below
            failures.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert load_log == ["test-snapshot-v1"]


def test_available_profiles_lists_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Classify resources are grouped per profile, newest creation first."""

    import tagger2.workflow.resources as resources_module

    stamps = iter(
        [
            "2026-08-11T00:00:00Z",
            "2026-08-12T00:00:00Z",
            "2026-08-12T00:00:00Z",
        ]
    )
    monkeypatch.setattr(resources_module, "utc_now", lambda: next(stamps))

    catalog = WorkflowResourceCatalog(tmp_path / "resources-catalog")
    _register_snapshot(
        catalog, tmp_path, "classify-e621-20260811-v1", "e621", _document()
    )
    newer = _document()
    newer["tags"] = [{"name": "solo", "category": "general", "post_count": 42}]
    _register_snapshot(
        catalog, tmp_path, "classify-e621-20260812-v1", "e621", newer
    )

    db = TagDatabase(catalog)
    assert db.available_profiles() == {
        "e621": ["classify-e621-20260812-v1", "classify-e621-20260811-v1"]
    }


def test_loads_snapshot_through_the_real_catalog(tmp_path: Path):
    """The default loading path reads the content-addressed resource file."""

    catalog = WorkflowResourceCatalog(tmp_path / "resources-real")
    _register_snapshot(
        catalog, tmp_path, "classify-e621-20260901-v1", "e621", _document()
    )

    db = TagDatabase(catalog)
    db.ensure_loaded("e621")

    assert db.lookup("e621", "1girl")["name"] == "solo"
    assert [info["name"] for info in db.autocomplete("e621", "ho")] == [
        "hot_dog",
        "horn",
        "horse",
        "house",
    ]


def test_implications_of_forward_and_reverse(tmp_path: Path):
    """implications_of returns resolved implied or implying tags ordered by post_count."""

    doc = _document()
    doc["tags"].extend([
        {"name": "canine", "category": "species", "post_count": 2000},
        {"name": "dog", "category": "species", "post_count": 800},
        {"name": "corgi", "category": "species", "post_count": 150},
        {"name": "mammal", "category": "species", "post_count": 5000},
    ])
    doc["aliases"].append({"antecedent_name": "puppy", "consequent_name": "dog"})
    doc["implications"] = [
        {"antecedent_name": "corgi", "consequent_name": "puppy"},  # implies alias -> dog
        {"antecedent_name": "dog", "consequent_name": "canine"},
        {"antecedent_name": "canine", "consequent_name": "mammal"},
        {"antecedent_name": "corgi", "consequent_name": "corgi"},  # self-implication skipped
    ]

    db = _memory_db(tmp_path, doc, scope="implications")
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    # Forward: corgi implies dog (via puppy alias)
    corgi_imp = db.implications_of("e621", "corgi")
    assert [t["name"] for t in corgi_imp] == ["dog"]

    # Reverse: dog is implied by corgi (via puppy alias)
    dog_rev = db.implications_of("e621", "dog", reverse=True)
    assert [t["name"] for t in dog_rev] == ["corgi"]

    # Reverse with alias antecedent: looking up puppy in reverse resolves to dog
    puppy_rev = db.implications_of("e621", "puppy", reverse=True)
    assert [t["name"] for t in puppy_rev] == ["corgi"]

    # Unknown tag returns []
    assert db.implications_of("e621", "unknown_tag") == []


def test_top_tags(tmp_path: Path):
    """top_tags returns canonical tags filtered by min_post_count and sorted by post_count desc."""

    doc = _document()
    db = _memory_db(tmp_path, doc, scope="top_tags")
    db.ensure_loaded("e621", resource_id="test-snapshot-v1")

    # min_post_count=100
    top = db.top_tags("e621", min_post_count=100)
    assert [(t["name"], t["post_count"]) for t in top] == [
        ("hot_dog", 900),
        ("horn", 500),
        ("horse", 500),
        ("solo", 100),
    ]

    # limit=2
    top_limit = db.top_tags("e621", min_post_count=0, limit=2)
    assert len(top_limit) == 2
    assert top_limit[0]["name"] == "hot_dog"
    assert top_limit[1]["name"] == "horn"

