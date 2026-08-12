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
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Mapping
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
    write_annotation_backup,
)
from .contracts import NineFieldAnnotation, WorkflowJobConfigV1, canonical_json, utc_now
from .dataset_import import ImportedSample, ImportResult, import_dataset
from .replacement_index import load_replacement_rules
from .ocr import OCREngine, run_ocr_stage
from .stages.nl import NlClient, run_nl_stage
from .stages.classify import ClassificationRules, classify_tags
from .stages.caption import (
    CaptionStageReport,
    TagPredictor,
    run_caption_stage,
    settings_from_config,
)
from .stages.policy import PolicyError, apply_policy, merge_artists
from .stages.token_budget import TokenBudgetError, fit as fit_token_budget
from .stages.replacement import ReplacementError, ReplacementSummary, replace_projection

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


def _display_policy(config: WorkflowJobConfigV1) -> CaptionDisplayPolicy:
    caption = config.caption
    return CaptionDisplayPolicy(
        replace_underscores_with_spaces=bool(caption.get("replace_underscores_with_spaces", True)),
        preserve_escapes=bool(caption.get("preserve_escapes", True)),
        triggers_enabled=bool(caption.get("triggers_enabled", False)),
        trigger_terms=tuple(caption.get("trigger_terms", ())),
    )


def build_projection(sample: ImportedSample) -> NineFieldAnnotation:
    """Build the nine-field projection for one imported sample.

    A standard JSON annotation is reused as-is; a raw e621 document contributes
    its artist/character plus classify tags. ``series`` stays empty for raw e621
    input, matching the source project's behaviour.
    """

    if sample.annotation_kind == "standard_json":
        raise PipelineError("standard JSON is handled by the caller")

    projection: dict[str, Any] = {
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


def _parse_policy_config(config_arg: dict[str, Any] | "PolicyConfig") -> "PolicyConfig":
    """Convert a policy config dictionary to PolicyConfig dataclass, or pass through if already a dataclass."""
    from .stages.policy import PolicyConfig, CoupledProbabilities
    
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


def run_offline_pipeline(
    config: WorkflowJobConfigV1,
    *,
    source_root: Path,
    output_root: Path,
    workspace: Path,
    replacement_index_path: Path | None = None,
    resource_fingerprints: dict[str, str] | None = None,
    tag_predictor: TagPredictor | None = None,
    policy_config: Any | None = None,
    token_counter: Any | None = None,
    classification_rules: ClassificationRules | None = None,
    ocr_engine: OCREngine | None = None,
    nl_client: NlClient | None = None,
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

    imported: ImportResult = import_dataset(
        source_root,
        recursive=config.recursive,
        input_txt_mode=str(config.caption.get("input_txt_mode", "tag")),
    )
    report.total_samples = len(imported.samples)
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

    caption_tags: dict[str, tuple[str, ...]] = {}
    if config.caption.get("enabled"):
        if tag_predictor is None:
            raise PipelineError("caption stage is enabled but no tag predictor was provided")
        caption_report: CaptionStageReport = run_caption_stage(
            imported.samples,
            source_root=source_root,
            predictor=tag_predictor,
            settings=settings_from_config(config.caption),
            model_id=str(config.caption.get("resource_id", "")),
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
            except Exception as exc:
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
        report.backup_path = str(
            write_annotation_backup(
                source_root,
                workspace / "backup" / "annotations.zip",
                (sample.annotation_key for sample in imported.samples),
            )
        )

    staging = ExportStaging(workspace / "staging")
    journal = CommitJournal(workspace / "commit_journal.jsonl")
    staged: list[StagedFile] = []
    totals = ReplacementSummary(0, 0, 0, 0)
    policy_counts: dict[str, int] = {"artist_dropped": 0, "quality_dropped": 0}
    budget_counts: dict[str, int] = {}
    export_format = str(config.export.get("format", "both"))
    if export_format not in {"json", "flat_txt", "both"}:
        raise PipelineError(f"unsupported export format: {export_format!r}")


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
            else:
                projection = build_projection(sample)
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
                temp_projections[sample.relative_image_path] = projection
        
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
        )
        nl_projections = nl_report.by_path()
        report.nl = {
            'generated': nl_report.generated,
            'reused': nl_report.reused,
            'failed': nl_report.failed,
        }

    for sample in imported.samples:
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
                projection = {
                    key: ("" if value is None else value) for key, value in projection.items()
                }
            else:
                projection = build_projection(sample)
                
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
                projection["nl"] = nl_projections[sample.relative_image_path]

            if token_counter is not None:
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

    report.policy = dict(policy_counts)
    report.token_budget = dict(budget_counts)
    report.replacement = {
        "replaced": totals.replaced,
        "dropped": totals.dropped,
        "passthrough": totals.passthrough,
        "keep_rewritten": totals.keep_rewritten,
    }

    blocking = [issue for issue in report.issues if issue.blocking]
    if blocking:
        # Fail closed: a blocking issue must not produce a half-written dataset.
        journal.append({"event": "commit_skipped", "blocking_issues": len(blocking)})
        _write_issue_log(workspace, report)
        return report

    dataset_root.mkdir(parents=True, exist_ok=True)
    report.committed_files = commit_staged_files(dataset_root, staging, staged, journal)
    _write_issue_log(workspace, report)
    return report


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



