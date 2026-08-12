"""Token Budget Review: let a human resolve captions that overflow the budget.

The trimming rules live in :mod:`.stages.token_budget` and are byte-identical to
the source project. This module owns only the reviewable state around an
overflow, mirroring :mod:`.count_review`:

* one row per overflowing sample, seeded by the pipeline
* four review actions (``edit``, ``recount``, ``rewrite_short``, ``apply``)
* a proposal column that is never written into the final annotation until an
  explicit ``apply``, so a suggestion cannot silently become the export value
* an explicit gate the pipeline must pass before export

A proposal is recorded once per action and is not re-derived from its own
output, so review cannot loop into repeated rewrites.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from .contracts import utc_now

REVIEW_STATUSES = ("overflow", "edited", "recounted", "rewritten", "applied")
TERMINAL_STATUS = "applied"
ACTION_STATUS = {
    "edit": "edited",
    "recount": "recounted",
    "rewrite_short": "rewritten",
    "apply": TERMINAL_STATUS,
}


class TokenBudgetReviewError(RuntimeError):
    """Raised when token budget review cannot proceed."""


class TokenBudgetReviewConflictError(TokenBudgetReviewError):
    """Raised when a row was modified concurrently."""


class TokenBudgetReviewStore:
    """Persistence for token budget review with optimistic concurrency."""

    def __init__(self, database: Any, job_id: str):
        self.database = database
        self.job_id = job_id

    def initialize(self, entries: Iterable[dict[str, Any]]) -> int:
        """Seed overflow rows. Existing rows are left untouched."""

        written = 0
        now = utc_now()
        with self.database.connection() as conn:
            for entry in entries:
                token_limit = int(entry["token_limit"])
                token_count = int(entry["token_count"])
                if token_limit < 1:
                    raise TokenBudgetReviewError("token_limit must be positive")
                if token_count < 0:
                    raise TokenBudgetReviewError("token_count cannot be negative")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_token_budget_review
                        (job_id, sample_id, nl_text, token_count, token_limit,
                         status, proposal_text, proposal_token_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'overflow', NULL, NULL, ?, ?)
                    """,
                    (
                        self.job_id,
                        int(entry["sample_id"]),
                        str(entry.get("nl_text", "")),
                        token_count,
                        token_limit,
                        now,
                        now,
                    ),
                )
                written += cursor.rowcount or 0
        return written

    def page(self, *, limit: int = 50, offset: int = 0, unresolved_only: bool = False) -> list[dict[str, Any]]:
        query = (
            "SELECT sample_id, nl_text, token_count, token_limit, status,"
            " proposal_text, proposal_token_count, updated_at"
            " FROM workflow_token_budget_review WHERE job_id = ?"
        )
        params: list[Any] = [self.job_id]
        if unresolved_only:
            query += f" AND status != '{TERMINAL_STATUS}'"
        query += " ORDER BY sample_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.database.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        return [
            {
                "sample_id": int(row["sample_id"]),
                "nl_text": row["nl_text"],
                "token_count": int(row["token_count"]),
                "token_limit": int(row["token_limit"]),
                "status": row["status"],
                "proposal_text": row["proposal_text"],
                "proposal_token_count": (
                    None if row["proposal_token_count"] is None else int(row["proposal_token_count"])
                ),
                "over_by": max(0, int(row["token_count"]) - int(row["token_limit"])),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def _row(self, conn: Any, sample_id: int) -> Any:
        row = conn.execute(
            "SELECT nl_text, token_count, token_limit, status, proposal_text,"
            " proposal_token_count, updated_at FROM workflow_token_budget_review"
            " WHERE job_id = ? AND sample_id = ?",
            (self.job_id, sample_id),
        ).fetchone()
        if row is None:
            raise TokenBudgetReviewError(f"no token budget review row for sample {sample_id}")
        return row

    def review(
        self,
        sample_id: int,
        *,
        action: str,
        expected_status: str,
        text: str | None = None,
        count_tokens: Callable[[Sequence[str]], Sequence[int]] | None = None,
    ) -> dict[str, Any]:
        """Record one review action, rejecting a stale status.

        ``edit`` and ``rewrite_short`` store the candidate as a proposal only.
        ``apply`` promotes the stored proposal, and refuses to apply anything
        that still exceeds the budget.
        """

        if action not in ACTION_STATUS:
            raise TokenBudgetReviewError(
                f"action must be one of {tuple(ACTION_STATUS)}, got {action!r}"
            )
        if expected_status not in REVIEW_STATUSES:
            raise TokenBudgetReviewError(f"unsupported expected status: {expected_status!r}")

        now = utc_now()
        with self.database.connection() as conn:
            row = self._row(conn, sample_id)
            current = str(row["status"])
            if current != expected_status:
                raise TokenBudgetReviewConflictError(
                    f"token budget review status conflict: expected {expected_status!r},"
                    f" stored {current!r}"
                )
            if current == TERMINAL_STATUS:
                raise TokenBudgetReviewError(
                    f"sample {sample_id} is already applied and cannot be reviewed again"
                )

            token_limit = int(row["token_limit"])
            nl_text = str(row["nl_text"])
            proposal_text = row["proposal_text"]
            proposal_tokens = (
                None if row["proposal_token_count"] is None else int(row["proposal_token_count"])
            )
            token_count = int(row["token_count"])

            if action in ("edit", "rewrite_short"):
                if text is None or not text.strip():
                    raise TokenBudgetReviewError(f"{action} requires non-empty text")
                proposal_text = text
                proposal_tokens = _count_one(count_tokens, text)
            elif action == "recount":
                # Re-measure the stored text; never rewrite it here.
                target = proposal_text if proposal_text is not None else nl_text
                measured = _count_one(count_tokens, target)
                if proposal_text is None:
                    token_count = measured
                else:
                    proposal_tokens = measured
            else:  # apply
                if proposal_text is None:
                    raise TokenBudgetReviewError(
                        f"sample {sample_id} has no proposal to apply"
                    )
                if proposal_tokens is None:
                    raise TokenBudgetReviewError(
                        f"sample {sample_id} proposal was never counted; recount first"
                    )
                if proposal_tokens > token_limit:
                    raise TokenBudgetReviewError(
                        f"proposal needs {proposal_tokens} tokens and exceeds the"
                        f" budget of {token_limit}"
                    )
                nl_text = proposal_text
                token_count = proposal_tokens

            status = ACTION_STATUS[action]
            conn.execute(
                "UPDATE workflow_token_budget_review"
                " SET nl_text = ?, token_count = ?, status = ?, proposal_text = ?,"
                " proposal_token_count = ?, updated_at = ?"
                " WHERE job_id = ? AND sample_id = ?",
                (
                    nl_text,
                    token_count,
                    status,
                    proposal_text,
                    proposal_tokens,
                    now,
                    self.job_id,
                    sample_id,
                ),
            )

            # Keep the reviewed NL value in the job-local overlay as soon as
            # it is applied.  The target dataset remains untouched until the
            # orchestrator observes that every review row is terminal.
            if status == TERMINAL_STATUS:
                self._write_overlay(sample_id, nl_text)

        return {
            "sample_id": sample_id,
            "status": status,
            "nl_text": nl_text,
            "token_count": token_count,
            "token_limit": token_limit,
            "proposal_text": proposal_text,
            "proposal_token_count": proposal_tokens,
        }

    def _write_overlay(self, sample_id: int, text: str) -> None:
        job = self.database.get_job(self.job_id)
        if not job:
            return
        path = Path(str(job["workspace_path"])) / "review_overlay.json"
        payload: dict[str, Any] = {"count": {}, "nl": {}}
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload.update({key: value for key, value in raw.items() if isinstance(value, dict)})
        except (OSError, json.JSONDecodeError):
            payload = {"count": {}, "nl": {}}
        payload.setdefault("nl", {})[str(sample_id)] = text
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def unresolved_count(self) -> int:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS unresolved FROM workflow_token_budget_review"
                f" WHERE job_id = ? AND status != '{TERMINAL_STATUS}'",
                (self.job_id,),
            ).fetchone()
        return int(row["unresolved"])

    def applied_texts(self) -> dict[int, str]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT sample_id, nl_text FROM workflow_token_budget_review"
                f" WHERE job_id = ? AND status = '{TERMINAL_STATUS}'",
                (self.job_id,),
            ).fetchall()
        return {int(row["sample_id"]): str(row["nl_text"]) for row in rows}

    def assert_ready_for_export(self) -> None:
        """Fail closed while any overflow is unresolved."""

        unresolved = self.unresolved_count()
        if unresolved:
            raise TokenBudgetReviewError(
                f"token budget review is incomplete: {unresolved} sample(s) still over budget"
            )


def _count_one(
    count_tokens: Callable[[Sequence[str]], Sequence[int]] | None, text: str
) -> int:
    if count_tokens is None:
        raise TokenBudgetReviewError("a tokenizer is required to count a caption")
    counts = list(count_tokens([text]))
    if len(counts) != 1 or type(counts[0]) is not int or counts[0] < 0:
        raise TokenBudgetReviewError("tokenizer returned an invalid token count")
    return counts[0]


__all__ = [
    "ACTION_STATUS",
    "REVIEW_STATUSES",
    "TERMINAL_STATUS",
    "TokenBudgetReviewConflictError",
    "TokenBudgetReviewError",
    "TokenBudgetReviewStore",
]
