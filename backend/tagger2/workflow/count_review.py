"""Count Review: derive count decisions and let a human confirm them.

Count values come from the ported rule engine in
:mod:`.stages.count_rules`, which is byte-identical to the source project. This
module owns the reviewable state around it:

* a wiki catalog, which may be empty (every count tag then reports
  ``wiki_missing`` and the rules degrade to the original annotation value)
* per-sample decisions persisted with an optimistic-concurrency version
* an explicit confirmation gate that the pipeline must pass before export
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .contracts import canonical_json, utc_now
from .stages.count_rules import (
    COUNT_RANK,
    CountDecision,
    WikiCountResolver,
    decide_count,
    normalize_original_count,
)

COUNT_VALUES = ("solo", "duo", "trio", "group")
DECISION_SOURCES = ("rules", "original_json", "nl_observation", "manual")


class CountReviewError(RuntimeError):
    """Raised when count review cannot proceed."""


class CountReviewConflictError(CountReviewError):
    """Raised when a decision was modified concurrently."""


def create_wiki_catalog(db_path: Path, entries: dict[str, str] | None = None) -> Path:
    """Create a wiki catalog database for the count resolver.

    An empty catalog is valid and is the honest default here: without the private
    e621 wiki snapshot every count tag reports ``wiki_missing`` and the rules fall
    back to the original annotation instead of inventing a count.
    """

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS wiki_catalog (title TEXT PRIMARY KEY, body TEXT)"
        )
        for title, body in (entries or {}).items():
            connection.execute(
                "INSERT OR REPLACE INTO wiki_catalog (title, body) VALUES (?, ?)",
                (title, body),
            )
        connection.commit()
    finally:
        connection.close()
    return db_path


@dataclass(frozen=True)
class CountEvidence:
    """Everything a reviewer needs to judge one count decision."""

    sample_id: int
    relative_image_path: str
    proposed_count: str
    base_value: str
    selected_source: str
    original_raw: Any
    original_normalized: str | None
    wiki_value: str | None
    matched_tags: tuple[str, ...]
    conflict: bool
    issue_codes: tuple[str, ...]
    warnings: tuple[str, ...]
    applied_lower_bounds: tuple[str, ...]
    blocking_code: str | None
    nl_observation: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "relative_image_path": self.relative_image_path,
            "proposed_count": self.proposed_count,
            "base_value": self.base_value,
            "selected_source": self.selected_source,
            "original_raw": self.original_raw,
            "original_normalized": self.original_normalized,
            "wiki_value": self.wiki_value,
            "matched_tags": list(self.matched_tags),
            "conflict": self.conflict,
            "issue_codes": list(self.issue_codes),
            "warnings": list(self.warnings),
            "applied_lower_bounds": list(self.applied_lower_bounds),
            "blocking_code": self.blocking_code,
            "nl_observation": dict(self.nl_observation),
        }


def _decision_from(decision: CountDecision, sample: Any, observation: dict[str, Any]) -> CountEvidence:
    return CountEvidence(
        sample_id=sample.sample_id,
        relative_image_path=sample.relative_image_path,
        proposed_count=decision.value,
        base_value=decision.base_value,
        selected_source=decision.selected_source,
        original_raw=decision.original_raw,
        original_normalized=decision.original_normalized,
        wiki_value=decision.wiki_value,
        matched_tags=tuple(decision.matched_tags),
        conflict=bool(decision.conflict),
        issue_codes=tuple(decision.issue_codes),
        warnings=tuple(decision.warnings),
        applied_lower_bounds=tuple(decision.applied_lower_bounds),
        blocking_code=decision.blocking_code,
        nl_observation=dict(observation),
    )


def derive_count_decisions(
    samples: Sequence[Any],
    projections: dict[str, dict[str, Any]],
    *,
    wiki_db_path: Path,
    observations: dict[str, dict[str, Any]] | None = None,
    overwrite_count: bool = False,
) -> list[CountEvidence]:
    """Run the ported count rules over every sample."""

    observations = observations or {}
    connection = sqlite3.connect(Path(wiki_db_path))
    try:
        resolver = WikiCountResolver(connection)
        evidence: list[CountEvidence] = []
        for sample in samples:
            relative = sample.relative_image_path
            projection = projections.get(relative, {})
            tags = tuple(projection.get("tags", ()))
            character = str(projection.get("character", ""))
            character_ids = tuple(
                part.strip() for part in character.split(",") if part.strip()
            )
            decision = decide_count(
                projection.get("count") or None,
                tags,
                character_ids,
                tags,
                resolver,
                overwrite_count,
            )
            evidence.append(_decision_from(decision, sample, observations.get(relative, {})))
        return evidence
    finally:
        connection.close()


class CountReviewStore:
    """Persistence for count review decisions with optimistic concurrency."""

    def __init__(self, database: Any, job_id: str):
        self.database = database
        self.job_id = job_id

    def initialize(self, evidence: Iterable[CountEvidence]) -> int:
        """Seed pending decisions. Existing rows are left untouched."""

        written = 0
        now = utc_now()
        with self.database.connection() as conn:
            for item in evidence:
                payload = dict(item.as_dict())
                payload["version"] = 1
                payload["decided_source"] = "rules"
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_count_review
                        (job_id, sample_id, count_value, status, decision_json, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        self.job_id,
                        item.sample_id,
                        item.proposed_count or "unknown",
                        canonical_json(payload),
                        now,
                        now,
                    ),
                )
                written += cursor.rowcount or 0
        return written

    def page(self, *, limit: int = 50, offset: int = 0, pending_only: bool = False) -> list[dict[str, Any]]:
        query = (
            "SELECT sample_id, count_value, status, decision_json, updated_at"
            " FROM workflow_count_review WHERE job_id = ?"
        )
        params: list[Any] = [self.job_id]
        if pending_only:
            query += " AND status = 'pending'"
        query += " ORDER BY sample_id LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self.database.connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            decision = json.loads(row["decision_json"])
            items.append(
                {
                    "sample_id": row["sample_id"],
                    "count_value": row["count_value"],
                    "status": row["status"],
                    "updated_at": row["updated_at"],
                    **decision,
                }
            )
        return items

    def resolve(
        self,
        sample_id: int,
        *,
        expected_version: int,
        count: str,
        source: str = "manual",
    ) -> dict[str, Any]:
        """Record a reviewed count, rejecting a stale version."""

        if count not in COUNT_VALUES:
            raise CountReviewError(f"count must be one of {COUNT_VALUES}, got {count!r}")
        if source not in DECISION_SOURCES:
            raise CountReviewError(f"unsupported decision source: {source!r}")

        now = utc_now()
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT decision_json FROM workflow_count_review"
                " WHERE job_id = ? AND sample_id = ?",
                (self.job_id, sample_id),
            ).fetchone()
            if row is None:
                raise CountReviewError(f"no count review row for sample {sample_id}")

            decision = json.loads(row["decision_json"])
            current_version = int(decision.get("version", 1))
            if current_version != expected_version:
                raise CountReviewConflictError(
                    f"count review version conflict: expected {expected_version},"
                    f" stored {current_version}"
                )

            decision["version"] = current_version + 1
            decision["decided_source"] = source
            decision["decided_count"] = count
            conn.execute(
                "UPDATE workflow_count_review"
                " SET count_value = ?, status = 'confirmed', decision_json = ?, updated_at = ?"
                " WHERE job_id = ? AND sample_id = ?",
                (count, canonical_json(decision), now, self.job_id, sample_id),
            )
        self._write_overlay(sample_id, count)
        return {"sample_id": sample_id, "count_value": count, "version": decision["version"]}

    def _write_overlay(self, sample_id: int, count: str) -> None:
        """Persist the reviewed value in the private job workspace.

        The database row remains authoritative, but a small JSON overlay makes
        the checkpoint inspectable and lets recovery tools rebuild staged output
        without exposing server paths through the API.
        """

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
            # A malformed private artifact is not allowed to corrupt the
            # authoritative review transaction; the next pipeline run rewrites
            # it from SQLite.
            payload = {"count": {}, "nl": {}}
        payload.setdefault("count", {})[str(sample_id)] = count
        temporary = path.with_suffix(path.suffix + ".partial")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    def pending_count(self) -> int:
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS pending FROM workflow_count_review"
                " WHERE job_id = ? AND status = 'pending'",
                (self.job_id,),
            ).fetchone()
        return int(row["pending"])

    def confirmed_counts(self) -> dict[int, str]:
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT sample_id, count_value FROM workflow_count_review"
                " WHERE job_id = ? AND status = 'confirmed'",
                (self.job_id,),
            ).fetchall()
        return {int(row["sample_id"]): str(row["count_value"]) for row in rows}

    def assert_ready_for_export(self) -> None:
        """Fail closed when any decision is still pending."""

        pending = self.pending_count()
        if pending:
            raise CountReviewError(
                f"count review is incomplete: {pending} sample(s) still pending"
            )


__all__ = [
    "COUNT_RANK",
    "COUNT_VALUES",
    "DECISION_SOURCES",
    "CountEvidence",
    "CountReviewConflictError",
    "CountReviewError",
    "CountReviewStore",
    "create_wiki_catalog",
    "derive_count_decisions",
    "normalize_original_count",
]
