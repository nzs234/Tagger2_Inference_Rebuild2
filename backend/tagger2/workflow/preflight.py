"""Workflow preflight validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..security import PathAllowlist, PathNotAllowedError
from .contracts import WorkflowJobConfigV1, WorkflowPathRef
from .resources import WorkflowResourceCatalog


class WorkflowPreflightError(Exception):
    """Raised when preflight validation fails."""
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class WorkflowPreflightService:
    """Preflight validation for workflow jobs."""

    def __init__(
        self,
        allowlist: PathAllowlist,
        resource_catalog: WorkflowResourceCatalog,
    ):
        self.allowlist = allowlist
        self.resource_catalog = resource_catalog

    def validate_config(self, config: WorkflowJobConfigV1) -> dict[str, Any]:
        """Validate workflow job configuration.
        
        Returns a report with warnings and errors. Raises WorkflowPreflightError
        if validation fails with blocking errors.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Validate source root
        try:
            source_root = self.allowlist.get(config.source_root.root_id)
            source_path = self.allowlist.resolve(
                config.source_root.root_id,
                config.source_root.relative_path
            )
            if not source_path.exists():
                errors.append(f"Source path does not exist: {config.source_root.relative_path}")
            elif not source_path.is_dir():
                errors.append(f"Source path is not a directory: {config.source_root.relative_path}")
        except PathNotAllowedError as e:
            errors.append(f"Source path not allowed: {e}")

        # Validate output root if full_copy mode
        if config.work_mode == "full_copy":
            if not config.output_root:
                errors.append("full_copy mode requires output_root")
            else:
                try:
                    output_root = self.allowlist.get(config.output_root.root_id)
                    if not output_root.writable:
                        errors.append("Output root is not writable")
                    
                    output_path = self.allowlist.resolve(
                        config.output_root.root_id,
                        config.output_root.relative_path
                    )
                    
                    # Check for overlap with source
                    if config.source_root.root_id == config.output_root.root_id:
                        source_path = self.allowlist.resolve(
                            config.source_root.root_id,
                            config.source_root.relative_path
                        )
                        try:
                            output_path.relative_to(source_path)
                            errors.append("Output path overlaps source path: output is inside source")
                        except ValueError:
                            try:
                                source_path.relative_to(output_path)
                                errors.append("Output path overlaps source path: source is inside output")
                            except ValueError:
                                pass  # No overlap
                
                except PathNotAllowedError as e:
                    errors.append(f"Output path not allowed: {e}")

        # Validate resources
        missing_resources: list[str] = []
        
        if config.caption.get("enabled"):
            resource_id = config.caption.get("resource_id")
            if resource_id:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"Caption resource not found: {resource_id}")

        if config.classify.get("enabled"):
            resource_id = config.classify.get("resource_id")
            if resource_id:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"Classify resource not found: {resource_id}")

        if config.replace.get("enabled"):
            resource_id = config.replace.get("resource_id")
            if resource_id:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"Replace resource not found: {resource_id}")

        if config.ocr.get("enabled"):
            resource_id = config.ocr.get("resource_id")
            if resource_id:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"OCR resource not found: {resource_id}")
                    warnings.append("OCR runtime may not be installed")

        if config.token_budget.get("enabled"):
            resource_id = config.token_budget.get("tokenizer_resource_id")
            if resource_id:
                # Tokenizer resources may be downloaded on-demand
                warnings.append(f"Tokenizer resource will be downloaded if not present: {resource_id}")

        if missing_resources:
            errors.extend(missing_resources)

        # Check write permissions for in_place mode
        if config.work_mode == "in_place":
            try:
                source_root = self.allowlist.get(config.source_root.root_id)
                if not source_root.writable:
                    errors.append("in_place mode requires writable source root")
            except PathNotAllowedError:
                pass  # Already reported above

        # Profile-specific validation
        if config.profile == "danbooru":
            warnings.append("Danbooru profile requires official resources (not bundled)")

        # Build report
        report: dict[str, Any] = {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

        if errors:
            raise WorkflowPreflightError(
                code="preflight_failed",
                message="Preflight validation failed",
                details=report
            )

        return report


__all__ = ["WorkflowPreflightError", "WorkflowPreflightService"]
