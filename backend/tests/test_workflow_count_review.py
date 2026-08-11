"""Tests for Count Review: ported rules plus the reviewable state around them."""

import tempfile
from pathlib import Path

import pytest


class Sample:
    def __init__(self, sample_id, path):
        self.sample_id = sample_id
        self.relative_image_path = path


def _wiki(tmpdir, entries=None):
    from backend.tagger2.workflow.count_review import create_wiki_catalog

    return create_wiki_catalog(Path(tmpdir) / "wiki.sqlite3", entries)


def test_original_count_normalization_matches_source_rules():
    """The ported normalizer handles aliases, numbers, sums and lower bounds."""
    from backend.tagger2.workflow.count_review import normalize_original_count

    assert normalize_original_count("solo") == "solo"
    assert normalize_original_count("couple") == "duo"
    assert normalize_original_count(3) == "trio"
    assert normalize_original_count(9) == "group"
    assert normalize_original_count("2 girls and 1 boy") == "trio"
    assert normalize_original_count("6+ girls") == "group"
    # Unparseable and contradictory values are rejected, not guessed.
    assert normalize_original_count("banana") is None
    assert normalize_original_count("") is None
    assert normalize_original_count(True) is None
    assert normalize_original_count(0) is None


def test_missing_wiki_snapshot_degrades_to_original_value():
    """Without the wiki snapshot, count tags warn and the original value is used."""
    from backend.tagger2.workflow.count_review import derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "duo", "tags": ["solo", "male"], "character": ""}},
            wiki_db_path=_wiki(tmpdir),
        )
        item = evidence[0]
        # `solo` is a count tag but is unverified, so it cannot override.
        assert item.wiki_value is None
        assert any(w.startswith("wiki_missing") for w in item.warnings)
        assert item.proposed_count == "duo"
        assert item.selected_source == "original_json"


def test_verified_wiki_tag_supplies_count():
    """A count tag present in the catalog resolves a count."""
    from backend.tagger2.workflow.count_review import derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        wiki = _wiki(tmpdir, {"trio": "three characters"})
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "", "tags": ["trio"], "character": ""}},
            wiki_db_path=wiki,
        )
        item = evidence[0]
        assert item.wiki_value == "trio"
        assert item.proposed_count == "trio"
        assert item.selected_source == "wiki_tags"


def test_conflict_between_original_and_wiki_is_reported():
    """A disagreement is surfaced rather than silently resolved."""
    from backend.tagger2.workflow.count_review import derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        wiki = _wiki(tmpdir, {"solo": "one character"})
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "duo", "tags": ["solo"], "character": ""}},
            wiki_db_path=wiki,
        )
        item = evidence[0]
        assert item.conflict is True
        assert "count_source_conflict" in item.issue_codes
        # Without overwrite_count the original annotation wins.
        assert item.proposed_count == "duo"

        overwritten = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "duo", "tags": ["solo"], "character": ""}},
            wiki_db_path=wiki,
            overwrite_count=True,
        )[0]
        # With overwrite_count the verified wiki tag wins over the original value,
        # and with no character identities nothing raises a lower bound.
        assert overwritten.selected_source == "wiki_tags"
        assert overwritten.proposed_count == "solo"


def test_character_count_raises_lower_bound():
    """Multiple canonical characters raise a solo base to duo."""
    from backend.tagger2.workflow.count_review import derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "solo", "tags": [], "character": "rex, blaze"}},
            wiki_db_path=_wiki(tmpdir),
        )
        item = evidence[0]
        assert item.proposed_count == "duo"
        assert "character" in item.applied_lower_bounds
        assert "count_character_lower_bound" in item.issue_codes


def test_sheet_layout_with_single_character_blocks():
    """A character sheet claiming duo with one identity is a blocking conflict."""
    from backend.tagger2.workflow.count_review import derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {
                "a.png": {
                    "count": "duo",
                    "tags": ["character_sheet", "multiple_views"],
                    "character": "rex",
                }
            },
            wiki_db_path=_wiki(tmpdir),
        )
        item = evidence[0]
        assert item.blocking_code == "count_sheet_multi_conflict"
        assert "count_sheet_multi_conflict" in item.issue_codes


def _store(tmpdir):
    from backend.tagger2.workflow.count_review import CountReviewStore
    from backend.tagger2.workflow.db import WorkflowDatabase

    database = WorkflowDatabase(Path(tmpdir) / "workflows.sqlite3")
    job_id, _workspace = database.create_job(
        config_json={},
        config_hash="h",
        profile="e621",
        work_mode="full_copy",
        overwrite_mode="incremental",
        source_root_id="in",
        output_root_id="out",
        workspace_root=Path(tmpdir) / "jobs",
    )
    database.create_sample(job_id, 0, "a.png", "png")
    database.create_sample(job_id, 1, "b.png", "png")
    return database, job_id, CountReviewStore(database, job_id)


def test_review_store_seeds_pending_decisions_and_gates_export():
    """Export is blocked until every decision is reviewed."""
    from backend.tagger2.workflow.count_review import CountReviewError, derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        database, job_id, store = _store(tmpdir)
        evidence = derive_count_decisions(
            [Sample(0, "a.png"), Sample(1, "b.png")],
            {
                "a.png": {"count": "solo", "tags": [], "character": ""},
                "b.png": {"count": "", "tags": [], "character": ""},
            },
            wiki_db_path=_wiki(tmpdir),
        )
        assert store.initialize(evidence) == 2
        # Re-seeding is idempotent and never overwrites a reviewed decision.
        assert store.initialize(evidence) == 0

        assert store.pending_count() == 2
        with pytest.raises(CountReviewError):
            store.assert_ready_for_export()

        page = store.page(limit=10)
        assert [item["sample_id"] for item in page] == [0, 1]
        assert page[0]["proposed_count"] == "solo"
        # A sample with no evidence at all is stored as unknown, not guessed.
        assert page[1]["count_value"] == "unknown"

        store.resolve(0, expected_version=1, count="solo")
        store.resolve(1, expected_version=1, count="group")
        assert store.pending_count() == 0
        store.assert_ready_for_export()
        assert store.confirmed_counts() == {0: "solo", 1: "group"}


def test_review_store_rejects_stale_version():
    """A concurrent edit is refused instead of overwriting the newer decision."""
    from backend.tagger2.workflow.count_review import (
        CountReviewConflictError,
        derive_count_decisions,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "solo", "tags": [], "character": ""}},
            wiki_db_path=_wiki(tmpdir),
        )
        store.initialize(evidence)

        store.resolve(0, expected_version=1, count="duo")
        with pytest.raises(CountReviewConflictError):
            store.resolve(0, expected_version=1, count="trio")

        # The newer version is still accepted.
        result = store.resolve(0, expected_version=2, count="trio")
        assert result["count_value"] == "trio"
        assert result["version"] == 3


def test_review_store_validates_count_and_source():
    from backend.tagger2.workflow.count_review import CountReviewError, derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        store.initialize(
            derive_count_decisions(
                [Sample(0, "a.png")],
                {"a.png": {"count": "solo", "tags": [], "character": ""}},
                wiki_db_path=_wiki(tmpdir),
            )
        )

        with pytest.raises(CountReviewError):
            store.resolve(0, expected_version=1, count="many")
        with pytest.raises(CountReviewError):
            store.resolve(0, expected_version=1, count="solo", source="telepathy")
        with pytest.raises(CountReviewError):
            store.resolve(99, expected_version=1, count="solo")


def test_nl_observation_is_attached_but_not_authoritative():
    """The NL observation is evidence for the reviewer, not an automatic decision."""
    from backend.tagger2.workflow.count_review import derive_count_decisions

    with tempfile.TemporaryDirectory() as tmpdir:
        evidence = derive_count_decisions(
            [Sample(0, "a.png")],
            {"a.png": {"count": "solo", "tags": [], "character": ""}},
            wiki_db_path=_wiki(tmpdir),
            observations={"a.png": {"status": "observed", "countValue": "group"}},
        )
        item = evidence[0]
        assert item.nl_observation["countValue"] == "group"
        # The model's opinion did not change the proposed count.
        assert item.proposed_count == "solo"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
