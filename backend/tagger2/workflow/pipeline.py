"""Offline e621 vertical: import -> replace -> normalize -> export -> commit.

The stages implemented here are the deterministic, rule-only ones, so their
output is reproducible without any model or network access. Caption, OCR, NL,
count review, policy and token budget are separate stages that plug into the
same workspace and commit contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .caption_format import (
    CaptionDisplayPolicy,
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
from .contracts import WorkflowJobConfigV1, canonical_json, utc_now
from .dataset_import import ImportedSample, ImportResult, import_dataset
from .replacement_index import load_replacement_rules
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


def build_projection(sample: ImportedSample) -> dict[str, Any]:
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


def run_offline_pipeline(
    config: WorkflowJobConfigV1,
    *,
    source_root: Path,
    output_root: Path,
    workspace: Path,
    replacement_index_path: Path | None = None,
    resource_fingerprints: dict[str, str] | None = None,
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
    export_format = str(config.export.get("format", "both"))
    if export_format not in {"json", "txt", "both"}:
        raise PipelineError(f"unsupported export format: {export_format!r}")

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

            if rules:
                projection, summary = replace_projection(projection, rules)
                totals = totals.merge(summary)

            normalization_format = "both" if export_format == "both" else (
                "json" if export_format == "json" else "flat_txt"
            )
            result = normalize_json_bytes(
                json.dumps(projection, ensure_ascii=False).encode("utf-8"),
                policy,
                export_format=normalization_format,
            )
            if not result.valid or result.payload is None or result.json_bytes is None:
                codes = ", ".join(
                    f"{error.code}" + (f"[{error.field}]" if error.field else "")
                    for error in result.field_errors
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
                staged.append(staging.stage(sample.annotation_key + ".json", result.json_bytes))
            if export_format in {"txt", "both"}:
                staged.append(
                    staging.stage(
                        sample.annotation_key + ".txt",
                        serialize_flat_txt(result.payload, policy),
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
