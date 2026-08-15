"""Workflow preflight validation."""

from __future__ import annotations

from typing import Any

from ..security import PathAllowlist, PathNotAllowedError
from .contracts import WorkflowJobConfigV1
from .db import WorkflowDatabase
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
        database: WorkflowDatabase,
        *,
        model_registry: Any | None = None,
        inference_engine: Any | None = None,
    ):
        self.allowlist = allowlist
        self.resource_catalog = resource_catalog
        self.database = database
        self.model_registry = model_registry
        self.inference_engine = inference_engine

    def _snapshot_profile(self, resource_id: str) -> str | None:
        """Return the profile a registered classification snapshot was built for.

        Returns ``None`` when the file is missing or unreadable, so the caller
        reports a blocking error instead of assuming a profile.
        """

        import json

        path = self.resource_catalog.get_resource_path(resource_id)
        if path is None or not path.is_file():
            return None
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        profile = document.get("profile")
        return profile if isinstance(profile, str) else None


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

        # Dataset lock: mirror the authoritative start-time scope calculation
        # for an early diagnostic.  The transaction in ``start_job`` remains
        # the final arbiter because preflight can race another request.
        scopes = [(config.source_root.root_id, config.source_root.relative_path)]
        if config.output_root is not None:
            scopes.append((config.output_root.root_id, config.output_root.relative_path))
        active_jobs = self.database.get_active_jobs_for_scopes(scopes)
        if active_jobs:
            job_ids = [str(job["job_id"])[:8] for job in active_jobs]
            errors.append(
                f"Dataset is locked by active job(s): {', '.join(job_ids)}. "
                "Wait for them to finish or cancel them before creating a new job."
            )

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
            model_id = config.caption.get("model_id") or config.caption.get("resource_id")
            if not model_id:
                missing_resources.append("Caption is enabled but no model_id is selected")
            elif self.model_registry is not None:
                try:
                    record = self.model_registry.get_model(str(model_id))
                except (AttributeError, KeyError, LookupError, ValueError, RuntimeError):
                    record = None
                if record is None:
                    missing_resources.append(
                        f"Caption local model is not registered: {model_id}"
                    )
                else:
                    loaded_ids = {
                        str(value)
                        for value in getattr(self.inference_engine, "loaded_model_ids", ())
                        if str(value)
                    }
                    if not bool(getattr(record, "loaded", False)) and str(
                        getattr(record, "model_id", "")
                    ) not in loaded_ids:
                        missing_resources.append(
                            f"Caption local model is not loaded: {model_id}"
                        )

        if config.classify.get("enabled"):
            resource_id = config.classify.get("resource_id")
            if not resource_id:
                missing_resources.append(
                    "Classify is enabled but no classification snapshot is selected"
                )
            else:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"Classify resource not found: {resource_id}")
                else:
                    if self.resource_catalog.get_resource_path(resource_id) is None:
                        missing_resources.append(
                            f"Classify resource digest verification failed: {resource_id}"
                        )
                    # A snapshot built for another profile must never be used as a
                    # substitute, so the profile is checked here rather than at run
                    # time when the dataset is already being processed.
                    snapshot_profile = self._snapshot_profile(resource_id)
                    if snapshot_profile is None:
                        missing_resources.append(
                            "Classify snapshot cannot be read:"
                            f" {resource_id}"
                        )
                    elif snapshot_profile != config.profile:
                        missing_resources.append(
                            f"Classify snapshot {resource_id} is built for profile"
                            f" {snapshot_profile!r}, but the job profile is"
                            f" {config.profile!r}; no cross-profile fallback is allowed"
                        )

        if config.replace.get("enabled"):
            resource_id = config.replace.get("resource_id")
            if not resource_id:
                missing_resources.append("Replace is enabled but no replacement resource_id is selected")
            else:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"Replace resource not found: {resource_id}")
                elif manifest.category not in {"replace", "replacement_index"}:
                    missing_resources.append(
                        f"Replace resource {resource_id} has incompatible category {manifest.category!r}"
                    )
                elif self.resource_catalog.get_resource_path(resource_id) is None:
                    missing_resources.append(
                        f"Replace resource digest verification failed: {resource_id}"
                    )

        nl_provider_id = str(config.nl.get("provider_id") or "")
        if config.nl.get("enabled") and nl_provider_id and config.nl.get("use_image") is not True:
            errors.append("NL API requires image input when a provider is selected")

        if config.ocr.get("enabled"):
            resource_id = config.ocr.get("resource_id")
            if resource_id:
                manifest = self.resource_catalog.get_manifest(resource_id)
                if not manifest:
                    missing_resources.append(f"OCR resource not found: {resource_id}")
                elif manifest.category != "ocr":
                    missing_resources.append(
                        f"OCR resource {resource_id} has incompatible category {manifest.category!r}"
                    )
                else:
                    descriptor_path = self.resource_catalog.get_resource_path(resource_id)
                    if descriptor_path is None:
                        missing_resources.append(
                            f"OCR resource digest verification failed: {resource_id}"
                        )
                    else:
                        descriptor_report = self.resource_catalog.validate_resource(
                            descriptor_path, "ocr"
                        )
                        if not descriptor_report.get("valid"):
                            missing_resources.extend(
                                f"OCR resource {resource_id}: {error}"
                                for error in descriptor_report.get("errors", [])
                            )
            else:
                missing_resources.append("OCR is enabled but no OCR resource_id is selected")

            # OCR is an isolated runtime, not an optional warning in a
            # production job.  A missing interpreter must fail closed before
            # any sample is touched.
            from .ocr import load_ocr_engine_from_resource
            try:
                descriptor_path = self.resource_catalog.get_resource_path(
                    str(config.ocr.get("resource_id") or "")
                )
                if descriptor_path is not None:
                    load_ocr_engine_from_resource(descriptor_path)
            except RuntimeError as exc:
                missing_resources.append(f"OCR runtime unavailable: {exc}")

        if config.token_budget.get("enabled"):
            resource_id = config.token_budget.get("tokenizer_resource_id")
            if not resource_id:
                missing_resources.append("Token budget is enabled but no tokenizer resource_id is selected")
            elif not self.resource_catalog.get_manifest(str(resource_id)):
                missing_resources.append(f"Tokenizer resource not found: {resource_id}")
            else:
                manifest = self.resource_catalog.get_manifest(str(resource_id))
                if manifest is not None and manifest.category != "tokenizer":
                    missing_resources.append(
                        f"Tokenizer resource {resource_id} has incompatible category {manifest.category!r}"
                    )
                elif self.resource_catalog.get_resource_path(str(resource_id)) is None:
                    missing_resources.append(
                        f"Tokenizer resource digest verification failed: {resource_id}"
                    )
                else:
                    from .tokenizer_resource import TokenizerResourceError, load_tokenizer_counter

                    try:
                        load_tokenizer_counter(
                            self.resource_catalog.get_resource_path(str(resource_id))  # type: ignore[arg-type]
                        )
                    except TokenizerResourceError as exc:
                        missing_resources.append(f"Tokenizer resource {resource_id}: {exc}")

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

        # Profile-specific validation. Danbooru resources are not bundled, so the
        # profile is selectable but every stage that needs a Danbooru resource
        # must fail closed instead of silently reusing an e621 one.
        if config.profile == "danbooru":
            warnings.append("Danbooru profile requires official resources (not bundled)")
            if config.replace.get("enabled") and not config.replace.get("resource_id"):
                errors.append(
                    "Danbooru profile: Replace is enabled but no Danbooru replacement"
                    " index is selected; the e621 index is not a valid substitute"
                )

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


