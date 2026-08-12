"""Offline e621 vertical: import -> replace -> normalize -> export -> commit.

The stages implemented here are the deterministic, rule-only ones, so their
output is reproducible without any model or network access. Caption, OCR, NL,
count review, policy and token budget are separate stages that plug into the
same workspace and commit contract.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stages.policy import PolicyConfig
from collections.abc import Mapping
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
from .replacement_index import load_replacement_rules
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
) -> PipelineReport:
    """Run the deterministic offline vertical and commit its results.

    ``in_place`` work mode commits into ``source_root`` and therefore always
    creates a verified annotation backup first. ``full_copy`` commits into
    ``output_root`` and leaves the source untouched.
    """

    source_root = Path(source_root)
    output_root = Path(output_root)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    report = PipelineReport(resource_fingerprints=dict(resource_fingerprints or {}))
    policy = _display_policy(config)
    stage_tracker = stage_tracker or _StageRunTracker(database, job_id)
    pipeline_stage_run = stage_tracker.begin("pipeline")

    def finish_pipeline_stage(status: str, *, processed: int = 0, checkpoint: dict[str, Any] | None = None) -> None:
        if pipeline_stage_run is not None:
            stage_tracker.update(
                "pipeline",
                status,
                total=report.total_samples,
                processed=processed,
                issue_count=len(report.issues),
                checkpoint=checkpoint,
            )

    confirmed_counts: dict[int, str] = {}
    applied_token_texts: dict[int, str] = {}
    if database is not None and job_id is not None:
        # Reviews are overlays on the immutable import/stage result.  Loading
        # them before processing means a resumed run produces the exact same
        # bytes as the first run plus the reviewer's decisions.
        from .count_review import CountReviewStore
        from .token_budget_review import TokenBudgetReviewStore

        confirmed_counts = CountReviewStore(database, job_id).confirmed_counts()
        applied_token_texts = TokenBudgetReviewStore(database, job_id).applied_texts()

    import_stage_run = stage_tracker.begin(
        "import",
        checkpoint={"checkpoint": "discovering"},
    )
    imported: ImportResult = import_dataset(
        source_root,
        recursive=config.recursive,
        input_txt_mode=str(config.caption.get("input_txt_mode", "tag")),
    )
    report.total_samples = len(imported.samples)
    if import_stage_run is not None:
        stage_tracker.update(
            "import",
            "completed",
            total=report.total_samples,
            processed=report.total_samples,
            issue_count=len(imported.issues),
            checkpoint={"checkpoint": "imported", "sample_count": report.total_samples},
        )
    if pipeline_stage_run is not None:
        stage_tracker.update(
            "pipeline",
            "running",
            total=report.total_samples,
            checkpoint={"checkpoint": "imported"},
        )

    if database is not None and job_id is not None:
        # The manifest is the source of truth for the control plane.  ``INSERT
        # OR IGNORE`` keeps a retry/recovery run idempotent and, importantly,
        # makes samples visible before any expensive stage starts.
        with database.connection() as conn:
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
                        job_id,
                        sample.sample_id,
                        sample.relative_image_path,
                        sample.image_format,
                        now,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE workflow_jobs SET total_samples = ? WHERE job_id = ?",
                (len(imported.samples), job_id),
            )
    for issue in imported.issues:
        report.issues.append(
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
        report.failed_samples += 1

    # Freeze the input manifest before anything is written.
    manifest_path = workspace / "input_manifest.jsonl"
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
    (workspace / "config_snapshot.json").write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    resource_snapshot = {
        resource_id: {
            "fingerprint": fingerprint,
            "manifest": dict((resource_manifests or {}).get(resource_id, {})),
        }
        for resource_id, fingerprint in (resource_fingerprints or {}).items()
    }
    (workspace / "resource_snapshot.json").write_text(
        json.dumps(resource_snapshot, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    if database is not None and job_id is not None:
        with database.connection() as conn:
            for resource_id, fingerprint in (resource_fingerprints or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO workflow_resource_snapshots "
                    "(job_id, resource_id, resource_fingerprint, manifest_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        job_id,
                        resource_id,
                        fingerprint,
                        canonical_json((resource_manifests or {}).get(resource_id, {})),
                        utc_now(),
                    ),
                )

    caption_tags: dict[str, tuple[str, ...]] = {}
    if config.caption.get("enabled"):
        if tag_predictor is None:
            raise PipelineError("caption stage is enabled but no tag predictor was provided")
        caption_report: CaptionStageReport = run_caption_stage(
            imported.samples,
            source_root=source_root,
            predictor=tag_predictor,
            settings=settings_from_config(config.caption),
            model_id=str(config.caption.get("model_id", "")),
        )
        report.caption = {
            "captioned": caption_report.captioned,
            "skipped": caption_report.skipped,
            "failed": caption_report.failed,
        }
        for result in caption_report.results:
            if result.error:
                report.issues.append(
                    StageIssue(
                        sample_id=None,
                        relative_image_path=result.relative_image_path,
                        module_id="caption",
                        code="caption_failed",
                        message=result.error,
                    )
                )
                report.failed_samples += 1
            elif not result.skipped:
                caption_tags[result.relative_image_path] = tuple(
                    tag.raw_tag for tag in result.tags
                )


    # OCR stage: text recognition into per-sample sidecars. It never changes
    # the nine-field payload and never blocks the run, so a missing runtime or a
    # failed image is a warning and the pipeline continues.
    ocr_results: dict[str, Any] = {}
    if config.ocr.get("enabled"):
        ocr_results, ocr_issues = run_ocr_stage(
            workspace,
            {
                sample.relative_image_path: source_root / sample.relative_image_path
                for sample in imported.samples
            },
            config.ocr,
            ocr_engine,
        )
        report.ocr = {
            "processed": sum(1 for result in ocr_results.values() if result.success),
            "failed": sum(1 for result in ocr_results.values() if not result.success),
            "regions": sum(
                len(result.detected_regions) for result in ocr_results.values() if result.success
            ),
        }
        for ocr_issue in ocr_issues:
            report.issues.append(
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

    # Classify stage: map caption tags to nine-field structure
    classified_projections: dict[str, dict[str, list[str]]] = {}
    if classification_rules is not None:
        # Build a combined tags source: prefer caption_tags, fallback to imported tags
        tags_to_classify: dict[str, tuple[str, ...]] = {}
        
        # First, add imported tags for samples with tag_txt or raw_e621_json
        for sample in imported.samples:
            if sample.annotation_kind in ("tag_txt", "raw_e621_json") and sample.tags:
                tags_to_classify[sample.relative_image_path] = sample.tags
        
        # Caption tags override imported tags
        tags_to_classify.update(caption_tags)
        
        for relative_path, tags in tags_to_classify.items():
            try:
                classified_projections[relative_path] = classify_tags(
                    list(tags), classification_rules
                )
            except Exception as exc:  # noqa: BLE001
                report.issues.append(
                    StageIssue(
                        sample_id=None,
                        relative_image_path=relative_path,
                        module_id="classify",
                        code="classify_failed",
                        message=str(exc),
                    )
                )
                report.failed_samples += 1

    rules = {}
    if config.replace.get("enabled"):
        if replacement_index_path is None:
            raise PipelineError("replace stage is enabled but no replacement index was provided")
        rules = load_replacement_rules(Path(replacement_index_path))

    dataset_root = source_root if config.work_mode == "in_place" else output_root

    if config.work_mode == "in_place" and imported.samples:
        # Never modify a dataset in place without a verified backup first.
        backup_target = workspace / "backup" / "annotations.zip"
        if backup_target.is_file():
            # Review-gated retries must reuse the original pre-workflow
            # baseline; taking a second backup would capture an intermediate
            # overlay and make Restore semantically incorrect.
            report.backup_path = str(backup_target)
        else:
            report.backup_path = str(
                write_annotation_backup(
                    source_root,
                    backup_target,
                    (sample.annotation_key for sample in imported.samples),
                )
            )
    staging = ExportStaging(workspace / "staging")
    journal = CommitJournal(
        workspace / "commit_journal.jsonl",
        database=database,
        job_id=job_id,
    )
    staged: list[StagedFile] = []
    exported_sample_ids: set[int] = set()
    # Persist complete projections independently of staging.  Review actions
    # use this immutable workspace overlay to rebuild JSON/TXT bytes without
    # re-running model stages or touching the target dataset.
    review_projections: dict[str, dict[str, Any]] = {}
    totals = ReplacementSummary(0, 0, 0, 0)
    policy_counts: dict[str, int] = {"artist_dropped": 0, "quality_dropped": 0}
    budget_counts: dict[str, int] = {}
    # Public API keeps the stable ``txt`` spelling; the normalizer uses the
    # internal ``flat_txt`` representation only at the serialization boundary.
    export_format = str(config.export.get("format", "both"))
    if export_format == "txt":
        export_format = "flat_txt"
    if export_format not in {"json", "flat_txt", "both"}:
        raise PipelineError(f"unsupported export format: {export_format!r}")

    def control_state() -> str | None:
        """Read the durable control state at safe batch boundaries."""

        if database is None or job_id is None:
            return None
        current = database.get_job(job_id)
        return None if current is None else str(current["status"])


    # NL stage: generate natural language captions
    nl_projections: dict[str, Any] = {}
    if config.nl.get('enabled') and config.nl.get('api_enabled') and nl_client is not None:
        # Build projections dict for NL stage (needs full nine-field structure)
        temp_projections: dict[str, dict[str, Any]] = {}
        for sample in imported.samples:
            if sample.annotation_kind == 'standard_json':
                document = json.loads(
                    (source_root / Path(sample.annotation_key + '.json')).read_bytes().decode('utf-8-sig')
                )
                temp_projections[sample.relative_image_path] = {
                    field_name: document.get(field_name) for field_name in NINE_FIELDS
                }
                if not temp_projections[sample.relative_image_path]["tags"]:
                    temp_projections[sample.relative_image_path]["tags"] = list(
                        sample.tags
                    )
            else:
                projection = dict(build_projection(sample))
                classified = classified_projections.get(sample.relative_image_path)
                if classified:
                    projection['quality'] = classified.get('quality', [])
                    if sample.annotation_kind == 'raw_e621_json':
                        projection['tags'] = [
                            tag for tag in classified.get('tags', [])
                            if tag != projection['character']
                        ]
                    else:
                        projection['character'] = ', '.join(classified.get('character', []))
                        projection['tags'] = classified.get('tags', [])
                        projection['artist'] = merge_artists(
                            str(projection['artist']),
                            ', '.join(classified.get('artist', [])),
                        )
                    projection['appearance'] = classified.get('appearance', [])
                    projection['environment'] = classified.get('environment', [])
                elif caption_tags.get(sample.relative_image_path):
                    projection['tags'] = list(caption_tags[sample.relative_image_path])
                temp_projections[sample.relative_image_path] = dict(projection)
        
        nl_report = run_nl_stage(
            imported.samples,
            temp_projections,
            source_root=source_root,
            client=nl_client,
            preset=str(config.nl.get('prompt_preset', 'general')),
            length=str(config.nl.get('length', 'medium')),
            reuse_original_nl=bool(config.nl.get('reuse_original_nl', True)),
            use_image=bool(config.nl.get('use_image', True)),
            use_full_json=bool(config.nl.get('use_full_json', False)),
            ocr_by_path={
                relative_path: {
                    "regions": result.detected_regions,
                    "success": result.success,
                }
                for relative_path, result in ocr_results.items()
            },
        )
        nl_projections = nl_report.by_path()
        report.nl = {
            'generated': nl_report.generated,
            'reused': nl_report.reused,
            'failed': nl_report.failed,
        }

    export_stage_run = stage_tracker.begin(
        "export",
        total=report.total_samples,
        checkpoint={"checkpoint": "staging"},
    )
    for sample in imported.samples:
        if control_state() in {"pausing", "paused", "cancelling", "cancelled", "interrupted"}:
            break
        try:
            if sample.annotation_kind == "standard_json":
                document = json.loads(
                    (source_root / Path(sample.annotation_key + ".json")).read_bytes().decode("utf-8-sig")
                )
                projection = {field_name: document.get(field_name) for field_name in NINE_FIELDS}
                projection = {
                    key: ([] if value is None and key in {"quality", "appearance", "tags", "environment"} else value)
                    for key, value in projection.items()
                }
                if not projection["tags"]:
                    projection["tags"] = list(sample.tags)
                projection = {
                    key: ("" if value is None else value) for key, value in projection.items()
                }
            else:
                projection = dict(build_projection(sample))
                
                # Use classified projection if available, otherwise fall back to raw tags
                classified = classified_projections.get(sample.relative_image_path)
                if classified:
                    # Merge classified fields into projection
                    projection["quality"] = classified.get("quality", [])
                    # For raw_e621_json, preserve original character/artist from import
                    if sample.annotation_kind == "raw_e621_json":
                        # Remove character from tags to avoid collision
                        projection["tags"] = [
                            tag
                            for tag in classified.get("tags", [])
                            if tag != projection["character"]
                        ]
                    else:
                        projection["character"] = ", ".join(classified.get("character", []))
                        projection["tags"] = classified.get("tags", [])
                        # Classified artist tags would otherwise be dropped
                        # entirely; merge them into the `artist` string field
                        # without overwriting an artist supplied by the input.
                        projection["artist"] = merge_artists(
                            str(projection["artist"]),
                            ", ".join(classified.get("artist", [])),
                        )
                    projection["appearance"] = classified.get("appearance", [])
                    projection["environment"] = classified.get("environment", [])
                elif caption_tags.get(sample.relative_image_path):
                    # No classification rules, use raw caption tags
                    projection["tags"] = list(caption_tags[sample.relative_image_path])
                elif config.caption.get("enabled") and not sample.skip_caption:
                    # Caption was requested but produced nothing usable for this
                    # sample; its failure is already recorded as an issue.
                    continue

            if rules:
                projection, summary = replace_projection(projection, rules)
                totals = totals.merge(summary)

            # Convert dict to PolicyConfig dataclass
            if policy_config is not None:
                parsed_policy_config = _parse_policy_config(policy_config)
                policy_config = parsed_policy_config

            if policy_config is not None:
                try:
                    projection, decision = apply_policy(
                        projection,
                        annotation_key=sample.annotation_key,
                        relative_image_path=sample.relative_image_path,
                        config=policy_config,
                        aesthetic_score=None,
                    )
                except PolicyError as exc:
                    report.issues.append(
                        StageIssue(
                            sample_id=sample.sample_id,
                            relative_image_path=sample.relative_image_path,
                            module_id="policy",
                            code="policy_failed",
                            message=str(exc),
                        )
                    )
                    report.failed_samples += 1
                    continue
                policy_counts["artist_dropped"] += int(decision.artistDropped)
                policy_counts["quality_dropped"] += int(decision.qualityDropped)
                policy_counts[decision.appearanceNlAction] = (
                    policy_counts.get(decision.appearanceNlAction, 0) + 1
                )

            # Merge NL result if available
            if nl_projections and sample.relative_image_path in nl_projections:
                projection["nl"] = nl_projections[sample.relative_image_path].nl

            # Human decisions are applied as an overlay.  They are deliberately
            # evaluated before policy/token/export so every downstream stage
            # sees the reviewed values.
            if sample.sample_id in confirmed_counts:
                projection["count"] = confirmed_counts[sample.sample_id]
            if sample.sample_id in applied_token_texts:
                projection["nl"] = applied_token_texts[sample.sample_id]

            # Preserve the pre-budget projection even when the budget stage
            # later parks this sample for human review.
            review_projections[str(sample.sample_id)] = dict(projection)

            # ``token_counter`` is an injected capability, but the stage is
            # still controlled by the immutable job contract.  A caller may
            # provide a tokenizer for another reason (or reuse a pipeline
            # helper) without accidentally enabling token review on a job that
            # explicitly disabled it.
            if config.token_budget.get("enabled") and token_counter is not None:
                caption_format = {
                    "replaceUnderscoresWithSpaces": policy.replace_underscores_with_spaces,
                    "preserveEscapes": policy.preserve_escapes,
                    "triggersEnabled": policy.triggers_enabled,
                    "triggerTerms": list(policy.trigger_terms),
                }
                try:
                    budget = fit_token_budget(
                        projection,
                        caption_format,
                        int(config.token_budget.get("max_tokens", 225)),
                        token_counter,
                    )
                except FlatTextSerializationError as exc:
                    # Trimming reached an empty payload, which cannot be
                    # serialized. That is an overflow needing a human decision.
                    report.issues.append(
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
                    report.token_overflows.append(
                        {
                            "sample_id": sample.sample_id,
                            "relative_image_path": sample.relative_image_path,
                            "nl_text": _safe_flat_txt(projection, policy),
                            "token_count": int(config.token_budget.get("max_tokens", 225)) + 1,
                            "token_limit": int(config.token_budget.get("max_tokens", 225)),
                        }
                    )
                    budget_counts["overflow"] = budget_counts.get("overflow", 0) + 1
                    report.failed_samples += 1
                    continue
                except TokenBudgetError as exc:
                    report.issues.append(
                        StageIssue(
                            sample_id=sample.sample_id,
                            relative_image_path=sample.relative_image_path,
                            module_id="token_budget",
                            code="token_budget_failed",
                            message=str(exc),
                        )
                    )
                    report.failed_samples += 1
                    continue
                budget_counts[budget.status] = budget_counts.get(budget.status, 0) + 1
                if budget.status == "overflow" or budget.annotation is None:
                    # Overflow needs a human decision; never silently truncate.
                    report.issues.append(
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
                    report.token_overflows.append(
                        {
                            "sample_id": sample.sample_id,
                            "relative_image_path": sample.relative_image_path,
                            "nl_text": _safe_flat_txt(
                                budget.annotation or projection, policy
                            ),
                            "token_count": budget.original_tokens,
                            "token_limit": int(config.token_budget.get("max_tokens", 225)),
                        }
                    )
                    report.failed_samples += 1
                    continue
                projection = dict(budget.annotation)

            review_projections[str(sample.sample_id)] = dict(projection)

            normalization_format = "both" if export_format == "both" else (
                "json" if export_format == "json" else "flat_txt"
            )
            normalized = normalize_json_bytes(
                json.dumps(projection, ensure_ascii=False).encode("utf-8"),
                policy,
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
                report.issues.append(
                    StageIssue(
                        sample_id=sample.sample_id,
                        relative_image_path=sample.relative_image_path,
                        module_id="export",
                        code="normalization_failed",
                        message=f"payload rejected: {codes}",
                    )
                )
                report.failed_samples += 1
                continue

            if export_format in {"json", "both"}:
                staged.append(
                    staging.stage(sample.annotation_key + ".json", normalized.json_bytes)
                )
            if export_format in {"flat_txt", "both"}:
                staged.append(
                    staging.stage(
                        sample.annotation_key + ".txt",
                        serialize_flat_txt(normalized.payload, policy),
                    )
                )
            # `full_copy` writes into a separate output root, so the image has
            # to travel with its annotation or the output is not a usable
            # dataset. `in_place` already has the image where it belongs.
            # Staged before the counter so a read failure below is counted once,
            # as a failure, rather than as both exported and failed.
            if config.work_mode == "full_copy":
                source_image = source_root / sample.relative_image_path
                if source_image.is_file():
                    staged.append(
                        staging.stage(
                            sample.relative_image_path, source_image.read_bytes()
                        )
                    )

            report.exported_samples += 1
            exported_sample_ids.add(sample.sample_id)

        except (ReplacementError, ValueError, OSError) as exc:
            report.issues.append(
                StageIssue(
                    sample_id=sample.sample_id,
                    relative_image_path=sample.relative_image_path,
                    module_id="export",
                    code="sample_failed",
                    message=str(exc),
                )
            )
            report.failed_samples += 1

    if export_stage_run is not None:
        export_control_state = control_state()
        export_status = (
            "skipped"
            if export_control_state
            in {"pausing", "paused", "cancelling", "cancelled", "interrupted"}
            else "completed"
        )
        stage_tracker.update(
            "export",
            export_status,
            total=report.total_samples,
            processed=report.exported_samples,
            issue_count=len(report.issues),
            checkpoint={
                "checkpoint": "staged",
                "exported_samples": report.exported_samples,
                "failed_samples": report.failed_samples,
            },
        )

    report.policy = dict(policy_counts)
    report.token_budget = dict(budget_counts)
    report.replacement = {
        "replaced": totals.replaced,
        "dropped": totals.dropped,
        "passthrough": totals.passthrough,
        "keep_rewritten": totals.keep_rewritten,
    }

    review_stage_run = stage_tracker.begin(
        "review",
        total=report.total_samples,
        checkpoint={"checkpoint": "overlay"},
    )
    if database is not None and job_id is not None:
        # The control plane must reflect stage completion even when the final
        # dataset commit is deferred for review.  A sample marked completed
        # here means its private overlay/staging bytes are valid, not that the
        # target dataset has already changed.
        issue_sample_ids = {
            issue.sample_id
            for issue in report.issues
            if issue.sample_id is not None and issue.severity == "error"
        }
        with database.connection() as conn:
            now = utc_now()
            for sample in imported.samples:
                if sample.sample_id in exported_sample_ids:
                    sample_status = "completed"
                elif sample.sample_id in issue_sample_ids:
                    sample_status = "failed"
                else:
                    sample_status = "skipped"
                conn.execute(
                    "UPDATE workflow_samples SET status = ?, updated_at = ? WHERE job_id = ? AND sample_id = ?",
                    (sample_status, now, job_id, sample.sample_id),
                )
            conn.execute(
                """
                UPDATE workflow_jobs
                   SET processed_samples = ?, succeeded_samples = ?,
                       failed_samples = ?, skipped_samples = ?
                 WHERE job_id = ?
                """,
                (
                    report.exported_samples + report.failed_samples + report.skipped_samples,
                    report.exported_samples,
                    report.failed_samples,
                    report.skipped_samples,
                    job_id,
                ),
            )

    if database is not None and job_id is not None:
        # Keep a durable projection snapshot for count/token review.  It is
        # deliberately stored under the private workspace, never returned by
        # the API, and is replaced atomically on a recovery run.
        projection_path = workspace / "projections.json"
        temporary = projection_path.with_suffix(".json.partial")
        temporary.write_text(
            json.dumps(review_projections, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(projection_path)

        # Count review is a production gate, not a test-only helper.  The
        # bundled wiki snapshot is optional; an empty catalog makes the rules
        # preserve the original value and exposes the uncertainty to the
        # reviewer instead of fabricating a count.
        if config.count_review.get("enabled") and imported.samples:
            from .count_review import (
                CountReviewStore,
                create_wiki_catalog,
                derive_count_decisions,
            )

            wiki_db = create_wiki_catalog(workspace / "wiki_catalog.sqlite3")

            class _ReviewSample:
                def __init__(self, sample_id: int, relative_image_path: str):
                    self.sample_id = sample_id
                    self.relative_image_path = relative_image_path

            evidence = derive_count_decisions(
                [
                    _ReviewSample(sample.sample_id, sample.relative_image_path)
                    for sample in imported.samples
                    if str(sample.sample_id) in review_projections
                ],
                {
                    next(
                        sample.relative_image_path
                        for sample in imported.samples
                        if sample.sample_id == int(sample_id)
                    ): projection
                    for sample_id, projection in review_projections.items()
                },
                wiki_db_path=wiki_db,
                observations={
                    relative_path: result.observation
                    for relative_path, result in nl_projections.items()
                },
                overwrite_count=bool(config.classify.get("overwrite_count", False)),
            )
            CountReviewStore(database, job_id).initialize(evidence)

        # Token rows are initialized here as well as by the API for backward
        # compatibility.  ``INSERT OR IGNORE`` makes the two paths harmless.
        if report.token_overflows:
            from .token_budget_review import TokenBudgetReviewStore

            TokenBudgetReviewStore(database, job_id).initialize(report.token_overflows)

        # Every issue is visible in the control plane before the job can enter
        # a waiting state. Keep prior rows for audit; recovery attempts get a
        # distinct issue id and the durable event cursor preserves history.
        for report_issue in report.issues:
            database.create_issue(
                job_id,
                module_id=report_issue.module_id,
                code=report_issue.code,
                severity=report_issue.severity,
                blocking=report_issue.blocking,
                message=report_issue.message,
                sample_id=report_issue.sample_id,
            )
    if review_stage_run is not None:
        stage_tracker.update(
            "review",
            "completed",
            total=report.total_samples,
            processed=report.exported_samples,
            issue_count=len(report.issues),
            checkpoint={"checkpoint": "overlay_ready"},
        )

    blocking = [issue for issue in report.issues if issue.blocking]
    if blocking:
        # Fail closed: a blocking issue must not produce a half-written dataset.
        journal.append({"event": "commit_skipped", "blocking_issues": len(blocking)})
        finish_pipeline_stage("failed", processed=report.exported_samples)
        _write_issue_log(workspace, report)
        return report

    # In production a review-gated run must leave all output in the private
    # staging tree.  The API resumes this checkpoint after Count/Token review
    # and performs the sole commit then.  Direct callers (the deterministic
    # offline tests and CLI) retain the historical immediate-commit behaviour.
    if database is not None and job_id is not None:
        from .count_review import CountReviewStore
        from .token_budget_review import TokenBudgetReviewStore

        if (
            (config.count_review.get("enabled") and CountReviewStore(database, job_id).pending_count())
            or (
                config.token_budget.get("enabled")
                and TokenBudgetReviewStore(database, job_id).unresolved_count()
            )
        ):
            journal.append({"event": "commit_deferred_for_review"})
            finish_pipeline_stage("skipped", processed=report.exported_samples, checkpoint={"waiting_review": True})
            _write_issue_log(workspace, report)
            return report

    if config.work_mode == "in_place" and report.backup_path:
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
            if database is not None and job_id is not None:
                database.create_issue(
                    job_id,
                    module_id=drift_issue.module_id,
                    code=drift_issue.code,
                    severity=drift_issue.severity,
                    blocking=drift_issue.blocking,
                    message=drift_issue.message,
                )
            journal.append({"event": "commit_skipped", "reason": "baseline_drift"})
            finish_pipeline_stage("failed", processed=report.exported_samples, checkpoint={"reason": "baseline_drift"})
            _write_issue_log(workspace, report)
            return report

    current_state = control_state()
    if current_state in {"pausing", "paused", "cancelling", "cancelled", "interrupted"}:
        journal.append({"event": "commit_skipped", "reason": current_state})
        finish_pipeline_stage("skipped", processed=report.exported_samples, checkpoint={"control_state": current_state})
        _write_issue_log(workspace, report)
        return report

    if database is not None and job_id is not None and current_state in {"running", "queued"}:
        database.update_job_status(job_id, "committing", expected_status=current_state)

    dataset_root.mkdir(parents=True, exist_ok=True)
    report.committed_files = commit_staged_files(dataset_root, staging, staged, journal)
    if database is not None and job_id is not None and hasattr(database, "record_artifact"):
        for item in staged:
            database.record_artifact(
                job_id,
                kind="staged_export",
                relative_path=item.relative_path,
                sha256=item.sha256,
                size_bytes=item.size,
            )
    finish_pipeline_stage("completed", processed=report.exported_samples)
    _write_issue_log(workspace, report)
    return report


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


def _safe_flat_txt(annotation: Mapping[str, object], policy: CaptionDisplayPolicy) -> str:
    """Flatten a caption for review, tolerating a payload that cannot serialize.

    An overflow row only needs readable text for the reviewer, so a payload that
    trimming emptied returns "" instead of failing the whole run.
    """

    try:
        return serialize_flat_txt(annotation, policy).decode("utf-8", "replace")
    except (FlatTextSerializationError, TypeError, ValueError):
        return ""



