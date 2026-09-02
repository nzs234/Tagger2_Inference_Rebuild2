"""Offline e621 vertical: import -> replace -> normalize -> export -> commit.

The stages implemented here are the deterministic, rule-only ones, so their
output is reproducible without any model or network access. Caption, OCR, NL,
count review, policy and token budget are separate stages that plug into the
same workspace and commit contract.
"""

from __future__ import annotations

import json
import hashlib
from random import Random
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stages.policy import PolicyConfig
from collections.abc import Mapping
from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .caption_format import (
    CaptionDisplayPolicy,
    FlatTextSerializationError,
    normalize_json_bytes,
    serialize_flat_txt,
)
from .commit import (
    CommitJournal,
    ExportStaging,
    StagedFile,
    commit_staged_files,
    sha256_file,
    verify_annotation_backup_baseline,
    write_annotation_backup,
)
from .contracts import (
    PartialNineFieldAnnotation,
    WorkflowJobConfigV1,
    canonical_json,
    utc_now,
)
from .dataset_import import ImportedSample, ImportResult, import_dataset
from .ocr import OCREngine, run_ocr_stage
from .projection_checkpoint import (
    ProjectionCheckpointError,
    load_projection_checkpoint,
    write_projection_checkpoint,
)
from .replacement_index import ReplacementRule, load_replacement_rules
from .stages.caption import (
    CaptionStageReport,
    TagPredictor,
    run_caption_stage,
    settings_from_config,
)
from .stages.classify import ClassificationRules, classify_tags
from .stages.nl import NlClient, run_nl_stage
from .stages.policy import PolicyError, apply_policy, merge_artists
from .stages.replacement import ReplacementError, ReplacementSummary, replace_projection
from .stages.token_budget import TokenBudgetError
from .stages.token_budget import fit as fit_token_budget

NINE_FIELDS = (
    "quality",
    "count",
    "character",
    "series",
    "artist",
    "appearance",
    "tags",
    "environment",
    "nl",
)


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot proceed."""


@dataclass
class StageIssue:
    sample_id: int | None
    relative_image_path: str | None
    module_id: str
    code: str
    message: str
    severity: str = "error"
    blocking: bool = True


@dataclass
class PipelineReport:
    """Outcome of one offline pipeline run."""

    total_samples: int = 0
    exported_samples: int = 0
    failed_samples: int = 0
    skipped_samples: int = 0
    committed_files: int = 0
    replacement: dict[str, int] = field(default_factory=dict)
    caption: dict[str, int] = field(default_factory=dict)
    ocr: dict[str, int] = field(default_factory=dict)
    nl: dict[str, int] = field(default_factory=dict)
    policy: dict[str, int] = field(default_factory=dict)
    token_budget: dict[str, int] = field(default_factory=dict)
    # Overflowing captions, for the caller to seed into token budget review.
    token_overflows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[StageIssue] = field(default_factory=list)
    backup_path: str | None = None
    resource_fingerprints: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_samples": self.total_samples,
            "exported_samples": self.exported_samples,
            "failed_samples": self.failed_samples,
            "skipped_samples": self.skipped_samples,
            "committed_files": self.committed_files,
            "replacement": dict(self.replacement),
            "caption": dict(self.caption),
            "ocr": dict(self.ocr),
            "nl": dict(self.nl),
            "policy": dict(self.policy),
            "token_budget": dict(self.token_budget),
            "token_overflows": [dict(item) for item in self.token_overflows],
            "issues": [
                {
                    "sample_id": issue.sample_id,
                    "relative_image_path": issue.relative_image_path,
                    "module_id": issue.module_id,
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                    "blocking": issue.blocking,
                }
                for issue in self.issues
            ],
            "backup_path": self.backup_path,
            "resource_fingerprints": dict(self.resource_fingerprints),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PipelineReport:
        """Rehydrate the private report stored beside a projection checkpoint."""

        report = cls(
            total_samples=int(value.get("total_samples", 0)),
            exported_samples=int(value.get("exported_samples", 0)),
            failed_samples=int(value.get("failed_samples", 0)),
            skipped_samples=int(value.get("skipped_samples", 0)),
            committed_files=int(value.get("committed_files", 0)),
            replacement=dict(value.get("replacement") or {}),
            caption=dict(value.get("caption") or {}),
            ocr=dict(value.get("ocr") or {}),
            nl=dict(value.get("nl") or {}),
            policy=dict(value.get("policy") or {}),
            token_budget=dict(value.get("token_budget") or {}),
            token_overflows=[dict(item) for item in value.get("token_overflows") or []],
            backup_path=(str(value["backup_path"]) if value.get("backup_path") else None),
            resource_fingerprints=dict(value.get("resource_fingerprints") or {}),
        )
        for item in value.get("issues") or []:
            if not isinstance(item, Mapping):
                raise ProjectionCheckpointError("checkpoint report contains an invalid issue")
            report.issues.append(
                StageIssue(
                    sample_id=(None if item.get("sample_id") is None else int(item["sample_id"])),
                    relative_image_path=(
                        None
                        if item.get("relative_image_path") is None
                        else str(item["relative_image_path"])
                    ),
                    module_id=str(item.get("module_id", "pipeline")),
                    code=str(item.get("code", "checkpoint_issue")),
                    message=str(item.get("message", "")),
                    severity=str(item.get("severity", "error")),
                    blocking=bool(item.get("blocking", True)),
                )
            )
        return report


class _StageRunTracker:
    """Keep durable stage-run rows out of the ``running`` state."""

    _TERMINAL = {"completed", "failed", "skipped"}

    def __init__(self, database: Any | None, job_id: str | None) -> None:
        self.database = database
        self.job_id = job_id
        self._open: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return (
            self.database is not None
            and self.job_id is not None
            and hasattr(self.database, "record_stage_run")
        )

    def begin(
        self,
        stage_id: str,
        *,
        total: int = 0,
        checkpoint: dict[str, Any] | None = None,
    ) -> str | None:
        database = self.database
        job_id = self.job_id
        if database is None or job_id is None or not hasattr(database, "record_stage_run"):
            return None
        run_id = database.record_stage_run(
            job_id,
            stage_id,
            status="running",
            total=total,
            checkpoint=checkpoint,
        )
        self._open[stage_id] = run_id
        return run_id

    def update(
        self,
        stage_id: str,
        status: str,
        *,
        total: int = 0,
        processed: int = 0,
        issue_count: int = 0,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        database = self.database
        job_id = self.job_id
        if database is None or job_id is None or not hasattr(database, "record_stage_run"):
            return
        run_id = self._open.get(stage_id)
        if run_id is None:
            run_id = self.begin(stage_id, total=total, checkpoint=checkpoint)
        if run_id is None:
            return
        database.record_stage_run(
            job_id,
            stage_id,
            status=status,
            run_id=run_id,
            total=total,
            processed=processed,
            issue_count=issue_count,
            checkpoint=checkpoint,
        )
        if status in self._TERMINAL:
            self._open.pop(stage_id, None)

    def close_open(self, *, status: str = "failed") -> None:
        """Close rows left open by an exception or an early return."""

        for stage_id in tuple(self._open):
            try:
                self.update(
                    stage_id,
                    status,
                    checkpoint={"checkpoint": "aborted", "reason": "pipeline_exit"},
                )
            except Exception:  # noqa: BLE001
                # Cleanup must never replace the original pipeline exception.
                self._open.pop(stage_id, None)


def _display_policy(config: WorkflowJobConfigV1) -> CaptionDisplayPolicy:
    caption = config.caption
    return CaptionDisplayPolicy(
        replace_underscores_with_spaces=bool(caption.get("replace_underscores_with_spaces", True)),
        preserve_escapes=bool(caption.get("preserve_escapes", True)),
        triggers_enabled=bool(caption.get("triggers_enabled", False)),
        trigger_terms=tuple(caption.get("trigger_terms", ())),
    )


def build_projection(sample: ImportedSample) -> PartialNineFieldAnnotation:
    """Build the nine-field projection for one imported sample.

    A standard JSON annotation is reused as-is; a raw e621 document contributes
    its artist/character plus classify tags. ``series`` stays empty for raw e621
    input, matching the source project's behaviour.
    """

    if sample.annotation_kind == "standard_json":
        raise PipelineError("standard JSON is handled by the caller")

    projection: PartialNineFieldAnnotation = {
        "quality": [],
        "count": "",
        "character": sample.character,
        "series": "",
        "artist": sample.artist,
        "appearance": [],
        "tags": list(sample.tags),
        "environment": [],
        "nl": sample.nl,
    }
    return projection


def _replacement_random_value(
    job_id: str | None,
    sample: ImportedSample,
) -> Callable[[], float] | None:
    """Return a stable per-job/sample RNG without changing the upstream port."""

    if not job_id:
        return None
    seed_material = (
        f"tagger2-replacement-v1\0{job_id}\0{sample.sample_id}\0"
        f"{sample.relative_image_path}"
    ).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest(), "big")
    rng = Random(seed)
    return rng.random


def _parse_policy_config(config_arg: dict[str, Any] | PolicyConfig) -> PolicyConfig:
    """Convert a policy config dictionary to PolicyConfig dataclass, or pass through if already a dataclass."""
    from .stages.policy import CoupledProbabilities, PolicyConfig
    
    # If already a PolicyConfig, return it as-is
    if isinstance(config_arg, PolicyConfig):
        return config_arg
    
    # Otherwise, parse the dictionary
    config_dict = config_arg
    
    def _parse_coupled(data: dict[str, Any]) -> CoupledProbabilities:
        return CoupledProbabilities(
            dropNl=data.get("nlDropoutProbability", 0.0),
            dropAppearance=data.get("appearanceDropoutProbability", 0.0),
        )
    
    return PolicyConfig(
        seed=config_dict["seed"],
        artistEnabled=config_dict.get("artistEnabled", False),
        artistDropoutProbability=config_dict.get("artistDropoutProbability", 0.0),
        qualityEnabled=config_dict.get("qualityEnabled", False),
        qualityDropoutProbability=config_dict.get("qualityDropoutProbability", 0.0),
        appearanceNlEnabled=config_dict.get("appearanceNlEnabled", False),
        solo=_parse_coupled(config_dict.get("solo", {})),
        nonSolo=_parse_coupled(config_dict.get("nonSolo", {})),
        unknown=_parse_coupled(config_dict.get("unknown", {})),
    )


def _safe_flat_txt(annotation: Mapping[str, object], policy: CaptionDisplayPolicy) -> str:
    """Flatten a caption for review, tolerating a payload that cannot serialize.

    An overflow row only needs readable text for the reviewer, so a payload that
    trimming emptied returns "" instead of failing the whole run.
    """

    try:
        return serialize_flat_txt(annotation, policy).decode("utf-8", "replace")
    except (FlatTextSerializationError, TypeError, ValueError):
        return ""


@dataclass(frozen=True)
class PipelineContext:
    """Immutable inputs for one offline pipeline run.

    Built once by :func:`_run_offline_pipeline_impl` from its keyword
    arguments; the phase functions read from it and never mutate it.
    """

    config: WorkflowJobConfigV1
    source_root: Path
    output_root: Path
    workspace: Path
    replacement_index_path: Path | None
    resource_fingerprints: dict[str, str]
    resource_manifests: Mapping[str, Mapping[str, Any]]
    tag_predictor: TagPredictor | None
    policy_config: Any | None
    token_counter: Any | None
    classification_rules: ClassificationRules | None
    ocr_engine: OCREngine | None
    nl_client: NlClient | None
    database: Any | None
    job_id: str | None
    stage_tracker: _StageRunTracker
    resource_verifier: Callable[[], None] | None


@dataclass
class PipelineState:
    """Mutable artifacts handed from one offline pipeline phase to the next."""

    report: PipelineReport
    policy: CaptionDisplayPolicy

    # Import and checkpoint/resume dispatch.
    imported: ImportResult | None = None
    checkpoint: dict[str, Any] | None = None
    resume_cursor: str | None = None
    resume_projections: dict[str, dict[str, Any]] = field(default_factory=dict)
    resume_completed_ids: set[int] = field(default_factory=set)

    # Reviewer overlays loaded before any stage runs.
    confirmed_counts: dict[int, str] = field(default_factory=dict)
    applied_token_texts: dict[int, str] = field(default_factory=dict)

    # Per-stage artifacts.
    caption_tags: dict[str, tuple[str, ...]] = field(default_factory=dict)
    ocr_results: dict[str, Any] = field(default_factory=dict)
    classified_projections: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    rules: dict[str, ReplacementRule] = field(default_factory=dict)
    nl_projections: dict[str, Any] = field(default_factory=dict)
    upstream_projections: dict[str, dict[str, Any]] = field(default_factory=dict)
    totals: ReplacementSummary = field(default_factory=lambda: ReplacementSummary(0, 0, 0, 0))

    # Export, staging and review parking.
    dataset_root: Path | None = None
    staging: ExportStaging | None = None
    journal: CommitJournal | None = None
    staged: list[StagedFile] = field(default_factory=list)
    exported_sample_ids: set[int] = field(default_factory=set)
    review_projections: dict[str, dict[str, Any]] = field(default_factory=dict)
    building_count_checkpoint: bool = False
    policy_counts: dict[str, int] = field(default_factory=dict)
    budget_counts: dict[str, int] = field(default_factory=dict)
    export_format: str = "both"

    # Durable stage-run ids and the export lease bookkeeping.
    pipeline_stage_run: str | None = None
    replace_stage_run: str | None = None
    policy_stage_run: str | None = None
    token_stage_run: str | None = None
    lease_lifecycle: Any | None = None
    lease_owner: str = ""
    lease_outcomes: dict[int, str] = field(default_factory=dict)


def _control_state(ctx: PipelineContext) -> str | None:
    """Read the durable control state at safe batch boundaries."""

    if ctx.database is None or ctx.job_id is None:
        return None
    current = ctx.database.get_job(ctx.job_id)
    return None if current is None else str(current["status"])


def _finish_pipeline_stage(
    ctx: PipelineContext,
    state: PipelineState,
    status: str,
    *,
    processed: int = 0,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    if state.pipeline_stage_run is not None:
        ctx.stage_tracker.update(
            "pipeline",
            status,
            total=state.report.total_samples,
            processed=processed,
            issue_count=len(state.report.issues),
            checkpoint=checkpoint,
        )


def _append_staged(ctx: PipelineContext, state: PipelineState, item: StagedFile) -> None:
    state.staged.append(item)
    if ctx.database is not None and ctx.job_id is not None and hasattr(ctx.database, "record_artifact"):
        ctx.database.record_artifact(
            ctx.job_id,
            kind="staged_export",
            relative_path=item.relative_path,
            sha256=item.sha256,
            size_bytes=item.size,
        )


def _leased_samples(ctx: PipelineContext, state: PipelineState) -> Iterator[ImportedSample]:
    """Yield the samples this run may process, claiming durable leases if any."""
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    if state.lease_lifecycle is None:
        yield from imported.samples
        return
    for offset in range(0, len(imported.samples), 500):
        batch = imported.samples[offset : offset + 500]
        claimed = state.lease_lifecycle.claim_batch(
            [sample.sample_id for sample in batch],
            owner=state.lease_owner,
        )
        if not claimed:
            continue
        claimed_set = set(claimed)
        state.lease_lifecycle.heartbeat_samples(claimed, owner=state.lease_owner)
        try:
            for index, sample in enumerate(batch):
                if sample.sample_id not in claimed_set:
                    continue
                yield sample
                if (index + 1) % 50 == 0:
                    remaining = [
                        item.sample_id
                        for item in batch[index + 1 :]
                        if item.sample_id in claimed_set
                    ]
                    if remaining:
                        state.lease_lifecycle.heartbeat_samples(
                            remaining,
                            owner=state.lease_owner,
                        )
        finally:
            state.lease_lifecycle.release_batch(
                {
                    sample_id: state.lease_outcomes.pop(sample_id, "pending")
                    for sample_id in claimed
                },
                owner=state.lease_owner,
            )


def _projection_for_sample(
    sample: ImportedSample,
    *,
    source_root: Path,
    classified_projections: Mapping[str, Mapping[str, list[str]]],
    caption_tags: Mapping[str, tuple[str, ...]],
    caption_enabled: bool,
    normalize_missing: bool,
    skip_uncaptioned: bool,
) -> dict[str, Any] | None:
    """Build the nine-field projection for one imported sample.

    Shared by the NL stage's temporary projection map and the upstream
    projection build.  ``normalize_missing`` adds the ``None`` -> ``[]``/``""``
    coercion the upstream build applies to standard JSON documents; the NL
    variant keeps the raw document values.  ``skip_uncaptioned`` returns
    ``None`` for a sample that was expected to produce a caption but did not
    (the upstream build skips it entirely, the NL variant still needs a
    projection).
    """

    if sample.annotation_kind == "standard_json":
        document = json.loads(
            (source_root / Path(sample.annotation_key + ".json"))
            .read_bytes()
            .decode("utf-8-sig")
        )
        projection: dict[str, Any] = {
            field_name: document.get(field_name) for field_name in NINE_FIELDS
        }
        if normalize_missing:
            projection = {
                key: (
                    []
                    if value is None
                    and key in {"quality", "appearance", "tags", "environment"}
                    else value
                )
                for key, value in projection.items()
            }
        if not projection["tags"]:
            projection["tags"] = list(sample.tags)
        if normalize_missing:
            projection = {
                key: ("" if value is None else value)
                for key, value in projection.items()
            }
        return projection

    projection = dict(build_projection(sample))
    classified = classified_projections.get(sample.relative_image_path)
    if classified:
        projection["quality"] = classified.get("quality", [])
        if sample.annotation_kind == "raw_e621_json":
            projection["tags"] = [
                tag
                for tag in classified.get("tags", [])
                if tag != projection["character"]
            ]
        else:
            projection["character"] = ", ".join(classified.get("character", []))
            projection["tags"] = classified.get("tags", [])
            projection["artist"] = merge_artists(
                str(projection["artist"]),
                ", ".join(classified.get("artist", [])),
            )
        projection["appearance"] = classified.get("appearance", [])
        projection["environment"] = classified.get("environment", [])
    elif caption_tags.get(sample.relative_image_path):
        projection["tags"] = list(caption_tags[sample.relative_image_path])
    elif skip_uncaptioned and caption_enabled and not sample.skip_caption:
        return None
    return projection


def _load_review_overlays(ctx: PipelineContext, state: PipelineState) -> None:
    """Load persisted reviewer decisions as overlays on the stage results."""
    if ctx.database is None or ctx.job_id is None:
        return
    from .count_review import CountReviewStore
    from .token_budget_review import TokenBudgetReviewStore

    state.confirmed_counts = CountReviewStore(ctx.database, ctx.job_id).confirmed_counts()
    state.applied_token_texts = TokenBudgetReviewStore(ctx.database, ctx.job_id).applied_texts()


def _prepare_imports(ctx: PipelineContext, state: PipelineState) -> None:
    """Discover the dataset, seed the control plane and record import issues."""
    stage_tracker = ctx.stage_tracker
    import_stage_run = stage_tracker.begin(
        "import",
        checkpoint={"checkpoint": "discovering"},
    )
    imported: ImportResult = import_dataset(
        ctx.source_root,
        recursive=ctx.config.recursive,
        input_txt_mode=str(ctx.config.caption.get("input_txt_mode", "tag")),
    )
    state.imported = imported
    state.report.total_samples = len(imported.samples)
    if import_stage_run is not None:
        stage_tracker.update(
            "import",
            "completed",
            total=state.report.total_samples,
            processed=state.report.total_samples,
            issue_count=len(imported.issues),
            checkpoint={"checkpoint": "imported", "sample_count": state.report.total_samples},
        )
    if state.pipeline_stage_run is not None:
        stage_tracker.update(
            "pipeline",
            "running",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "imported"},
        )

    if ctx.database is not None and ctx.job_id is not None:
        # The manifest is the source of truth for the control plane.  ``INSERT
        # OR IGNORE`` keeps a retry/recovery run idempotent and, importantly,
        # makes samples visible before any expensive stage starts.
        with ctx.database.connection() as conn:
            now = utc_now()
            for sample in imported.samples:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_samples
                        (job_id, sample_id, relative_image_path, image_format,
                         status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        ctx.job_id,
                        sample.sample_id,
                        sample.relative_image_path,
                        sample.image_format,
                        now,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE workflow_jobs SET total_samples = ? WHERE job_id = ?",
                (len(imported.samples), ctx.job_id),
            )
    for issue in imported.issues:
        state.report.issues.append(
            StageIssue(
                sample_id=None,
                relative_image_path=issue.relative_image_path,
                module_id="import",
                code=issue.code,
                message=issue.message,
                severity=issue.severity,
                blocking=issue.blocking,
            )
        )
        state.report.failed_samples += 1


def _restore_checkpoint(ctx: PipelineContext, state: PipelineState) -> None:
    """Resume from a persisted projection checkpoint when one is valid.

    A restored checkpoint replaces the in-memory report wholesale and selects
    the resume cursor that the later phases branch on.
    """
    if ctx.database is None or ctx.job_id is None:
        return
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    checkpoint = load_projection_checkpoint(
        ctx.workspace,
        job_id=ctx.job_id,
        config_hash=ctx.config.config_hash(),
        resource_fingerprints=ctx.resource_fingerprints,
        samples=imported.samples,
    )
    state.checkpoint = checkpoint
    if checkpoint is None:
        return
    state.resume_cursor = str(checkpoint["stage_cursor"])
    state.resume_projections = {
        str(sample_id): dict(projection)
        for sample_id, projection in dict(checkpoint["projections"]).items()
    }
    state.report = PipelineReport.from_dict(dict(checkpoint["report"]))
    state.report.total_samples = len(imported.samples)
    state.report.exported_samples = 0
    state.report.committed_files = 0
    if state.resume_cursor == "count_review":
        state.report.policy = {}
        state.report.token_budget = {}
        state.report.token_overflows = []
    elif state.resume_cursor == "token_review":
        overflow_issues = [
            issue for issue in state.report.issues if issue.code == "token_budget_overflow"
        ]
        state.report.failed_samples = max(0, state.report.failed_samples - len(overflow_issues))
        state.report.issues = [
            issue for issue in state.report.issues if issue.code != "token_budget_overflow"
        ]
        state.report.token_budget = {}
        state.report.token_overflows = []
    elif state.resume_cursor == "projection":
        with ctx.database.connection() as conn:
            rows = conn.execute(
                "SELECT sample_id FROM workflow_samples"
                " WHERE job_id = ? AND status = 'completed'",
                (ctx.job_id,),
            ).fetchall()
        state.resume_completed_ids = {int(row["sample_id"]) for row in rows}
        state.report.exported_samples = len(state.resume_completed_ids)


def _write_workspace_snapshots(ctx: PipelineContext, state: PipelineState) -> None:
    """Freeze the input manifest, config and resource snapshots."""
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    # Freeze the input manifest before anything is written.
    manifest_path = ctx.workspace / "input_manifest.jsonl"
    if state.checkpoint is None:
        with manifest_path.open("w", encoding="utf-8") as stream:
            for sample in imported.samples:
                stream.write(
                    canonical_json(
                        {
                            "sample_id": sample.sample_id,
                            "relative_image_path": sample.relative_image_path,
                            "annotation_key": sample.annotation_key,
                            "image_format": sample.image_format,
                            "annotation_kind": sample.annotation_kind,
                            "skip_caption": sample.skip_caption,
                        }
                    )
                    + "\n"
                )
        (ctx.workspace / "config_snapshot.json").write_text(
            json.dumps(ctx.config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
    resource_snapshot = {
        resource_id: {
            "fingerprint": fingerprint,
            "manifest": dict(ctx.resource_manifests.get(resource_id, {})),
        }
        for resource_id, fingerprint in ctx.resource_fingerprints.items()
    }
    if state.checkpoint is None:
        (ctx.workspace / "resource_snapshot.json").write_text(
            json.dumps(resource_snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if ctx.database is not None and ctx.job_id is not None:
        with ctx.database.connection() as conn:
            for resource_id, fingerprint in ctx.resource_fingerprints.items():
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_resource_snapshots "
                    "(job_id, resource_id, resource_fingerprint, manifest_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        ctx.job_id,
                        resource_id,
                        fingerprint,
                        canonical_json(ctx.resource_manifests.get(resource_id, {})),
                        utc_now(),
                    ),
                )


def _run_caption_phase(ctx: PipelineContext, state: PipelineState) -> None:
    """Run the caption stage and collect the raw tags per sample."""
    if not (ctx.config.caption.get("enabled") and state.checkpoint is None):
        return
    stage_tracker = ctx.stage_tracker
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    caption_stage_run = stage_tracker.begin(
        "caption",
        total=state.report.total_samples,
        checkpoint={"checkpoint": "starting"},
    )
    if ctx.tag_predictor is None:
        raise PipelineError("caption stage is enabled but no tag predictor was provided")
    caption_report: CaptionStageReport = run_caption_stage(
        imported.samples,
        source_root=ctx.source_root,
        predictor=ctx.tag_predictor,
        settings=settings_from_config(ctx.config.caption),
        model_id=str(ctx.config.caption.get("model_id", "")),
    )
    state.report.caption = {
        "captioned": caption_report.captioned,
        "skipped": caption_report.skipped,
        "failed": caption_report.failed,
    }
    for result in caption_report.results:
        if result.error:
            state.report.issues.append(
                StageIssue(
                    sample_id=None,
                    relative_image_path=result.relative_image_path,
                    module_id="caption",
                    code="caption_failed",
                    message=result.error,
                )
            )
            state.report.failed_samples += 1
        elif not result.skipped:
            state.caption_tags[result.relative_image_path] = tuple(
                tag.raw_tag for tag in result.tags
            )
    if caption_stage_run is not None:
        stage_tracker.update(
            "caption",
            "completed",
            total=state.report.total_samples,
            processed=caption_report.captioned + caption_report.skipped,
            issue_count=caption_report.failed,
            checkpoint={
                "checkpoint": "captioned",
                "captioned": caption_report.captioned,
                "skipped": caption_report.skipped,
                "failed": caption_report.failed,
            },
        )


def _run_ocr_phase(ctx: PipelineContext, state: PipelineState) -> None:
    """Run the OCR sidecar stage.

    OCR never changes the nine-field payload and never blocks the run, so a
    missing runtime or a failed image is a warning and the pipeline continues.
    """
    if not (ctx.config.ocr.get("enabled") and state.checkpoint is None):
        return
    stage_tracker = ctx.stage_tracker
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    ocr_stage_run = stage_tracker.begin(
        "ocr",
        total=state.report.total_samples,
        checkpoint={"checkpoint": "starting"},
    )
    ocr_results, ocr_issues = run_ocr_stage(
        ctx.workspace,
        {
            sample.relative_image_path: ctx.source_root / sample.relative_image_path
            for sample in imported.samples
        },
        ctx.config.ocr,
        ctx.ocr_engine,
    )
    state.ocr_results = ocr_results
    state.report.ocr = {
        "processed": sum(1 for result in ocr_results.values() if result.success),
        "failed": sum(1 for result in ocr_results.values() if not result.success),
        "regions": sum(
            len(result.detected_regions) for result in ocr_results.values() if result.success
        ),
    }
    for ocr_issue in ocr_issues:
        state.report.issues.append(
            StageIssue(
                sample_id=None,
                relative_image_path=ocr_issue.relative_image_path,
                module_id="ocr",
                code=ocr_issue.code,
                message=ocr_issue.message,
                severity="warning",
                blocking=False,
            )
        )
    if ocr_stage_run is not None:
        stage_tracker.update(
            "ocr",
            "completed",
            total=state.report.total_samples,
            processed=len(ocr_results),
            issue_count=len(ocr_issues),
            checkpoint={
                "checkpoint": "sidecars",
                "processed": state.report.ocr.get("processed", 0),
                "failed": state.report.ocr.get("failed", 0),
                "regions": state.report.ocr.get("regions", 0),
            },
        )


def _run_classify_phase(ctx: PipelineContext, state: PipelineState) -> None:
    """Map caption/imported tags into the nine-field structure."""
    stage_tracker = ctx.stage_tracker
    classify_stage_run = (
        stage_tracker.begin(
            "classify",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "starting"},
        )
        if ctx.config.classify.get("enabled") and state.checkpoint is None
        else None
    )
    if ctx.classification_rules is not None and state.checkpoint is None:
        imported = state.imported
        assert imported is not None  # set by _prepare_imports
        # Build a combined tags source: prefer caption_tags, fallback to imported tags
        tags_to_classify: dict[str, tuple[str, ...]] = {}

        # First, add imported tags for samples with tag_txt or raw_e621_json
        for sample in imported.samples:
            if sample.annotation_kind in ("tag_txt", "raw_e621_json") and sample.tags:
                tags_to_classify[sample.relative_image_path] = sample.tags

        # Caption tags override imported tags
        tags_to_classify.update(state.caption_tags)

        classified_projections: dict[str, dict[str, list[str]]] = {}
        for relative_path, tags in tags_to_classify.items():
            try:
                classified_projections[relative_path] = classify_tags(
                    list(tags), ctx.classification_rules
                )
            except Exception as exc:  # noqa: BLE001
                state.report.issues.append(
                    StageIssue(
                        sample_id=None,
                        relative_image_path=relative_path,
                        module_id="classify",
                        code="classify_failed",
                        message=str(exc),
                    )
                )
                state.report.failed_samples += 1
        state.classified_projections = classified_projections
        if classify_stage_run is not None:
            stage_tracker.update(
                "classify",
                "completed",
                total=state.report.total_samples,
                processed=len(classified_projections),
                issue_count=sum(
                    1 for issue in state.report.issues if issue.module_id == "classify"
                ),
                checkpoint={
                    "checkpoint": "classified",
                    "sample_count": len(classified_projections),
                },
            )


def _prepare_replacement_rules(ctx: PipelineContext, state: PipelineState) -> None:
    """Load the replacement rule table when the replace stage is enabled."""
    if ctx.config.replace.get("enabled") and state.checkpoint is None:
        if ctx.replacement_index_path is None:
            raise PipelineError("replace stage is enabled but no replacement index was provided")
        state.rules = load_replacement_rules(Path(ctx.replacement_index_path))
        state.replace_stage_run = ctx.stage_tracker.begin(
            "replace",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "rules_loaded", "rule_count": len(state.rules)},
        )


def _prepare_staging(ctx: PipelineContext, state: PipelineState) -> None:
    """Take the in-place backup, then prepare staging, journal and staged files."""
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    state.dataset_root = ctx.source_root if ctx.config.work_mode == "in_place" else ctx.output_root

    if ctx.config.work_mode == "in_place" and imported.samples:
        # Never modify a dataset in place without a verified backup first.
        backup_target = ctx.workspace / "backup" / "annotations.zip"
        if backup_target.is_file():
            # Review-gated retries must reuse the original pre-workflow
            # baseline; taking a second backup would capture an intermediate
            # overlay and make Restore semantically incorrect.
            state.report.backup_path = str(backup_target)
        else:
            state.report.backup_path = str(
                write_annotation_backup(
                    ctx.source_root,
                    backup_target,
                    (sample.annotation_key for sample in imported.samples),
                )
            )
    staging = ExportStaging(ctx.workspace / "staging")
    state.staging = staging
    state.journal = CommitJournal(
        ctx.workspace / "commit_journal.jsonl",
        database=ctx.database,
        job_id=ctx.job_id,
    )
    staged: list[StagedFile] = []
    if ctx.database is not None and ctx.job_id is not None and hasattr(ctx.database, "list_artifacts"):
        for artifact in ctx.database.list_artifacts(ctx.job_id, kind="staged_export"):
            relative_path = str(artifact["relative_path"])
            staged_path = staging.staged_path(relative_path)
            expected_size = int(artifact["size_bytes"])
            expected_digest = str(artifact["sha256"])
            if (
                not staged_path.is_file()
                or staged_path.stat().st_size != expected_size
                or sha256_file(staged_path) != expected_digest
            ):
                raise PipelineError("persisted staged artifact failed integrity verification")
            staged.append(
                StagedFile(
                    relative_path=relative_path,
                    sha256=expected_digest,
                    size=expected_size,
                )
            )
    state.staged = staged


def _prepare_export_state(ctx: PipelineContext, state: PipelineState) -> None:
    """Initialize export bookkeeping, review gates and durable stage runs."""
    state.exported_sample_ids = set(state.resume_completed_ids)
    # Persist complete projections independently of staging.  Review actions
    # use this immutable workspace overlay to rebuild JSON/TXT bytes without
    # re-running model stages or touching the target dataset.
    state.review_projections = {}
    state.totals = ReplacementSummary(
        int(state.report.replacement.get("replaced", 0)),
        int(state.report.replacement.get("dropped", 0)),
        int(state.report.replacement.get("passthrough", 0)),
        int(state.report.replacement.get("keep_rewritten", 0)),
    )
    state.building_count_checkpoint = bool(
        state.resume_cursor in {None, "projection"}
        and ctx.database is not None
        and ctx.job_id is not None
        and ctx.config.count_review.get("enabled")
    )
    state.policy_counts = (
        dict(state.report.policy)
        if state.resume_cursor == "token_review"
        else {"artist_dropped": 0, "quality_dropped": 0}
    )
    state.budget_counts = {}
    state.policy_stage_run = (
        ctx.stage_tracker.begin(
            "policy",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "starting"},
        )
        if ctx.policy_config is not None
        and not state.building_count_checkpoint
        and state.resume_cursor != "token_review"
        else None
    )
    state.token_stage_run = (
        ctx.stage_tracker.begin(
            "token_budget",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "starting"},
        )
        if ctx.config.token_budget.get("enabled")
        and ctx.token_counter is not None
        and not state.building_count_checkpoint
        else None
    )
    # Public API keeps the stable ``txt`` spelling; the normalizer uses the
    # internal ``flat_txt`` representation only at the serialization boundary.
    export_format = str(ctx.config.export.get("format", "both"))
    if export_format == "txt":
        export_format = "flat_txt"
    if export_format not in {"json", "flat_txt", "both"}:
        raise PipelineError(f"unsupported export format: {export_format!r}")
    state.export_format = export_format


def _run_nl_phase(ctx: PipelineContext, state: PipelineState) -> None:
    """Generate natural language captions for the assembled projections."""
    nl_active = bool(
        state.checkpoint is None
        and ctx.config.nl.get("enabled")
        and ctx.config.nl.get("api_enabled")
        and ctx.nl_client is not None
    )
    nl_stage_run = (
        ctx.stage_tracker.begin(
            "nl",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "starting"},
        )
        if nl_active
        else None
    )
    if not nl_active:
        return
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    assert ctx.nl_client is not None
    # Build projections dict for NL stage (needs full nine-field structure)
    temp_projections: dict[str, dict[str, Any]] = {}
    for sample in imported.samples:
        projection = _projection_for_sample(
            sample,
            source_root=ctx.source_root,
            classified_projections=state.classified_projections,
            caption_tags=state.caption_tags,
            caption_enabled=bool(ctx.config.caption.get("enabled")),
            normalize_missing=False,
            skip_uncaptioned=False,
        )
        if projection is not None:
            temp_projections[sample.relative_image_path] = dict(projection)

    nl_report = run_nl_stage(
        imported.samples,
        temp_projections,
        source_root=ctx.source_root,
        client=ctx.nl_client,
        preset=str(ctx.config.nl.get("prompt_preset", "general")),
        length=str(ctx.config.nl.get("length", "medium")),
        reuse_original_nl=bool(ctx.config.nl.get("reuse_original_nl", True)),
        use_image=bool(ctx.config.nl.get("use_image", True)),
        use_full_json=bool(ctx.config.nl.get("use_full_json", False)),
        concurrency=int(ctx.config.nl.get("concurrency", 4)),
        ocr_by_path={
            relative_path: {
                "regions": result.detected_regions,
                "success": result.success,
            }
            for relative_path, result in state.ocr_results.items()
        },
    )
    state.nl_projections = nl_report.by_path()
    state.report.nl = {
        "generated": nl_report.generated,
        "reused": nl_report.reused,
        "failed": nl_report.failed,
    }
    if nl_stage_run is not None:
        ctx.stage_tracker.update(
            "nl",
            "completed",
            total=state.report.total_samples,
            processed=nl_report.generated + nl_report.reused,
            issue_count=nl_report.failed,
            checkpoint={
                "checkpoint": "generated",
                "generated": nl_report.generated,
                "reused": nl_report.reused,
                "failed": nl_report.failed,
            },
        )


def _build_upstream_projections(ctx: PipelineContext, state: PipelineState) -> None:
    """Freeze the post-stage projection for every sample, or resume it.

    The complete post-Caption/Classify/Replace/OCR/NL projection is frozen even
    when no human review is configured.  A process restart can then resume
    deterministic Policy/Token/Export work without invoking a model or remote
    provider again.
    """
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    upstream_projections: dict[str, dict[str, Any]] = {}
    if state.checkpoint is not None:
        upstream_projections = {
            str(sample_id): dict(projection)
            for sample_id, projection in state.resume_projections.items()
        }
    else:
        totals = state.totals
        for sample in imported.samples:
            try:
                projection = _projection_for_sample(
                    sample,
                    source_root=ctx.source_root,
                    classified_projections=state.classified_projections,
                    caption_tags=state.caption_tags,
                    caption_enabled=bool(ctx.config.caption.get("enabled")),
                    normalize_missing=True,
                    skip_uncaptioned=True,
                )
                if projection is None:
                    continue

                if state.rules:
                    random_value = _replacement_random_value(ctx.job_id, sample)
                    if random_value is None:
                        projection, summary = replace_projection(projection, state.rules)
                    else:
                        projection, summary = replace_projection(
                            projection,
                            state.rules,
                            random_value=random_value,
                        )
                    totals = totals.merge(summary)

                if state.nl_projections and sample.relative_image_path in state.nl_projections:
                    projection["nl"] = state.nl_projections[sample.relative_image_path].nl
                upstream_projections[str(sample.sample_id)] = dict(projection)
            except (ReplacementError, ValueError, OSError) as exc:
                state.report.issues.append(
                    StageIssue(
                        sample_id=sample.sample_id,
                        relative_image_path=sample.relative_image_path,
                        module_id="export",
                        code="sample_failed",
                        message=str(exc),
                    )
                )
                state.report.failed_samples += 1

        state.totals = totals
        state.report.replacement = {
            "replaced": totals.replaced,
            "dropped": totals.dropped,
            "passthrough": totals.passthrough,
            "keep_rewritten": totals.keep_rewritten,
        }
        if ctx.database is not None and ctx.job_id is not None and upstream_projections:
            checkpoint_path, checkpoint_digest, checkpoint_size = (
                write_projection_checkpoint(
                    ctx.workspace,
                    stage_cursor="projection",
                    job_id=ctx.job_id,
                    config_hash=ctx.config.config_hash(),
                    resource_fingerprints=ctx.resource_fingerprints,
                    samples=imported.samples,
                    projections=upstream_projections,
                    report=state.report.as_dict(),
                )
            )
            if hasattr(ctx.database, "record_artifact"):
                ctx.database.record_artifact(
                    ctx.job_id,
                    kind="projection_checkpoint",
                    relative_path=checkpoint_path.relative_to(ctx.workspace).as_posix(),
                    sha256=checkpoint_digest,
                    size_bytes=checkpoint_size,
                )
    state.upstream_projections = upstream_projections


def _export_one_sample(
    ctx: PipelineContext,
    state: PipelineState,
    staging: ExportStaging,
    sample: ImportedSample,
) -> None:
    """Apply policy/budget overlays, normalize and stage one sample.

    Per-sample failures are recorded as issues on the report instead of
    aborting the run; unexpected errors propagate to the caller.
    """
    stored_projection = state.upstream_projections.get(str(sample.sample_id))
    if stored_projection is None:
        return
    projection = dict(stored_projection)

    if state.building_count_checkpoint:
        state.review_projections[str(sample.sample_id)] = dict(projection)
        state.report.exported_samples += 1
        state.exported_sample_ids.add(sample.sample_id)
        return

    # Count review is upstream of Policy.  Refuse a partial overlay:
    # every checkpointed sample must carry an explicit reviewed value.
    if state.resume_cursor == "count_review" and sample.sample_id not in state.confirmed_counts:
        raise ProjectionCheckpointError(
            f"count review overlay is missing sample {sample.sample_id}"
        )
    if sample.sample_id in state.confirmed_counts:
        projection["count"] = state.confirmed_counts[sample.sample_id]

    parsed_policy_config = None
    if ctx.policy_config is not None and state.resume_cursor != "token_review":
        parsed_policy_config = _parse_policy_config(ctx.policy_config)

    if parsed_policy_config is not None:
        try:
            projection, decision = apply_policy(
                projection,
                annotation_key=sample.annotation_key,
                relative_image_path=sample.relative_image_path,
                config=parsed_policy_config,
                aesthetic_score=None,
            )
        except PolicyError as exc:
            state.report.issues.append(
                StageIssue(
                    sample_id=sample.sample_id,
                    relative_image_path=sample.relative_image_path,
                    module_id="policy",
                    code="policy_failed",
                    message=str(exc),
                )
            )
            state.report.failed_samples += 1
            return
        state.policy_counts["artist_dropped"] += int(decision.artistDropped)
        state.policy_counts["quality_dropped"] += int(decision.qualityDropped)
        state.policy_counts[decision.appearanceNlAction] = (
            state.policy_counts.get(decision.appearanceNlAction, 0) + 1
        )

    # Token review is downstream of Policy.  The token checkpoint
    # therefore stores post-policy/pre-budget data and applies only the
    # reviewed NL text when it resumes.
    if sample.sample_id in state.applied_token_texts:
        projection["nl"] = state.applied_token_texts[sample.sample_id]

    # Preserve the pre-budget projection even when the budget stage
    # later parks this sample for human review.
    state.review_projections[str(sample.sample_id)] = dict(projection)

    # ``token_counter`` is an injected capability, but the stage is
    # still controlled by the immutable job contract.  A caller may
    # provide a tokenizer for another reason (or reuse a pipeline
    # helper) without accidentally enabling token review on a job that
    # explicitly disabled it.
    if ctx.config.token_budget.get("enabled") and ctx.token_counter is not None:
        caption_format = {
            "replaceUnderscoresWithSpaces": state.policy.replace_underscores_with_spaces,
            "preserveEscapes": state.policy.preserve_escapes,
            "triggersEnabled": state.policy.triggers_enabled,
            "triggerTerms": list(state.policy.trigger_terms),
        }
        try:
            budget = fit_token_budget(
                projection,
                caption_format,
                int(ctx.config.token_budget.get("max_tokens", 225)),
                ctx.token_counter,
            )
        except FlatTextSerializationError as exc:
            # Trimming reached an empty payload, which cannot be
            # serialized. That is an overflow needing a human decision.
            state.report.issues.append(
                StageIssue(
                    sample_id=sample.sample_id,
                    relative_image_path=sample.relative_image_path,
                    module_id="token_budget",
                    code="token_budget_overflow",
                    message=(
                        "caption cannot fit the token budget without"
                        f" emptying the payload: {exc}"
                    ),
                    blocking=False,
                )
            )
            state.report.token_overflows.append(
                {
                    "sample_id": sample.sample_id,
                    "relative_image_path": sample.relative_image_path,
                    "nl_text": _safe_flat_txt(projection, state.policy),
                    "token_count": int(ctx.config.token_budget.get("max_tokens", 225)) + 1,
                    "token_limit": int(ctx.config.token_budget.get("max_tokens", 225)),
                }
            )
            state.budget_counts["overflow"] = state.budget_counts.get("overflow", 0) + 1
            state.report.failed_samples += 1
            return
        except TokenBudgetError as exc:
            state.report.issues.append(
                StageIssue(
                    sample_id=sample.sample_id,
                    relative_image_path=sample.relative_image_path,
                    module_id="token_budget",
                    code="token_budget_failed",
                    message=str(exc),
                )
            )
            state.report.failed_samples += 1
            return
        state.budget_counts[budget.status] = state.budget_counts.get(budget.status, 0) + 1
        if budget.status == "overflow" or budget.annotation is None:
            # Overflow needs a human decision; never silently truncate.
            state.report.issues.append(
                StageIssue(
                    sample_id=sample.sample_id,
                    relative_image_path=sample.relative_image_path,
                    module_id="token_budget",
                    code="token_budget_overflow",
                    message=(
                        f"caption needs {budget.original_tokens} tokens and cannot fit"
                        f" the budget even after trimming"
                    ),
                    blocking=False,
                )
            )
            state.report.token_overflows.append(
                {
                    "sample_id": sample.sample_id,
                    "relative_image_path": sample.relative_image_path,
                    "nl_text": _safe_flat_txt(
                        budget.annotation or projection, state.policy
                    ),
                    "token_count": budget.original_tokens,
                    "token_limit": int(ctx.config.token_budget.get("max_tokens", 225)),
                }
            )
            state.report.failed_samples += 1
            return
        projection = dict(budget.annotation)

    normalization_format = "both" if state.export_format == "both" else (
        "json" if state.export_format == "json" else "flat_txt"
    )
    normalized = normalize_json_bytes(
        json.dumps(projection, ensure_ascii=False).encode("utf-8"),
        state.policy,
        export_format=normalization_format,
    )
    if (
        not normalized.valid
        or normalized.payload is None
        or normalized.json_bytes is None
    ):
        codes = ", ".join(
            f"{error.code}" + (f"[{error.field}]" if error.field else "")
            for error in normalized.field_errors
        )
        state.report.issues.append(
            StageIssue(
                sample_id=sample.sample_id,
                relative_image_path=sample.relative_image_path,
                module_id="export",
                code="normalization_failed",
                message=f"payload rejected: {codes}",
            )
        )
        state.report.failed_samples += 1
        return

    if state.export_format in {"json", "both"}:
        _append_staged(
            ctx, state, staging.stage(sample.annotation_key + ".json", normalized.json_bytes)
        )
    if state.export_format in {"flat_txt", "both"}:
        _append_staged(
            ctx,
            state,
            staging.stage(
                sample.annotation_key + ".txt",
                serialize_flat_txt(normalized.payload, state.policy),
            ),
        )
    # `full_copy` writes into a separate output root, so the image has
    # to travel with its annotation or the output is not a usable
    # dataset. `in_place` already has the image where it belongs.
    # Staged before the counter so a read failure below is counted once,
    # as a failure, rather than as both exported and failed.
    if ctx.config.work_mode == "full_copy":
        source_image = ctx.source_root / sample.relative_image_path
        if source_image.is_file():
            _append_staged(
                ctx, state, staging.stage_file(sample.relative_image_path, source_image)
            )

    state.report.exported_samples += 1
    state.exported_sample_ids.add(sample.sample_id)


def _run_export_phase(ctx: PipelineContext, state: PipelineState) -> None:
    """Lease, finalize and stage every sample, then close the stage runs."""
    staging = state.staging
    assert staging is not None  # set by _prepare_staging
    lease_lifecycle = None
    state.lease_owner = ""
    state.lease_outcomes = {}
    previously_failed_sample_ids = {
        int(issue.sample_id)
        for issue in state.report.issues
        if issue.sample_id is not None and issue.severity == "error"
    }
    if ctx.database is not None and ctx.job_id is not None and state.resume_cursor in {None, "projection"}:
        from .lifecycle import JobLifecycle

        lease_lifecycle = JobLifecycle(ctx.database, ctx.job_id)
        state.lease_lifecycle = lease_lifecycle
        state.lease_owner = f"pipeline-{ctx.job_id}-{uuid.uuid4().hex}"

    export_stage_run = (
        ctx.stage_tracker.begin(
            "export",
            total=state.report.total_samples,
            checkpoint={"checkpoint": "staging"},
        )
        if not state.building_count_checkpoint
        else None
    )
    for sample in _leased_samples(ctx, state):
        if _control_state(ctx) in {"pausing", "paused", "cancelling", "cancelled", "interrupted"}:
            break
        failures_before = state.report.failed_samples
        try:
            _export_one_sample(ctx, state, staging, sample)
        except (ReplacementError, ValueError, OSError) as exc:
            state.report.issues.append(
                StageIssue(
                    sample_id=sample.sample_id,
                    relative_image_path=sample.relative_image_path,
                    module_id="export",
                    code="sample_failed",
                    message=str(exc),
                )
            )
            state.report.failed_samples += 1
        finally:
            if lease_lifecycle is not None:
                if sample.sample_id in state.exported_sample_ids:
                    state.lease_outcomes[sample.sample_id] = "completed"
                elif (
                    sample.sample_id in previously_failed_sample_ids
                    or state.report.failed_samples > failures_before
                ):
                    state.lease_outcomes[sample.sample_id] = "failed"
                else:
                    state.lease_outcomes[sample.sample_id] = "skipped"

    if export_stage_run is not None:
        export_control_state = _control_state(ctx)
        export_status = (
            "skipped"
            if export_control_state
            in {"pausing", "paused", "cancelling", "cancelled", "interrupted"}
            else "completed"
        )
        ctx.stage_tracker.update(
            "export",
            export_status,
            total=state.report.total_samples,
            processed=state.report.exported_samples,
            issue_count=len(state.report.issues),
            checkpoint={
                "checkpoint": "staged",
                "exported_samples": state.report.exported_samples,
                "failed_samples": state.report.failed_samples,
            },
        )

    if state.replace_stage_run is not None:
        ctx.stage_tracker.update(
            "replace",
            "completed",
            total=state.report.total_samples,
            processed=state.report.exported_samples,
            issue_count=sum(1 for issue in state.report.issues if issue.module_id == "replace"),
            checkpoint={
                "checkpoint": "applied",
                "replaced": state.totals.replaced,
                "dropped": state.totals.dropped,
                "passthrough": state.totals.passthrough,
            },
        )
    if state.policy_stage_run is not None:
        ctx.stage_tracker.update(
            "policy",
            "completed",
            total=state.report.total_samples,
            processed=state.report.exported_samples,
            issue_count=sum(1 for issue in state.report.issues if issue.module_id == "policy"),
            checkpoint={"checkpoint": "applied", **state.policy_counts},
        )
    if state.token_stage_run is not None:
        ctx.stage_tracker.update(
            "token_budget",
            "completed",
            total=state.report.total_samples,
            processed=state.report.exported_samples,
            issue_count=sum(
                1 for issue in state.report.issues if issue.module_id == "token_budget"
            ),
            checkpoint={"checkpoint": "applied", **state.budget_counts},
        )

    state.report.policy = dict(state.policy_counts)
    state.report.token_budget = dict(state.budget_counts)
    state.report.replacement = {
        "replaced": state.totals.replaced,
        "dropped": state.totals.dropped,
        "passthrough": state.totals.passthrough,
        "keep_rewritten": state.totals.keep_rewritten,
    }


def _sync_control_plane(ctx: PipelineContext, state: PipelineState) -> None:
    """Mirror sample statuses, review parking and issues into the database."""
    imported = state.imported
    assert imported is not None  # set by _prepare_imports
    report = state.report
    review_stage_run = ctx.stage_tracker.begin(
        "review",
        total=report.total_samples,
        checkpoint={"checkpoint": "overlay"},
    )
    if ctx.database is not None and ctx.job_id is not None:
        # The control plane must reflect stage completion even when the final
        # dataset commit is deferred for review.  A sample marked completed
        # here means its private overlay/staging bytes are valid, not that the
        # target dataset has already changed.
        issue_sample_ids = {
            issue.sample_id
            for issue in report.issues
            if issue.sample_id is not None and issue.severity == "error"
        }
        with ctx.database.connection() as conn:
            now = utc_now()
            if state.lease_lifecycle is None:
                for sample in imported.samples:
                    if sample.sample_id in state.exported_sample_ids:
                        sample_status = "completed"
                    elif sample.sample_id in issue_sample_ids:
                        sample_status = "failed"
                    else:
                        sample_status = "skipped"
                    conn.execute(
                        "UPDATE workflow_samples SET status = ?, updated_at = ? WHERE job_id = ? AND sample_id = ?",
                        (sample_status, now, ctx.job_id, sample.sample_id),
                    )
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS total FROM workflow_samples"
                " WHERE job_id = ? GROUP BY status",
                (ctx.job_id,),
            ).fetchall()
            status_totals = {
                str(row["status"]): int(row["total"]) for row in status_rows
            }
            completed_total = status_totals.get("completed", 0)
            failed_total = status_totals.get("failed", 0)
            skipped_total = status_totals.get("skipped", 0)
            conn.execute(
                """
                UPDATE workflow_jobs
                   SET processed_samples = ?, succeeded_samples = ?,
                       failed_samples = ?, skipped_samples = ?
                 WHERE job_id = ?
                """,
                (
                    completed_total + failed_total + skipped_total,
                    completed_total,
                    failed_total,
                    skipped_total,
                    ctx.job_id,
                ),
            )

    if ctx.database is not None and ctx.job_id is not None:
        # Keep a durable projection snapshot for count/token review.  It is
        # deliberately stored under the private workspace, never returned by
        # the API, and is replaced atomically on a recovery run.
        projection_path = ctx.workspace / "projections.json"
        temporary = projection_path.with_suffix(".json.partial")
        temporary.write_text(
            json.dumps(state.review_projections, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(projection_path)

        # Count review is a production gate, not a test-only helper.  The
        # bundled wiki snapshot is optional; an empty catalog makes the rules
        # preserve the original value and exposes the uncertainty to the
        # reviewer instead of fabricating a count.
        if state.building_count_checkpoint and imported.samples:
            from .count_review import (
                CountReviewStore,
                create_wiki_catalog,
                derive_count_decisions,
            )

            wiki_db = create_wiki_catalog(ctx.workspace / "wiki_catalog.sqlite3")

            class _ReviewSample:
                def __init__(self, sample_id: int, relative_image_path: str):
                    self.sample_id = sample_id
                    self.relative_image_path = relative_image_path

            evidence = derive_count_decisions(
                [
                    _ReviewSample(sample.sample_id, sample.relative_image_path)
                    for sample in imported.samples
                    if str(sample.sample_id) in state.review_projections
                ],
                {
                    next(
                        sample.relative_image_path
                        for sample in imported.samples
                        if sample.sample_id == int(sample_id)
                    ): projection
                    for sample_id, projection in state.review_projections.items()
                },
                wiki_db_path=wiki_db,
                observations={
                    relative_path: result.observation
                    for relative_path, result in state.nl_projections.items()
                },
                overwrite_count=bool(ctx.config.classify.get("overwrite_count", False)),
            )
            CountReviewStore(ctx.database, ctx.job_id).initialize(evidence)

            checkpoint_path, checkpoint_digest, checkpoint_size = (
                write_projection_checkpoint(
                    ctx.workspace,
                    stage_cursor="count_review",
                    job_id=ctx.job_id,
                    config_hash=ctx.config.config_hash(),
                    resource_fingerprints=ctx.resource_fingerprints,
                    samples=imported.samples,
                    projections=state.review_projections,
                    report=report.as_dict(),
                )
            )
            if hasattr(ctx.database, "record_artifact"):
                ctx.database.record_artifact(
                    ctx.job_id,
                    kind="projection_checkpoint_count",
                    relative_path=checkpoint_path.relative_to(ctx.workspace).as_posix(),
                    sha256=checkpoint_digest,
                    size_bytes=checkpoint_size,
                )

        # Token rows are initialized here as well as by the API for backward
        # compatibility.  ``INSERT OR IGNORE`` makes the two paths harmless.
        if report.token_overflows:
            from .token_budget_review import TokenBudgetReviewStore

            TokenBudgetReviewStore(ctx.database, ctx.job_id).initialize(report.token_overflows)
            checkpoint_path, checkpoint_digest, checkpoint_size = (
                write_projection_checkpoint(
                    ctx.workspace,
                    stage_cursor="token_review",
                    job_id=ctx.job_id,
                    config_hash=ctx.config.config_hash(),
                    resource_fingerprints=ctx.resource_fingerprints,
                    samples=imported.samples,
                    projections=state.review_projections,
                    report=report.as_dict(),
                )
            )
            if hasattr(ctx.database, "record_artifact"):
                ctx.database.record_artifact(
                    ctx.job_id,
                    kind="projection_checkpoint_token",
                    relative_path=checkpoint_path.relative_to(ctx.workspace).as_posix(),
                    sha256=checkpoint_digest,
                    size_bytes=checkpoint_size,
                )

        # Every issue is visible in the control plane before the job can enter
        # a waiting state. Keep prior rows for audit; recovery attempts get a
        # distinct issue id and the durable event cursor preserves history.
        for report_issue in report.issues:
            ctx.database.create_issue(
                ctx.job_id,
                module_id=report_issue.module_id,
                code=report_issue.code,
                severity=report_issue.severity,
                blocking=report_issue.blocking,
                message=report_issue.message,
                sample_id=report_issue.sample_id,
            )
    if review_stage_run is not None:
        ctx.stage_tracker.update(
            "review",
            "completed",
            total=report.total_samples,
            processed=report.exported_samples,
            issue_count=len(report.issues),
            checkpoint={"checkpoint": "overlay_ready"},
        )


def _finalize_report(ctx: PipelineContext, state: PipelineState) -> PipelineReport:
    """Run the commit gates, commit the staged files and return the report."""
    report = state.report
    journal = state.journal
    staging = state.staging
    dataset_root = state.dataset_root
    assert journal is not None and staging is not None and dataset_root is not None

    blocking = [issue for issue in report.issues if issue.blocking]
    if blocking:
        # Fail closed: a blocking issue must not produce a half-written dataset.
        journal.append({"event": "commit_skipped", "blocking_issues": len(blocking)})
        _finish_pipeline_stage(ctx, state, "failed", processed=report.exported_samples)
        _write_issue_log(ctx.workspace, report)
        return report

    # In production a review-gated run must leave all output in the private
    # staging tree.  The API resumes this checkpoint after Count/Token review
    # and performs the sole commit then.  Direct callers (the deterministic
    # offline tests and CLI) retain the historical immediate-commit behaviour.
    if ctx.database is not None and ctx.job_id is not None:
        from .count_review import CountReviewStore
        from .token_budget_review import TokenBudgetReviewStore

        if (
            (ctx.config.count_review.get("enabled") and CountReviewStore(ctx.database, ctx.job_id).pending_count())
            or (
                ctx.config.token_budget.get("enabled")
                and TokenBudgetReviewStore(ctx.database, ctx.job_id).unresolved_count()
            )
        ):
            journal.append({"event": "commit_deferred_for_review"})
            _finish_pipeline_stage(
                ctx, state, "skipped", processed=report.exported_samples, checkpoint={"waiting_review": True}
            )
            _write_issue_log(ctx.workspace, report)
            return report

    if ctx.config.work_mode == "in_place" and report.backup_path:
        drift = verify_annotation_backup_baseline(Path(report.backup_path), dataset_root)
        if drift:
            drift_issue = StageIssue(
                    sample_id=None,
                    relative_image_path=None,
                    module_id="commit",
                    code="baseline_drift",
                    message=f"dataset changed after backup: {', '.join(drift[:10])}",
                    blocking=True,
                )
            report.issues.append(drift_issue)
            if ctx.database is not None and ctx.job_id is not None:
                ctx.database.create_issue(
                    ctx.job_id,
                    module_id=drift_issue.module_id,
                    code=drift_issue.code,
                    severity=drift_issue.severity,
                    blocking=drift_issue.blocking,
                    message=drift_issue.message,
                )
            journal.append({"event": "commit_skipped", "reason": "baseline_drift"})
            _finish_pipeline_stage(
                ctx, state, "failed", processed=report.exported_samples, checkpoint={"reason": "baseline_drift"}
            )
            _write_issue_log(ctx.workspace, report)
            return report

    current_state = _control_state(ctx)
    if current_state in {"pausing", "paused", "cancelling", "cancelled", "interrupted"}:
        journal.append({"event": "commit_skipped", "reason": current_state})
        _finish_pipeline_stage(
            ctx, state, "skipped", processed=report.exported_samples, checkpoint={"control_state": current_state}
        )
        _write_issue_log(ctx.workspace, report)
        return report

    if ctx.resource_verifier is not None:
        try:
            ctx.resource_verifier()
        except Exception as exc:  # noqa: BLE001 - fail closed before commit
            resource_issue = StageIssue(
                sample_id=None,
                relative_image_path=None,
                module_id="resource",
                code="resource_hash_drift",
                message=f"a frozen workflow resource changed before commit: {exc}",
                blocking=True,
            )
            report.issues.append(resource_issue)
            journal.append({"event": "commit_skipped", "reason": "resource_hash_drift"})
            if ctx.database is not None and ctx.job_id is not None:
                ctx.database.create_issue(
                    ctx.job_id,
                    module_id=resource_issue.module_id,
                    code=resource_issue.code,
                    severity=resource_issue.severity,
                    blocking=True,
                    message=resource_issue.message,
                )
            _finish_pipeline_stage(
                ctx,
                state,
                "failed",
                processed=report.exported_samples,
                checkpoint={"reason": "resource_hash_drift"},
            )
            _write_issue_log(ctx.workspace, report)
            return report

    if ctx.database is not None and ctx.job_id is not None and current_state in {"running", "queued"}:
        ctx.database.update_job_status(ctx.job_id, "committing", expected_status=current_state)

    dataset_root.mkdir(parents=True, exist_ok=True)
    state.staged = list({item.relative_path: item for item in state.staged}.values())
    report.committed_files = commit_staged_files(dataset_root, staging, state.staged, journal)
    _finish_pipeline_stage(ctx, state, "completed", processed=report.exported_samples)
    _write_issue_log(ctx.workspace, report)
    return report


def _run_offline_pipeline_impl(
    config: WorkflowJobConfigV1,
    *,
    source_root: Path,
    output_root: Path,
    workspace: Path,
    replacement_index_path: Path | None = None,
    resource_fingerprints: dict[str, str] | None = None,
    resource_manifests: Mapping[str, Mapping[str, Any]] | None = None,
    tag_predictor: TagPredictor | None = None,
    policy_config: Any | None = None,
    token_counter: Any | None = None,
    classification_rules: ClassificationRules | None = None,
    ocr_engine: OCREngine | None = None,
    nl_client: NlClient | None = None,
    # The direct/offline API intentionally remains usable without a database.
    # Production runs pass both values so that samples, issues and review gates
    # are durable before a commit is attempted.
    database: Any | None = None,
    job_id: str | None = None,
    stage_tracker: _StageRunTracker | None = None,
    resource_verifier: Callable[[], None] | None = None,
) -> PipelineReport:
    """Run the deterministic offline vertical and commit its results.

    ``in_place`` work mode commits into ``source_root`` and therefore always
    creates a verified annotation backup first. ``full_copy`` commits into
    ``output_root`` and leaves the source untouched.

    The body is a linear sequence of phase functions: immutable inputs travel
    in the frozen :class:`PipelineContext`, mutable cross-phase artifacts in
    :class:`PipelineState`.  Checkpoint/resume dispatch happens in
    :func:`_restore_checkpoint` right after the import phase; the model stages
    (caption, OCR, classify, NL) skip themselves when a checkpoint is present.
    """

    ctx = PipelineContext(
        config=config,
        source_root=Path(source_root),
        output_root=Path(output_root),
        workspace=Path(workspace),
        replacement_index_path=replacement_index_path,
        resource_fingerprints=dict(resource_fingerprints or {}),
        resource_manifests=resource_manifests or {},
        tag_predictor=tag_predictor,
        policy_config=policy_config,
        token_counter=token_counter,
        classification_rules=classification_rules,
        ocr_engine=ocr_engine,
        nl_client=nl_client,
        database=database,
        job_id=job_id,
        stage_tracker=stage_tracker or _StageRunTracker(database, job_id),
        resource_verifier=resource_verifier,
    )
    ctx.workspace.mkdir(parents=True, exist_ok=True)

    state = PipelineState(
        report=PipelineReport(resource_fingerprints=dict(ctx.resource_fingerprints)),
        policy=_display_policy(config),
    )
    state.pipeline_stage_run = ctx.stage_tracker.begin("pipeline")

    _load_review_overlays(ctx, state)
    _prepare_imports(ctx, state)
    _restore_checkpoint(ctx, state)
    _write_workspace_snapshots(ctx, state)
    _run_caption_phase(ctx, state)
    _run_ocr_phase(ctx, state)
    _run_classify_phase(ctx, state)
    _prepare_replacement_rules(ctx, state)
    _prepare_staging(ctx, state)
    _prepare_export_state(ctx, state)
    _run_nl_phase(ctx, state)
    _build_upstream_projections(ctx, state)
    _run_export_phase(ctx, state)
    _sync_control_plane(ctx, state)
    return _finalize_report(ctx, state)


def run_offline_pipeline(
    config: WorkflowJobConfigV1,
    *,
    source_root: Path,
    output_root: Path,
    workspace: Path,
    replacement_index_path: Path | None = None,
    resource_fingerprints: dict[str, str] | None = None,
    resource_manifests: Mapping[str, Mapping[str, Any]] | None = None,
    tag_predictor: TagPredictor | None = None,
    policy_config: Any | None = None,
    token_counter: Any | None = None,
    classification_rules: ClassificationRules | None = None,
    ocr_engine: OCREngine | None = None,
    nl_client: NlClient | None = None,
    database: Any | None = None,
    job_id: str | None = None,
    resource_verifier: Callable[[], None] | None = None,
) -> PipelineReport:
    """Run the pipeline and close durable stage rows on every exit path."""

    tracker = _StageRunTracker(database, job_id)
    try:
        return _run_offline_pipeline_impl(
            config,
            source_root=source_root,
            output_root=output_root,
            workspace=workspace,
            replacement_index_path=replacement_index_path,
            resource_fingerprints=resource_fingerprints,
            resource_manifests=resource_manifests,
            tag_predictor=tag_predictor,
            policy_config=policy_config,
            token_counter=token_counter,
            classification_rules=classification_rules,
            ocr_engine=ocr_engine,
            nl_client=nl_client,
            database=database,
            job_id=job_id,
            stage_tracker=tracker,
            resource_verifier=resource_verifier,
        )
    finally:
        tracker.close_open()


def _write_issue_log(workspace: Path, report: PipelineReport) -> None:
    with (workspace / "issues.jsonl").open("w", encoding="utf-8") as stream:
        for issue in report.issues:
            stream.write(
                canonical_json(
                    {
                        "sample_id": issue.sample_id,
                        "relative_image_path": issue.relative_image_path,
                        "module_id": issue.module_id,
                        "code": issue.code,
                        "message": issue.message,
                        "severity": issue.severity,
                        "blocking": issue.blocking,
                        "at": utc_now(),
                    }
                )
                + "\n"
            )


__all__ = [
    "NINE_FIELDS",
    "PipelineError",
    "PipelineReport",
    "StageIssue",
    "build_projection",
    "run_offline_pipeline",
]
