"""Workflow API routes."""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Any, Callable, Literal, Sequence

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pathlib import Path

from ..security import PathAllowlist, PathNotAllowedError
from .contracts import WorkflowJobConfigV1
from .db import WorkflowDatabase, default_workflow_database_path
from .preflight import WorkflowPreflightError, WorkflowPreflightService
from .resources import WorkflowResourceCatalog


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
    error: str | None


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
    """Response for a resource import preview."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    rule_count: int
    action_counts: dict[str, int]
    passthrough_count: int
    fingerprint: str | None


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
    async def _execute_job_async(job_id: str) -> None:
        """Execute a workflow job in the background, updating status and seeding reviews."""
        from .lifecycle import JobLifecycle
        from .pipeline import run_offline_pipeline
        from .count_review import CountReviewStore
        from .token_budget_review import TokenBudgetReviewStore
        import traceback
    
        try:
            job = database.get_job(job_id)
            if job is None:
                return
    
            lifecycle = JobLifecycle(database, job_id)
            lifecycle.transition_to("running")
    
            config = WorkflowJobConfigV1.from_payload(job["config"])
            workspace = Path(job["workspace"])
    
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
                lifecycle.transition_to("failed")
                database.update_job(job_id, error_message=f"path not allowed: {exc}")
                return
    
            # Wire up resources from catalog
            replacement_index_path = None
            if config.replace.get("enabled"):
                # Use the designated e621 replacement index
                replacement_index_path = resource_catalog.get_resource_path("e621-replacement-index-v1")
                if replacement_index_path is None:
                    raise ValueError("replace stage enabled but e621-replacement-index-v1 not found in catalog")
            
            # TODO: wire up tag_predictor, policy_config, token_counter when available
            report = run_offline_pipeline(
                config,
                source_root=source_path,
                output_root=output_path,
                workspace=workspace,
                replacement_index_path=replacement_index_path,
            )
    
            # Seed count review if decisions exist
            if report.count_decisions:
                count_store = CountReviewStore(database, job_id)
                count_store.initialize(report.count_decisions)
    
            # Seed token review if overflows exist
            if report.token_overflows:
                token_store = TokenBudgetReviewStore(database, job_id)
                token_store.initialize(report.token_overflows)
    
            # Mark completed or failed based on blocking issues
            has_blocking = any(
                issue.get("blocking", False) for issue in report.issues
            )
            final_status = "failed" if has_blocking else "completed"
            lifecycle.transition_to(final_status)
    
            if report.issues:
                database.update_job(
                    job_id,
                    error_message=f"{len(report.issues)} issue(s) recorded",
                )
    
        except Exception as exc:
            try:
                lifecycle = JobLifecycle(database, job_id)
                lifecycle.transition_to("failed")
                database.update_job(
                        job_id,
                        error_message=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
                    )
            except Exception:
                pass  # Already failed, no need to propagate
    


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
        report = resource_catalog.validate_csv_resource(source_path)

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
        )

    @router.post("/resources/import/apply")
    async def apply_resource_import(
        request: WorkflowResourceImportRequest,
    ) -> dict[str, Any]:
        """Import and register a resource after re-validating it."""
        source_path = _resolve_resource_source(request)

        report = resource_catalog.validate_csv_resource(source_path)
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

    @router.get("/jobs")
    async def list_jobs(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List workflow jobs."""
        jobs = database.list_jobs(limit=limit, offset=offset)
        return jobs

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
            error=job["error"],
        )

    @router.get("/jobs/{job_id}/issues")
    async def list_job_issues(job_id: str, blocking_only: bool = False) -> list[dict[str, Any]]:
        """List issues for a workflow job."""
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        issues = database.list_issues(job_id, blocking_only=blocking_only)
        return issues

    return router


__all__ = ["create_workflow_router"]

