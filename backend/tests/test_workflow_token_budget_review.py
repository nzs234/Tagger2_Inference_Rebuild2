"""Tests for Token Budget Review: the reviewable state around an overflow.

The trimming rules themselves are covered by test_workflow_policy_budget.py.
These tests pin the review contract: proposals never become the export value
without an explicit apply, stale writes are rejected, and export is gated.
"""

import tempfile
from pathlib import Path

import pytest


def _store(tmpdir):
    from backend.tagger2.workflow.db import WorkflowDatabase
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewStore

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
    return database, job_id, TokenBudgetReviewStore(database, job_id)


def _counter(mapping):
    """A deterministic stand-in for the real tokenizer."""

    def count_tokens(texts):
        return [mapping.get(text, len(text.split())) for text in texts]

    return count_tokens


def _seed(store, **overrides):
    entry = {"sample_id": 0, "nl_text": "a b c d e", "token_count": 5, "token_limit": 3}
    entry.update(overrides)
    return store.initialize([entry])


def test_seeding_is_idempotent_and_reports_the_overflow_margin():
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        assert _seed(store) == 1
        # Re-seeding must not clobber review progress already recorded.
        assert _seed(store) == 0

        (row,) = store.page()
        assert row["status"] == "overflow"
        assert row["over_by"] == 2
        assert row["proposal_text"] is None
        assert store.unresolved_count() == 1


def test_edit_records_a_proposal_without_changing_the_export_value():
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)

        result = store.review(
            0,
            action="edit",
            expected_status="overflow",
            text="a b",
            count_tokens=_counter({"a b": 2}),
        )
        assert result["status"] == "edited"
        assert result["proposal_text"] == "a b"
        assert result["proposal_token_count"] == 2
        # The suggestion must not become the caption until it is applied.
        assert result["nl_text"] == "a b c d e"
        assert store.applied_texts() == {}
        assert store.unresolved_count() == 1


def test_apply_promotes_the_proposal_and_clears_the_gate():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)
        store.review(
            0,
            action="edit",
            expected_status="overflow",
            text="a b",
            count_tokens=_counter({"a b": 2}),
        )

        applied = store.review(0, action="apply", expected_status="edited")
        assert applied["status"] == "applied"
        assert applied["nl_text"] == "a b"
        assert applied["token_count"] == 2
        assert store.applied_texts() == {0: "a b"}
        assert store.unresolved_count() == 0
        store.assert_ready_for_export()

        # An applied row is terminal, so review cannot loop back over it.
        with pytest.raises(TokenBudgetReviewError, match="already applied"):
            store.review(0, action="apply", expected_status="applied")


def test_apply_refuses_a_proposal_that_still_exceeds_the_budget():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)
        store.review(
            0,
            action="rewrite_short",
            expected_status="overflow",
            text="a b c d",
            count_tokens=_counter({"a b c d": 4}),
        )

        with pytest.raises(TokenBudgetReviewError, match="exceeds the budget"):
            store.review(0, action="apply", expected_status="rewritten")
        # The row stays unresolved rather than exporting an over-budget caption.
        assert store.unresolved_count() == 1
        assert store.applied_texts() == {}


def test_apply_requires_a_counted_proposal():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)

        with pytest.raises(TokenBudgetReviewError, match="no proposal"):
            store.review(0, action="apply", expected_status="overflow")


def test_recount_remeasures_without_rewriting_the_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)

        result = store.review(
            0,
            action="recount",
            expected_status="overflow",
            count_tokens=_counter({"a b c d e": 9}),
        )
        assert result["status"] == "recounted"
        assert result["nl_text"] == "a b c d e"
        assert result["token_count"] == 9
        assert result["proposal_text"] is None


def test_stale_status_is_rejected():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewConflictError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)
        store.review(
            0,
            action="edit",
            expected_status="overflow",
            text="a b",
            count_tokens=_counter({"a b": 2}),
        )

        # A second reviewer still holding the original status must lose.
        with pytest.raises(TokenBudgetReviewConflictError, match="status conflict"):
            store.review(
                0,
                action="edit",
                expected_status="overflow",
                text="a",
                count_tokens=_counter({"a": 1}),
            )


def test_export_gate_blocks_while_any_sample_is_unresolved():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        store.initialize(
            [
                {"sample_id": 0, "nl_text": "a b c d e", "token_count": 5, "token_limit": 3},
                {"sample_id": 1, "nl_text": "f g h", "token_count": 3, "token_limit": 2},
            ]
        )
        store.review(
            0,
            action="edit",
            expected_status="overflow",
            text="a b",
            count_tokens=_counter({"a b": 2}),
        )
        store.review(0, action="apply", expected_status="edited")

        with pytest.raises(TokenBudgetReviewError, match="still over budget"):
            store.assert_ready_for_export()
        assert store.unresolved_count() == 1
        # Paging can focus on what still needs a decision.
        assert [row["sample_id"] for row in store.page(unresolved_only=True)] == [1]


def test_invalid_actions_and_empty_text_are_refused():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        _seed(store)

        with pytest.raises(TokenBudgetReviewError, match="action must be one of"):
            store.review(0, action="truncate", expected_status="overflow")
        with pytest.raises(TokenBudgetReviewError, match="requires non-empty text"):
            store.review(
                0,
                action="edit",
                expected_status="overflow",
                text="   ",
                count_tokens=_counter({}),
            )
        with pytest.raises(TokenBudgetReviewError, match="tokenizer is required"):
            store.review(0, action="edit", expected_status="overflow", text="a b")
        with pytest.raises(TokenBudgetReviewError, match="no token budget review row"):
            store.review(99, action="recount", expected_status="overflow")


def test_rejects_a_nonpositive_budget_and_negative_count():
    from backend.tagger2.workflow.token_budget_review import TokenBudgetReviewError

    with tempfile.TemporaryDirectory() as tmpdir:
        _database, _job_id, store = _store(tmpdir)
        with pytest.raises(TokenBudgetReviewError, match="token_limit must be positive"):
            _seed(store, token_limit=0)
        with pytest.raises(TokenBudgetReviewError, match="cannot be negative"):
            _seed(store, token_count=-1)
