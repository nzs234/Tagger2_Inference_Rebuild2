"""Workflow API routes."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ..security import PathAllowlist, PathNotAllowedError
from .contracts import WorkflowJobConfigV2, utc_now
from .db import WorkflowDatabase, default_workflow_database_path
from .policy_config_parser import parse_policy_config
from .preflight import WorkflowPreflightError, WorkflowPreflightService
from .resources import WorkflowResourceCatalog


def _build_public_error_codes() -> dict[type, str]:
    """Stable client-facing codes for the failures a run can raise."""

    from .commit import CommitError
    from .pipeline import PipelineError
    from .stages.policy import PolicyError
    from .stages.replacement import ReplacementError
    from .stages.token_budget import TokenBudgetError

    return {
        CommitError: "commit_failed",
        PipelineError: "pipeline_failed",
        PolicyError: "policy_failed",
        ReplacementError: "replacement_failed",
        TokenBudgetError: "token_budget_failed",
        PathNotAllowedError: "path_not_allowed",
        FileNotFoundError: "input_unavailable",
        PermissionError: "permission_denied",
        OSError: "io_error",
    }


_PUBLIC_ERROR_CODES: dict[type, str] | None = None


def _public_error_code(exc: BaseException) -> str:
    """Map an exception onto a stable public code via its MRO.

    Walking the MRO means a subclass of a mapped error still reports the
    specific code, and anything unmapped degrades to `internal_error` rather
    than exposing the exception text.
    """

    global _PUBLIC_ERROR_CODES
    if _PUBLIC_ERROR_CODES is None:
        _PUBLIC_ERROR_CODES = _build_public_error_codes()
    for klass in type(exc).__mro__:
        code = _PUBLIC_ERROR_CODES.get(klass)
        if code is not None:
            return code
    return "internal_error"


class WorkflowJobCreateRequest(BaseModel):
    """Request to create a workflow job."""
    config: dict[str, Any]


class WorkflowJobCreateResponse(BaseModel):
    """Response after creating a workflow job."""
    job_id: str
    status: str


class WorkflowJobStatusResponse(BaseModel):
    """Workflow job status response."""
    job_id: str
    status: str
    profile: str
    work_mode: str
    total_samples: int
    processed_samples: int
    succeeded_samples: int
    failed_samples: int
    skipped_samples: int
    current_module_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    # A stable public code, never an exception message or traceback. The full
    # diagnosis stays server-side in the job workspace.
    error_code: str | None


class WorkflowJobSummaryResponse(BaseModel):
    """One row of the job list.

    Deliberately narrower than the ``workflow_jobs`` table: `workspace_path`,
    `config_json` and `config_hash` are server-side details and must not cross
    the API boundary.
    """

    job_id: str
    status: str
    profile: str
    work_mode: str
    overwrite_mode: str
    source_root_id: str
    output_root_id: str | None
    current_module_id: str | None
    total_samples: int
    processed_samples: int
    succeeded_samples: int
    failed_samples: int
    skipped_samples: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    error_code: str | None
    pinned: bool = False


class WorkflowJobIssueResponse(BaseModel):
    """One recorded issue, addressed by relative path only."""

    issue_id: str
    sample_id: int | None
    relative_image_path: str | None
    module_id: str
    code: str
    message: str
    severity: str
    blocking: bool
    created_at: str | None


class WorkflowJobReportResponse(BaseModel):
    """Per-stage report for a finished job."""

    job_id: str
    available: bool
    report: dict[str, Any] | None = None


def _job_summary(job: dict[str, Any], *, pinned: bool = False) -> WorkflowJobSummaryResponse:
    """Project a `workflow_jobs` row onto the public summary.

    Whitelists fields explicitly, so a future column added to the table cannot
    silently start crossing the API boundary.
    """

    return WorkflowJobSummaryResponse(
        job_id=str(job["job_id"]),
        status=str(job["status"]),
        profile=str(job["profile"]),
        work_mode=str(job["work_mode"]),
        overwrite_mode=str(job["overwrite_mode"]),
        source_root_id=str(job["source_root_id"]),
        output_root_id=job["output_root_id"],
        current_module_id=job["current_module_id"],
        total_samples=int(job["total_samples"]),
        processed_samples=int(job["processed_samples"]),
        succeeded_samples=int(job["succeeded_samples"]),
        failed_samples=int(job["failed_samples"]),
        skipped_samples=int(job["skipped_samples"]),
        created_at=str(job["created_at"]),
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        error_code=job["error"],
        pinned=pinned,
    )


class WorkflowCountResolveRequest(BaseModel):
    """Record a reviewed count for one sample."""

    sample_id: int
    expected_version: int
    count: str
    source: str = "manual"


class WorkflowCountResolveBatchRequest(BaseModel):
    """Record several reviewed counts in one call."""

    items: list[WorkflowCountResolveRequest]


class WorkflowCountConfirmRequest(BaseModel):
    """Explicitly confirm that count review is complete."""

    confirmed: bool


class WorkflowPinRequest(BaseModel):
    """Pin/unpin a job for workspace retention."""

    pinned: bool = True


class WorkflowTokenReviewRequest(BaseModel):
    """Record one token budget review action for a sample."""

    sample_id: int
    action: Literal["edit", "recount", "rewrite_short", "apply"]
    expected_status: Literal["overflow", "edited", "recounted", "rewritten", "applied"]
    text: str | None = None


class WorkflowResourceImportRequest(BaseModel):
    """Request to preview or apply a resource import.

    The source file is addressed by root id + relative path so a client can never
    name an arbitrary absolute path on the server.
    """

    root_id: str
    relative_path: str
    resource_id: str
    category: str


class WorkflowResourceImportPreviewResponse(BaseModel):
    """Response for a resource import preview.

    ``rule_count`` is the category-neutral "usable rows" count: executable rules
    for a replacement index, tags for a classification snapshot. The snapshot
    specific counters stay optional so a replacement preview is unchanged.
    """

    valid: bool
    errors: list[str]
    warnings: list[str]
    rule_count: int
    action_counts: dict[str, int]
    passthrough_count: int
    fingerprint: str | None
    profile: str | None = None
    tag_count: int | None = None
    alias_count: int | None = None
    implication_count: int | None = None
    category_counts: dict[str, int] | None = None


def _preflight_http_error(error: WorkflowPreflightError) -> HTTPException:
    """Translate a preflight failure into a client error.

    The reasons are folded into the message and ``fields`` because the
    application error envelope only forwards code, message and fields.
    """

    errors = [str(item) for item in error.details.get("errors", [])]
    message = error.message if not errors else f"{error.message}: " + "; ".join(errors)
    return HTTPException(
        status_code=400,
        detail={
            "code": error.code,
            "message": message,
            "fields": {"config": errors} if errors else None,
        },
    )


def create_workflow_router(
    allowlist: PathAllowlist,
    resource_catalog: WorkflowResourceCatalog,
    database: WorkflowDatabase | None = None,
    token_counter: Callable[[Sequence[str]], Sequence[int]] | None = None,
    model_registry: Any | None = None,
    inference_engine: Any | None = None,
    storage: Any | None = None,
) -> APIRouter:
    """Create workflow API router.

    ``token_counter`` stays optional because the tokenizer resource is not
    bundled. Without it the token review actions that need a count fail closed
    with ``token_review_unavailable`` instead of guessing a length.
    """
    
    router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

    if database is None:
        database = WorkflowDatabase(default_workflow_database_path())

    # The workflow executor is currently process-local.  If the host was
    # restarted, queued/running rows no longer have a worker behind them.  Make
    # that fact durable before serving routes so operators can recover them
    # instead of observing jobs stuck forever in ``running``.
    database.mark_interrupted_jobs()

    preflight_service = WorkflowPreflightService(allowlist, resource_catalog, database)

    def _record_job_failure(job_id: str, exc: BaseException, trace: str) -> None:
        """Fail a job without leaking internals to the client.

        The job row keeps a short, stable code so the UI can branch on it. The
        exception text and traceback go to `<workspace>/job_error.log`, which an
        operator can read but the API never returns.
        """

        from .lifecycle import JobLifecycle

        code = _public_error_code(exc)
        try:
            job = database.get_job(job_id)
            if job is not None:
                log_path = Path(job["workspace_path"]) / "job_error.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    f"{utc_now()} {type(exc).__name__}: {exc}\n\n{trace}",
                    encoding="utf-8",
                )
        except OSError:
            # Losing the diagnostic file must not mask the original failure or
            # leave the job stuck in `running`.
            pass

        try:
            JobLifecycle(database, job_id).transition("failed")
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            database.update_job_status(job_id, status="failed", error=code)
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            with database.connection() as conn:
                conn.execute(
                    "UPDATE workflow_stage_runs SET status = 'failed', finished_at = ? "
                    "WHERE job_id = ? AND status IN ('pending', 'running')",
                    (utc_now(), job_id),
                )
        except Exception:  # noqa: BLE001, S110
            pass

    async def _execute_job_async(job_id: str) -> None:
        """Execute a workflow job in the background, updating status and seeding reviews."""
        import asyncio
        import traceback

        from .count_review import CountReviewStore
        from .lifecycle import JobLifecycle
        from .pipeline import run_offline_pipeline
        from .token_budget_review import TokenBudgetReviewStore
    
        try:
            job = database.get_job(job_id)
            if job is None:
                return
            initial_status = str(job["status"])
            if initial_status == "cancelling":
                lifecycle = JobLifecycle(database, job_id)
                lifecycle.transition("cancelled")
                return
            if initial_status == "pausing":
                lifecycle = JobLifecycle(database, job_id)
                lifecycle.transition("paused")
                return
            if initial_status in {"cancelled", "paused", "interrupted"}:
                return
    
            lifecycle = JobLifecycle(database, job_id)
            # A queued job is the normal path.  Keep a compatibility fallback
            # for jobs created against the old pending->running lifecycle.
            try:
                lifecycle.transition("running")
            except Exception:
                if str(job["status"]) != "running":
                    raise
    
            config = WorkflowJobConfigV2.from_payload(json.loads(job["config_json"]))
            workspace = Path(job["workspace_path"])
    
            # Resolve physical paths from root references
            source_path = None
            output_path = None
            try:
                source_ref = allowlist.resolve(
                    config.source_root.root_id, config.source_root.relative_path
                )
                source_path = Path(source_ref)
                if config.output_root:
                    output_ref = allowlist.resolve(
                        config.output_root.root_id, config.output_root.relative_path
                    )
                    output_path = Path(output_ref)
                elif config.work_mode == "in_place":
                    output_path = source_path
                else:
                    raise ValueError("full_copy requires output_root")
            except PathNotAllowedError as exc:
                _record_job_failure(job_id, exc, str(exc))
                return
    
            # Freeze content-addressed resources before stage execution.  The
            # digest is persisted with the job so recovery can detect drift.
            resource_fingerprints: dict[str, str] = {}

            frozen_manifests: dict[str, dict[str, Any]] = {}

            def freeze_resource(resource_id: str) -> None:
                manifest = resource_catalog.get_manifest(resource_id)
                if manifest is None or resource_catalog.get_resource_path(resource_id) is None:
                    raise ValueError(f"resource digest verification failed: {resource_id}")
                resource_fingerprints[resource_id] = manifest.resource_fingerprint
                frozen_manifests[resource_id] = dict(manifest.__dict__)

            for section_name, resource_key in (
                ("classify", "resource_id"),
                ("replace", "resource_id"),
                ("ocr", "resource_id"),
                ("token_budget", "tokenizer_resource_id"),
            ):
                section = getattr(config, section_name)
                if section.get("enabled"):
                    resource_id = str(section.get(resource_key, ""))
                    if resource_id:
                        freeze_resource(resource_id)

            # Wire up resources from catalog
            replacement_index_path = None
            if config.replace.get("enabled"):
                # Use the replacement index specified in the job config
                replace_resource_id = str(config.replace.get("resource_id", ""))
                if not replace_resource_id:
                    raise ValueError("replace stage is enabled but no resource_id was configured")
                replacement_index_path = resource_catalog.get_resource_path(replace_resource_id)
                if replacement_index_path is None:
                    raise ValueError(
                        f"replace stage is enabled but the replacement index is not registered: {replace_resource_id}"
                    )
            
            # Wire up tag predictor from inference engine
            tag_predictor = None
            if config.caption.get("enabled") and inference_engine is not None and model_registry is not None:
                from .stages.caption import EngineTagPredictor
                model_id = str(config.caption.get("model_id", ""))
                if model_id and model_registry.get_model(model_id) is not None:
                    threshold_mode = str(config.caption.get("threshold_mode", "model_default"))
                    tag_predictor = EngineTagPredictor(
                        engine=inference_engine,
                        model_id=model_id,
                        threshold=None if threshold_mode == "model_default" else float(config.caption.get("threshold", 0.35)),
                        category_thresholds=config.caption.get("category_thresholds"),
                        use_category_thresholds=bool(config.caption.get("use_category_thresholds", True)),
                    )

            # Wire up classification rules from the registered snapshot. A
            # missing or unreadable snapshot fails the job instead of letting
            # Classify silently produce nothing.
            classification_rules = None
            if config.classify.get("enabled"):
                from .classify_snapshot import (
                    ClassifySnapshotError,
                    load_classification_rules,
                )

                classify_resource_id = str(config.classify.get("resource_id", ""))
                if not classify_resource_id:
                    raise ValueError("classify stage is enabled but no resource_id was configured")
                classify_path = resource_catalog.get_resource_path(classify_resource_id)
                if classify_path is None:
                    raise ValueError(
                        "classify stage is enabled but the classification snapshot"
                        f" is not registered: {classify_resource_id}"
                    )
                try:
                    classification_rules = load_classification_rules(classify_path)
                except ClassifySnapshotError as exc:
                    raise ValueError(f"failed to load classification rules: {exc}") from exc
                if classification_rules.profile != config.profile:
                    raise ValueError(
                        "classification snapshot profile"
                        f" {classification_rules.profile!r} does not match the job profile"
                        f" {config.profile!r}"
                    )

            # OCR runs in an isolated runtime. Building the engine is what
            # detects a missing runtime, and the stage turns that into a
            # non-blocking warning rather than failing the job.
            ocr_engine = None
            if config.ocr.get("enabled"):
                from .ocr import PaddleOCREngine

                try:
                    ocr_engine = await asyncio.to_thread(PaddleOCREngine)
                except RuntimeError:
                    ocr_engine = None

            # NL client if provider configured
            nl_client = None
            if config.nl.get("enabled"):
                provider_id = str(config.nl.get("provider_id", ""))
                if provider_id:
                    from tagger2.providers import ProviderConfig, create_provider

                    from .nl_adapter import ProviderNlAdapter
                    
                    assert storage is not None  # Type narrowing
                    # Get provider profile from storage
                    stored_profile = storage.get_provider_profile(provider_id)
                    if stored_profile is None:
                        raise ValueError(f"Provider {provider_id} not found")
                    if not bool(stored_profile.get("enabled", True)):
                        raise ValueError(f"Provider {provider_id} is disabled")
                    
                    # Build provider config
                    cfg = dict(stored_profile.get("config") or {})
                    cfg.update({
                        "id": provider_id,
                        "name": stored_profile.get("name"),
                        "kind": stored_profile.get("kind"),
                        "base_url": stored_profile.get("base_url")
                    })
                    cfg["model"] = cfg.pop("primary_model", cfg.get("model", ""))
                    cfg["backup_model"] = cfg.pop("fallback_model", cfg.get("backup_model"))
                    cfg["max_output_tokens"] = cfg.pop("max_tokens", cfg.get("max_output_tokens", 8192))
                    
                    # Get API keys from secret store
                    secret_ref = stored_profile.get("secret_ref")
                    keys = []
                    if secret_ref:
                        try:
                            from tagger2.secrets import CompositeSecretStore
                            secret_store = CompositeSecretStore()
                            raw_keys = secret_store.get(secret_ref)
                            if raw_keys:
                                keys = [k.strip() for k in raw_keys.replace(",", "\n").split("\n") if k.strip()]
                        except Exception:  # noqa: BLE001, S110
                            pass
                    cfg["api_keys"] = tuple(keys)
                    
                    # Create provider instance
                    provider = create_provider(ProviderConfig.from_mapping(cfg))
                    nl_client = ProviderNlAdapter(
                        provider,
                        model=(str(config.nl.get("model")) if config.nl.get("model") else None),
                    )

            # Policy config converted to dataclass if enabled
            policy_config_arg = None
            if config.policy.get("enabled"):
                try:
                    policy_config_arg = parse_policy_config(config.policy)
                except Exception as exc:
                    raise ValueError(f"invalid policy configuration: {exc}") from exc

            # Token counter passed through if available
            token_counter_arg = token_counter if config.token_budget.get("enabled") else None

            report = await asyncio.to_thread(
                run_offline_pipeline,
                config,
                source_root=source_path,
                output_root=output_path,
                workspace=workspace,
                replacement_index_path=replacement_index_path,
                resource_fingerprints=resource_fingerprints,
                resource_manifests=frozen_manifests,
                tag_predictor=tag_predictor,
                classification_rules=classification_rules,
                policy_config=policy_config_arg,
                token_counter=token_counter_arg,
                ocr_engine=ocr_engine,
                nl_client=nl_client,
                database=database,
                job_id=job_id,
            )
    
            # Persist the stage report so the UI can read per-stage counters
            # (OCR included) without the pipeline holding process state.
            try:
                (workspace / "pipeline_report.json").write_text(
                    json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
            except OSError:
                # A missing report must not fail an otherwise successful run.
                pass

            # Check if human review is needed before marking as completed
            latest = database.get_job(job_id)
            if latest is None:
                return
            latest_status = str(latest["status"])
            if latest_status == "cancelling":
                lifecycle.transition("cancelled")
                return
            if latest_status == "pausing":
                lifecycle.transition("paused")
                return
            if latest_status in {"cancelled", "paused", "interrupted"}:
                return
            count_store = CountReviewStore(database, job_id)
            token_store_check = TokenBudgetReviewStore(database, job_id)
            
            pending_count = count_store.pending_count()
            pending_token = token_store_check.unresolved_count()
            
            has_blocking = any(
                issue.blocking for issue in report.issues
            )
            
            # Determine final status based on blocking issues and pending reviews
            if has_blocking:
                final_status = "failed"
            elif bool(config.count_review.get("enabled")) and pending_count > 0:
                final_status = "waiting_count_review"
            elif bool(config.token_budget.get("enabled")) and pending_token > 0:
                final_status = "waiting_token_review"
            else:
                final_status = "completed"
            
            lifecycle.transition(final_status)
    
            # Issues are already persisted in the issues table via report

        except Exception as exc:  # noqa: BLE001
            # The traceback is operator data, not client data: write it to the
            # job workspace and store only a stable code on the job row.
            _record_job_failure(job_id, exc, traceback.format_exc())

    async def _schedule_job(job_id: str, background_tasks: BackgroundTasks) -> str:
        """Queue one execution, preserving idempotency for UI retries."""

        from .lifecycle import LifecycleError

        lifecycle, job = _lifecycle(job_id)
        status = str(job["status"])
        if status in {"completed", "cancelled", "failed", "running", "queued"}:
            if status == "queued":
                background_tasks.add_task(_execute_job_async, job_id)
            return status
        try:
            queued = lifecycle.transition("queued")
        except LifecycleError:
            # Compatibility with a database created by the pre-queued schema;
            # the current lifecycle implementation accepts ``queued``.
            queued = lifecycle.transition("running")
        background_tasks.add_task(_execute_job_async, job_id)
        return queued

    def _review_gate(job_id: str, section: str, waiting_status: str) -> bool:
        """Validate the immutable review gate before a confirm transition.

        Review rows can exist in old databases (and in operator-created test
        fixtures) even when the corresponding stage is disabled.  Their
        presence must not turn a disabled stage into an implicit gate.  The
        status check is deliberately performed here, before reading the review
        rows, so a confirm request can never requeue a job from ``pending`` or
        a different checkpoint.
        """

        _lifecycle_obj, job = _lifecycle(job_id)
        try:
            payload = json.loads(str(job["config_json"]))
        except (TypeError, json.JSONDecodeError):
            payload = None
        configured = payload.get(section) if isinstance(payload, dict) else None
        # A V1 job predates the explicit review sections.  Keep that read-only
        # compatibility path for old operator fixtures, but every V2 snapshot
        # must carry an explicit ``enabled`` flag and the waiting checkpoint.
        # Config rows created before the V2 contract do not have a review
        # section.  Keep their read-only review endpoints usable for migration
        # tooling, while treating a V2 row with a missing section as disabled.
        legacy = configured is None and int(job.get("config_version") or 1) < 2
        enabled = legacy or (
            isinstance(configured, dict) and configured.get("enabled") is True
        )
        if not enabled:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "review_disabled",
                    "message": f"{section} review is disabled for this job",
                    "fields": {"stage": section},
                },
            )

        status = str(job["status"])
        if not legacy and status != waiting_status:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "review_not_waiting",
                    "message": f"{section} review can only be confirmed while the job is waiting",
                    "fields": {"stage": section, "expected_status": waiting_status},
                },
            )
        return not legacy

    async def _requeue_after_review(
        job_id: str,
        background_tasks: BackgroundTasks,
        *,
        expected_status: str | None,
    ) -> str:
        """CAS a waiting review checkpoint back into the execution queue.

        The caller has already validated the review rows.  Re-reading the job
        and requiring the exact waiting status here closes the race where a
        worker (or a second reviewer) changes the job between that validation
        and the lifecycle transition.
        """

        from .lifecycle import LifecycleError

        lifecycle, job = _lifecycle(job_id)
        status = str(job["status"])
        if expected_status is not None and status != expected_status:
            raise LifecycleError(
                f"review checkpoint changed from {expected_status!r} to {status!r}"
            )
        # JobLifecycle.transition uses update_job_status(... expected_status=...)
        # so this is a compare-and-set, not a read-then-write transition.
        queued = lifecycle.transition("queued")
        background_tasks.add_task(_execute_job_async, job_id)
        return queued
    


    @router.get("/capabilities")
    async def get_capabilities() -> dict[str, Any]:
        """Get workflow capabilities and available resources."""
        resources = resource_catalog.list_resources()
        return {
            "profiles": ["e621", "danbooru"],
            "work_modes": ["in_place", "full_copy"],
            "resources": [
                {
                    "resource_id": r.resource_id,
                    "category": r.category,
                    "fingerprint": r.resource_fingerprint,
                }
                for r in resources
            ],
        }

    @router.get("/resources")
    async def list_resources(category: str | None = None) -> list[dict[str, Any]]:
        """List available resources."""
        resources = resource_catalog.list_resources(category=category)
        return [
            {
                "resource_id": r.resource_id,
                "category": r.category,
                "fingerprint": r.resource_fingerprint,
                "source_url": r.source_url,
                "created_at": r.created_at,
            }
            for r in resources
        ]

    def _resolve_resource_source(request: WorkflowResourceImportRequest) -> Path:
        try:
            return allowlist.resolve(
                request.root_id,
                request.relative_path,
                must_exist=True,
                expect="file",
            )
        except PathNotAllowedError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "path_not_allowed", "message": str(exc)},
            ) from exc

    @router.post("/resources/import/preview")
    async def preview_resource_import(
        request: WorkflowResourceImportRequest,
    ) -> WorkflowResourceImportPreviewResponse:
        """Validate a resource file without importing it."""
        source_path = _resolve_resource_source(request)
        report = resource_catalog.validate_resource(source_path, request.category)

        warnings: list[str] = []
        if report.get("truncated"):
            warnings.append("error list truncated; fix the reported rows and re-run preview")
        if resource_catalog.get_manifest(request.resource_id) is not None:
            warnings.append(f"resource id already registered: {request.resource_id}")

        return WorkflowResourceImportPreviewResponse(
            valid=report["valid"],
            errors=report["errors"],
            warnings=warnings,
            rule_count=report["line_count"],
            action_counts=report.get("action_counts", {}),
            passthrough_count=report.get("passthrough_count", 0),
            fingerprint=(
                resource_catalog.fingerprint_file(source_path) if report["valid"] else None
            ),
            profile=report.get("profile") or None,
            tag_count=report.get("tag_count"),
            alias_count=report.get("alias_count"),
            implication_count=report.get("implication_count"),
            category_counts=report.get("category_counts"),
        )

    @router.post("/resources/import/apply")
    async def apply_resource_import(
        request: WorkflowResourceImportRequest,
    ) -> dict[str, Any]:
        """Import and register a resource after re-validating it."""
        source_path = _resolve_resource_source(request)

        report = resource_catalog.validate_resource(source_path, request.category)
        if not report["valid"]:
            raise HTTPException(
                status_code=400,
                detail={"code": "validation_failed", "errors": report["errors"]},
            )

        manifest = resource_catalog.import_resource(
            source_path=source_path,
            resource_id=request.resource_id,
            category=request.category,
        )

        return {
            "resource_id": manifest.resource_id,
            "fingerprint": manifest.resource_fingerprint,
            "category": manifest.category,
            "created_at": manifest.created_at,
            "rule_count": report["line_count"],
        }

    def _lifecycle(job_id: str):
        from .lifecycle import JobLifecycle

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        return JobLifecycle(database, job_id), job

    @router.post("/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> dict[str, Any]:
        """Pause a running job so it can be resumed later."""
        from .lifecycle import LifecycleError

        lifecycle, _job = _lifecycle(job_id)
        try:
            return {"job_id": job_id, "status": lifecycle.request_pause()}
        except LifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_transition", "message": str(exc)},
            ) from exc

    @router.post("/jobs/{job_id}/resume")
    async def resume_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Resume a paused job."""
        from .lifecycle import LifecycleError

        lifecycle, job = _lifecycle(job_id)
        if str(job["status"]) == "pending":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "invalid_transition",
                    "message": "pending jobs must be explicitly started",
                },
            )
        try:
            status = lifecycle.resume()
            background_tasks.add_task(_execute_job_async, job_id)
            return {"job_id": job_id, "status": status}
        except LifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_transition", "message": str(exc)},
            ) from exc

    @router.post("/jobs/{job_id}/start")
    async def start_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Acquire the dataset lock and explicitly queue a pending job."""
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail={"code": "job_not_found", "message": "job not found"})
        if str(job["status"]) != "pending":
            raise HTTPException(status_code=409, detail={"code": "invalid_state_for_start", "message": f"job is already {job['status']}"})
        if not database.start_job(job_id, expected_status="pending"):
            raise HTTPException(status_code=409, detail={"code": "dataset_locked", "message": "dataset scope is already active"})
        background_tasks.add_task(_execute_job_async, job_id)
        return {"job_id": job_id, "status": "queued"}

    
    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a job permanently. Cannot be resumed once cancelled."""
        from .lifecycle import LifecycleError

        lifecycle, _job = _lifecycle(job_id)
        try:
            status = str(_job["status"])
            if status in {"queued", "running", "pausing"}:
                target = lifecycle.request_cancel()
            elif status in {"pending", "paused", "waiting_count_review", "waiting_token_review"}:
                target = lifecycle.transition("cancelled")
            else:
                target = lifecycle.transition("cancelled")
            return {"job_id": job_id, "status": target}
        except LifecycleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @router.post("/jobs/{job_id}/repair")
    async def repair_job(job_id: str) -> dict[str, Any]:
        """Repair an interrupted run and report what recovery found."""
        lifecycle, job = _lifecycle(job_id)
        report = lifecycle.repair(Path(str(job["workspace_path"])))
        return {
            "job_id": job_id,
            **report.as_dict(),
            "resumable_samples": len(lifecycle.resumable_samples()),
        }

    @router.post("/jobs/{job_id}/recover")
    async def recover_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Repair expired sample leases and queue an interrupted job."""

        lifecycle, job = _lifecycle(job_id)
        status = str(job["status"])
        if status not in {"interrupted", "failed", "paused"}:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_state_for_recover", "message": f"Cannot recover {status}"},
            )
        lifecycle.repair(Path(str(job["workspace_path"])))
        try:
            queued = lifecycle.transition("queued")
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_transition", "message": str(exc)},
            ) from exc
        background_tasks.add_task(_execute_job_async, job_id)
        return {"job_id": job_id, "status": queued}

    @router.post("/jobs/{job_id}/pin")
    async def pin_job(job_id: str, request: WorkflowPinRequest) -> dict[str, Any]:
        """Pin or unpin a job workspace for retention."""

        if not database.set_job_pinned(job_id, request.pinned):
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        return {"job_id": job_id, "pinned": request.pinned}


    @router.post("/jobs/{job_id}/restore")
    async def restore_job(job_id: str) -> dict[str, Any]:
        """Restore original annotations from backup archive.
        
        This operation restores the dataset to its pre-workflow state using
        the backup created during job initialization. The job must be in a
        terminal state (completed or failed) or explicitly cancelled.
        """
        from .commit import CommitError, restore_annotation_backup
        
        _lifecycle_obj, job = _lifecycle(job_id)
        status = str(job["status"])
        
        # Only allow restore from terminal or cancelled states
        if status not in ("completed", "failed", "cancelled", "interrupted", "rollback_required"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_state_for_restore",
                    "message": f"Cannot restore from state: {status}"
                }
            )

        if str(job["work_mode"]) != "in_place":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "restore_not_applicable",
                    "message": "restore is only available for in_place jobs",
                },
            )

        workspace = Path(str(job["workspace_path"]))
        backup_zip = workspace / "backup" / "annotations.zip"
        if not backup_zip.exists():
            # Compatibility with archives created by the first workflow
            # vertical.  New commits always use the nested artifact path.
            legacy = workspace / "backup.zip"
            backup_zip = legacy if legacy.exists() else backup_zip
        
        if not backup_zip.exists():
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "backup_not_found",
                    "message": "Backup archive not found for this job"
                }
            )
        
        # Restore is intentionally limited to in_place jobs.  A full-copy
        # result has an independent output dataset and is never allowed to
        # mutate the source during restore.
        root_id = str(job["source_root_id"])
        if not root_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "missing_dataset_root",
                    "message": "Job has no dataset root to restore into",
                },
            )

        try:
            payload = json.loads(str(job["config_json"]))
            source = payload.get("source_root", {}) if isinstance(payload, dict) else {}
            relative_path = str(source.get("relative_path", "")) if isinstance(source, dict) else ""
            dataset_root = Path(allowlist.resolve(root_id, relative_path, must_exist=True, expect="dir"))
        except PathNotAllowedError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_dataset_root",
                    "message": "Dataset root is no longer registered",
                },
            ) from exc

        restore_started = False
        restore_operation_key = f"restore:{job_id}:{backup_zip.name}"

        def _mark_restore_failed() -> None:
            """Leave a failed restore retryable without retaining its lock."""

            try:
                database.update_job_status(
                    job_id,
                    "rollback_required",
                    error="restore_failed",
                    expected_status="restoring",
                )
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                database.record_operation(
                    job_id,
                    "restore",
                    idempotency_key=restore_operation_key,
                    status="failed",
                    payload={"code": "restore_failed"},
                )
                database.record_event(job_id, "restore_failed", payload={"code": "restore_failed"})
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                database.release_dataset_locks(job_id)
            except Exception:  # noqa: BLE001, S110
                pass
        
        try:
            # Serialize Restore against new starts. The terminal job released
            # its execution lock, so reacquire the exact source scope at the
            # operation boundary before touching dataset files.
            if not database.start_job(job_id, expected_status=status):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "restore_locked", "message": "dataset is busy"},
                )
            restore_started = True
            if not database.update_job_status(job_id, "restoring", expected_status="queued"):
                database.release_dataset_locks(job_id)
                raise HTTPException(
                    status_code=409,
                    detail={"code": "restore_state_race", "message": "job changed during restore"},
                )
            restored_count = restore_annotation_backup(backup_zip, dataset_root)
            database.record_operation(
                job_id,
                "restore",
                idempotency_key=restore_operation_key,
                payload={"restored_files": restored_count},
            )
            database.record_event(job_id, "restore_completed", payload={"restored_files": restored_count})
            if not database.update_job_status(job_id, "completed", expected_status="restoring"):
                raise CommitError("restore state changed before completion")
            # ``completed`` normally releases these rows in the database CAS;
            # keep this explicit so the contract survives older DB adapters.
            database.release_dataset_locks(job_id)
            return {
                "job_id": job_id,
                "root_id": root_id,
                "restored_files": restored_count,
            }
        except CommitError as exc:
            if restore_started:
                _mark_restore_failed()
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "restore_failed",
                    "message": "restore failed; recovery is required",
                }
            ) from exc
        except HTTPException:
            # Only release a lock acquired by this request.  If start_job lost
            # the CAS race, another restore/recovery may own the job's lock;
            # releasing it here would silently un-serialize that operation.
            if restore_started:
                database.release_dataset_locks(job_id)
            raise
        except Exception as exc:  # noqa: BLE001
            if restore_started:
                _mark_restore_failed()
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "restore_failed",
                    "message": "restore failed; recovery is required",
                },
            ) from exc

    @router.post("/jobs/{job_id}/discard")
    async def discard_job(job_id: str) -> dict[str, Any]:
        """Discard a job's workspace and intermediate files.
        
        This permanently removes the job's workspace directory including all
        intermediate files, staged outputs, and backups. The job record remains
        in the database for audit purposes but cannot be restored or resumed.
        
        The job must be in a terminal state (completed, failed, cancelled).
        """
        import shutil
        
        _lifecycle_obj, job = _lifecycle(job_id)
        status = str(job["status"])

        if database.is_job_pinned(job_id):
            raise HTTPException(
                status_code=409,
                detail={"code": "job_pinned", "message": "unpin the job before discarding its workspace"},
            )
        
        # Only allow discard from terminal states
        if status not in ("completed", "failed", "cancelled", "interrupted", "rollback_required"):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_state_for_discard",
                    "message": f"Cannot discard job in state: {status}"
                }
            )
        
        workspace = Path(str(job["workspace_path"]))
        
        if not workspace.exists():
            # Already discarded or never created
            return {
                "job_id": job_id,
                "discarded": False,
                "message": "Workspace already removed"
            }
        
        try:
            # Remove the entire workspace directory
            shutil.rmtree(workspace)
            database.record_operation(
                job_id,
                "discard",
                idempotency_key=f"discard:{job_id}",
                payload={"workspace": workspace.name},
            )
            database.record_event(job_id, "workspace_discarded")
            
            # Update job record to mark as discarded
            # Note: This is a simple implementation. Full version would update
            # a 'discarded_at' timestamp in the database
            
            return {
                "job_id": job_id,
                "discarded": True,
                "removed_path": str(workspace.name),  # Only return relative name
            }
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "discard_failed",
                    "message": f"Failed to remove workspace: {exc}"
                }
            ) from exc
    def _count_store(job_id: str):
        from .count_review import CountReviewStore

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        return CountReviewStore(database, job_id)

    @router.get("/jobs/{job_id}/count-review")
    async def list_count_review(
        job_id: str,
        limit: int = 50,
        offset: int = 0,
        pending_only: bool = False,
    ) -> dict[str, Any]:
        """Page through count decisions with the evidence a reviewer needs."""
        store = _count_store(job_id)
        return {
            "items": store.page(limit=limit, offset=offset, pending_only=pending_only),
            "pending": store.pending_count(),
        }

    @router.post("/jobs/{job_id}/count-review/resolve")
    async def resolve_count_review(
        job_id: str,
        request: WorkflowCountResolveRequest,
    ) -> dict[str, Any]:
        """Record one reviewed count, rejecting a stale version."""
        from .count_review import CountReviewConflictError, CountReviewError

        store = _count_store(job_id)
        try:
            return store.resolve(
                request.sample_id,
                expected_version=request.expected_version,
                count=request.count,
                source=request.source,
            )
        except CountReviewConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "count_review_conflict", "message": str(exc)},
            ) from exc
        except CountReviewError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "count_review_invalid", "message": str(exc)},
            ) from exc

    @router.post("/jobs/{job_id}/count-review/resolve-batch")
    async def resolve_count_review_batch(
        job_id: str,
        request: WorkflowCountResolveBatchRequest,
    ) -> dict[str, Any]:
        """Record several reviewed counts, stopping at the first rejection."""
        from .count_review import CountReviewConflictError, CountReviewError

        store = _count_store(job_id)
        applied: list[dict[str, Any]] = []
        for item in request.items:
            try:
                applied.append(
                    store.resolve(
                        item.sample_id,
                        expected_version=item.expected_version,
                        count=item.count,
                        source=item.source,
                    )
                )
            except CountReviewConflictError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "count_review_conflict",
                        "message": str(exc),
                        "fields": {"applied": [entry["sample_id"] for entry in applied]},
                    },
                ) from exc
            except CountReviewError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "count_review_invalid", "message": str(exc)},
                ) from exc
        return {"applied": applied, "pending": store.pending_count()}

    @router.post("/jobs/{job_id}/count-review/confirm")
    async def confirm_count_review(
        job_id: str,
        request: WorkflowCountConfirmRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Gate export on an explicit confirmation with nothing left pending."""
        from .count_review import CountReviewError
        from .lifecycle import LifecycleError

        if not request.confirmed:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "count_review_not_confirmed",
                    "message": "explicit count review confirmation is required",
                },
            )
        strict_gate = _review_gate(job_id, "count_review", "waiting_count_review")
        store = _count_store(job_id)
        try:
            store.assert_ready_for_export()
        except CountReviewError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "count_review_incomplete", "message": str(exc)},
            ) from exc
        if not strict_gate:
            # V1/operator fixtures may have review rows without a persisted
            # review section.  They are read-only compatibility records; do
            # not enqueue a pending job without the authoritative start lock.
            current = database.get_job(job_id)
            return {
                "job_id": job_id,
                "confirmed": True,
                "pending": 0,
                "status": str(current["status"]) if current else "pending",
            }
        try:
            status = await _requeue_after_review(
                job_id,
                background_tasks,
                expected_status="waiting_count_review",
            )
        except LifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "review_state_conflict", "message": str(exc)},
            ) from exc
        database.record_event(job_id, "count_review_confirmed", payload={"pending": 0})
        return {"job_id": job_id, "confirmed": True, "pending": 0, "status": status}


    def _token_store(job_id: str):
        from .token_budget_review import TokenBudgetReviewStore

        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        return TokenBudgetReviewStore(database, job_id)

    @router.get("/jobs/{job_id}/token-review")
    async def list_token_review(
        job_id: str,
        limit: int = 50,
        offset: int = 0,
        unresolved_only: bool = False,
    ) -> dict[str, Any]:
        """Page through captions that overflow the token budget."""
        store = _token_store(job_id)
        return {
            "items": store.page(limit=limit, offset=offset, unresolved_only=unresolved_only),
            "unresolved": store.unresolved_count(),
        }

    @router.post("/jobs/{job_id}/token-review/review")
    async def review_token_budget(
        job_id: str,
        request: WorkflowTokenReviewRequest,
    ) -> dict[str, Any]:
        """Record one review action, rejecting a stale status."""
        from .token_budget_review import (
            TokenBudgetReviewConflictError,
            TokenBudgetReviewError,
        )

        store = _token_store(job_id)
        if token_counter is None and request.action != "apply":
            # No tokenizer resource, so report unavailable rather than guess.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "token_review_unavailable",
                    "message": "no tokenizer resource is registered, so captions cannot be counted",
                },
            )
        try:
            return store.review(
                request.sample_id,
                action=request.action,
                expected_status=request.expected_status,
                text=request.text,
                count_tokens=token_counter,
            )
        except TokenBudgetReviewConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "token_review_conflict", "message": str(exc)},
            ) from exc
        except TokenBudgetReviewError as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "token_review_invalid", "message": str(exc)},
            ) from exc

    @router.post("/jobs/{job_id}/token-review/confirm")
    async def confirm_token_review(
        job_id: str,
        request: WorkflowCountConfirmRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        """Gate export on every overflow having been applied."""
        from .lifecycle import LifecycleError
        from .token_budget_review import TokenBudgetReviewError

        if not request.confirmed:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "token_review_not_confirmed",
                    "message": "explicit token budget review confirmation is required",
                },
            )
        strict_gate = _review_gate(job_id, "token_budget", "waiting_token_review")
        store = _token_store(job_id)
        try:
            store.assert_ready_for_export()
        except TokenBudgetReviewError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "token_review_incomplete", "message": str(exc)},
            ) from exc
        if not strict_gate:
            current = database.get_job(job_id)
            return {
                "job_id": job_id,
                "confirmed": True,
                "unresolved": 0,
                "status": str(current["status"]) if current else "pending",
            }
        try:
            status = await _requeue_after_review(
                job_id,
                background_tasks,
                expected_status="waiting_token_review",
            )
        except LifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "review_state_conflict", "message": str(exc)},
            ) from exc
        database.record_event(job_id, "token_review_confirmed", payload={"unresolved": 0})
        return {"job_id": job_id, "confirmed": True, "unresolved": 0, "status": status}
    @router.post("/jobs/preflight")
    async def preflight_job(config: dict[str, Any]) -> dict[str, Any]:
        """Validate job configuration before creation."""
        try:
            job_config = WorkflowJobConfigV2.from_payload(config)
            report = preflight_service.validate_config(job_config)
            return report
        except WorkflowPreflightError as exc:
            raise _preflight_http_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_config", "message": str(exc)},
            ) from exc

    @router.post("/jobs", response_model=WorkflowJobCreateResponse)
    async def create_job(request: WorkflowJobCreateRequest) -> WorkflowJobCreateResponse:
        """Create a draft workflow job; execution requires an explicit start."""
        try:
            job_config = WorkflowJobConfigV2.from_payload(request.config)
            
            # Run preflight validation
            preflight_service.validate_config(job_config)
            
            workspace_root = database.db_path.parent / "jobs"
            workspace_root.mkdir(parents=True, exist_ok=True)

            stored_config = job_config.to_dict()
            job_id, _workspace = database.create_job(
                config_json=stored_config,
                config_hash=job_config.config_hash(),
                profile=job_config.profile,
                work_mode=job_config.work_mode,
                overwrite_mode=job_config.overwrite_mode,
                source_root_id=job_config.source_root.root_id,
                output_root_id=job_config.output_root.root_id if job_config.output_root else None,
                workspace_root=workspace_root,
            )
            return WorkflowJobCreateResponse(job_id=job_id, status="pending")
        
        except WorkflowPreflightError as exc:
            raise _preflight_http_error(exc) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_config", "message": str(exc)},
            ) from exc

    @router.get("/jobs", response_model=list[WorkflowJobSummaryResponse])
    async def list_jobs(limit: int = 100, offset: int = 0) -> list[WorkflowJobSummaryResponse]:
        """List workflow jobs, projected onto the public summary shape."""
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        return [
            _job_summary(job, pinned=database.is_job_pinned(str(job["job_id"])))
            for job in database.list_jobs(limit=limit, offset=offset)
        ]

    @router.get("/jobs/{job_id}", response_model=WorkflowJobStatusResponse)
    async def get_job_status(job_id: str) -> WorkflowJobStatusResponse:
        """Get workflow job status."""
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return WorkflowJobStatusResponse(
            job_id=job["job_id"],
            status=job["status"],
            profile=job["profile"],
            work_mode=job["work_mode"],
            total_samples=job["total_samples"],
            processed_samples=job["processed_samples"],
            succeeded_samples=job["succeeded_samples"],
            failed_samples=job["failed_samples"],
            skipped_samples=job["skipped_samples"],
            current_module_id=job["current_module_id"],
            created_at=job["created_at"],
            started_at=job["started_at"],
            finished_at=job["finished_at"],
            error_code=job["error"],
        )

    @router.get("/jobs/{job_id}/report", response_model=WorkflowJobReportResponse)
    async def get_job_report(job_id: str) -> WorkflowJobReportResponse:
        """Return the persisted per-stage report for a finished job.

        Returns ``{"available": false}`` while a job has not written its report
        yet, so the client can render an empty state instead of treating a
        pending job as an error.
        """
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        report_path = Path(job["workspace_path"]) / "pipeline_report.json"
        if not report_path.is_file():
            return WorkflowJobReportResponse(job_id=job_id, available=False)
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "report_unreadable", "message": str(exc)},
            ) from exc
        if isinstance(payload, dict) and payload.get("backup_path"):
            # The report stores an absolute server path; the client only needs
            # to know that a backup exists.
            payload = dict(payload)
            payload["backup_path"] = None
            payload["backup_available"] = True
        return WorkflowJobReportResponse(job_id=job_id, available=True, report=payload)

    @router.get("/jobs/{job_id}/issues", response_model=list[WorkflowJobIssueResponse])
    async def list_job_issues(
        job_id: str, blocking_only: bool = False
    ) -> list[WorkflowJobIssueResponse]:
        """List issues for a workflow job, projected onto the public shape."""
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )

        return [
            WorkflowJobIssueResponse(
                issue_id=str(issue.get("issue_id", "")),
                sample_id=issue.get("sample_id"),
                relative_image_path=issue.get("relative_image_path"),
                module_id=str(issue.get("module_id", "")),
                code=str(issue.get("code", "")),
                message=str(issue.get("message", "")),
                severity=str(issue.get("severity", "error")),
                blocking=bool(issue.get("blocking", False)),
                created_at=issue.get("created_at"),
            )
            for issue in database.list_issues(job_id, blocking_only=blocking_only)
        ]

    @router.get("/jobs/{job_id}/events")
    async def list_job_events(
        job_id: str,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return a bounded replay page for the workflow control-plane.

        The cursor is deliberately an integer database sequence rather than a
        timestamp, so clients can reconnect without missing events that share
        the same clock tick.  This JSON endpoint is also the persistence layer
        used by a future SSE adapter.
        """

        if database.get_job(job_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        if after_event_id < 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_event_cursor", "message": "after_event_id must be non-negative"},
            )
        bounded_limit = max(1, min(int(limit), 500))
        events = database.list_events(
            job_id,
            after_event_id=after_event_id,
            limit=bounded_limit,
        )
        next_cursor = events[-1]["event_id"] if events else after_event_id
        return {
            "job_id": job_id,
            "events": events,
            "next_after_event_id": next_cursor,
            "has_more": len(events) >= bounded_limit,
        }

    @router.get("/jobs/{job_id}/events/stream")
    async def stream_job_events(job_id: str, after_event_id: int = 0):
        """Replay durable workflow events as an SSE stream.

        The stream is finite at the current cursor and can be reconnected with
        the last event id.  This keeps the API responsive without a long-lived
        database polling task; clients reconnect while a job is active.
        """

        from fastapi.responses import StreamingResponse

        if database.get_job(job_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        if after_event_id < 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_event_cursor", "message": "after_event_id must be non-negative"},
            )
        events = database.list_events(job_id, after_event_id=after_event_id, limit=500)

        async def body():
            for event in events:
                yield (
                    f"id: {event['event_id']}\n"
                    "event: workflow\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                )

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


__all__ = ["create_workflow_router"]


