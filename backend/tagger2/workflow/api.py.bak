"""Workflow API routes."""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Any, Callable, Literal, Sequence

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import json
from pathlib import Path

from ..security import PathAllowlist, PathNotAllowedError
from .contracts import WorkflowJobConfigV1, utc_now
from .db import WorkflowDatabase, default_workflow_database_path
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


def _job_summary(job: dict[str, Any]) -> WorkflowJobSummaryResponse:
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
) -> APIRouter:
    """Create workflow API router.

    ``token_counter`` stays optional because the tokenizer resource is not
    bundled. Without it the token review actions that need a count fail closed
    with ``token_review_unavailable`` instead of guessing a length.
    """
    
    router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

    if database is None:
        database = WorkflowDatabase(default_workflow_database_path())

    preflight_service = WorkflowPreflightService(allowlist, resource_catalog)

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
        except Exception:
            # Losing the diagnostic file must not mask the original failure or
            # leave the job stuck in `running`.
            pass

        try:
            JobLifecycle(database, job_id).transition("failed")
        except Exception:
            pass
        try:
            database.update_job_status(job_id, status="failed", error=code)
        except Exception:
            pass

    async def _execute_job_async(job_id: str) -> None:
        """Execute a workflow job in the background, updating status and seeding reviews."""
        from .lifecycle import JobLifecycle
        from .pipeline import run_offline_pipeline
        from .count_review import CountReviewStore
        from .token_budget_review import TokenBudgetReviewStore
        import asyncio
        import traceback
    
        try:
            job = database.get_job(job_id)
            if job is None:
                return
    
            lifecycle = JobLifecycle(database, job_id)
            lifecycle.transition("running")
    
            config = WorkflowJobConfigV1.from_payload(json.loads(job["config_json"]))
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
                lifecycle.transition("failed")
                database.update_job_status(job_id, status="failed", error=f"path not allowed: {exc}")
                return
    
            # Wire up resources from catalog
            replacement_index_path = None
            if config.replace.get("enabled"):
                # Use the designated e621 replacement index
                replacement_index_path = resource_catalog.get_resource_path("e621-replacement-index-v1")
                if replacement_index_path is None:
                    raise ValueError("replace stage enabled but e621-replacement-index-v1 not found in catalog")
            
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

            # Policy config passed through if enabled
            policy_config_arg = config.policy if config.policy.get("enabled") else None

            # Token counter passed through if available
            token_counter_arg = token_counter if config.token_budget.get("enabled") else None

            report = await asyncio.to_thread(
                run_offline_pipeline,
                config,
                source_root=source_path,
                output_root=output_path,
                workspace=workspace,
                replacement_index_path=replacement_index_path,
                tag_predictor=tag_predictor,
                classification_rules=classification_rules,
                policy_config=policy_config_arg,
                token_counter=token_counter_arg,
                ocr_engine=ocr_engine,
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

            # Count review is seeded during policy stage, not from pipeline report
    
            # Seed token review if overflows exist
            if report.token_overflows:
                token_store = TokenBudgetReviewStore(database, job_id)
                token_store.initialize(report.token_overflows)
    
            # Mark completed or failed based on blocking issues
            has_blocking = any(
                issue.blocking for issue in report.issues
            )
            final_status = "failed" if has_blocking else "completed"
            lifecycle.transition(final_status)
    
            # Issues are already persisted in the issues table via report
    
        except Exception as exc:
            # The traceback is operator data, not client data: write it to the
            # job workspace and store only a stable code on the job row.
            _record_job_failure(job_id, exc, traceback.format_exc())
    


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
            return {"job_id": job_id, "status": lifecycle.pause()}
        except LifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_transition", "message": str(exc)},
            ) from exc

    @router.post("/jobs/{job_id}/resume")
    async def resume_job(job_id: str) -> dict[str, Any]:
        """Resume a paused job."""
        from .lifecycle import LifecycleError

        lifecycle, _job = _lifecycle(job_id)
        try:
            return {"job_id": job_id, "status": lifecycle.resume()}
        except LifecycleError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "invalid_transition", "message": str(exc)},
            ) from exc

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
    ) -> dict[str, Any]:
        """Gate export on an explicit confirmation with nothing left pending."""
        from .count_review import CountReviewError

        if not request.confirmed:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "count_review_not_confirmed",
                    "message": "explicit count review confirmation is required",
                },
            )
        store = _count_store(job_id)
        try:
            store.assert_ready_for_export()
        except CountReviewError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "count_review_incomplete", "message": str(exc)},
            ) from exc
        return {"job_id": job_id, "confirmed": True, "pending": 0}


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
    ) -> dict[str, Any]:
        """Gate export on every overflow having been applied."""
        from .token_budget_review import TokenBudgetReviewError

        if not request.confirmed:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "token_review_not_confirmed",
                    "message": "explicit token budget review confirmation is required",
                },
            )
        store = _token_store(job_id)
        try:
            store.assert_ready_for_export()
        except TokenBudgetReviewError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "token_review_incomplete", "message": str(exc)},
            ) from exc
        return {"job_id": job_id, "confirmed": True, "unresolved": 0}
    @router.post("/jobs/preflight")
    async def preflight_job(config: dict[str, Any]) -> dict[str, Any]:
        """Validate job configuration before creation."""
        try:
            job_config = WorkflowJobConfigV1.from_payload(config)
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
    async def create_job(request: WorkflowJobCreateRequest, background_tasks: BackgroundTasks) -> WorkflowJobCreateResponse:
        """Create a new workflow job."""
        try:
            job_config = WorkflowJobConfigV1.from_payload(request.config)
            
            # Run preflight validation
            preflight_service.validate_config(job_config)
            
            workspace_root = database.db_path.parent / "jobs"
            workspace_root.mkdir(parents=True, exist_ok=True)

            job_id, _workspace = database.create_job(
                config_json=request.config,
                config_hash=job_config.config_hash(),
                profile=job_config.profile,
                work_mode=job_config.work_mode,
                overwrite_mode=job_config.overwrite_mode,
                source_root_id=job_config.source_root.root_id,
                output_root_id=job_config.output_root.root_id if job_config.output_root else None,
                workspace_root=workspace_root,
            )
            
            
            # Start execution in background
            background_tasks.add_task(_execute_job_async, job_id)
            
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
            _job_summary(job) for job in database.list_jobs(limit=limit, offset=offset)
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

    return router


__all__ = ["create_workflow_router"]

