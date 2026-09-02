"""Job CRUD routes for the workflow API: create, preflight, list, inspect."""

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .api_context import WorkflowRouteContext
from .api_models import (
    WorkflowJobCreateRequest,
    WorkflowJobCreateResponse,
    WorkflowJobIssueResponse,
    WorkflowJobReportResponse,
    WorkflowJobStatusResponse,
    WorkflowJobSummaryResponse,
    _job_summary,
)
from .api_shared import _preflight_http_error, _resolve_caption_model
from .contracts import WorkflowJobConfigV2
from .preflight import WorkflowPreflightError


def register_job_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the job creation/inspection endpoints."""

    database = ctx.database
    preflight_service = ctx.preflight_service

    @router.post("/jobs/preflight")
    async def preflight_job(config: dict[str, Any]) -> dict[str, Any]:
        """Validate job configuration before creation."""
        try:
            job_config = WorkflowJobConfigV2.from_payload(config)
            job_config = _resolve_caption_model(ctx, job_config)
            # Preflight fingerprints resources (file hashing) and queries the
            # database; keep that blocking work off the event loop.
            report = await asyncio.to_thread(preflight_service.validate_config, job_config)
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
            job_config = _resolve_caption_model(ctx, job_config)

            # Run preflight validation (resource fingerprinting + DB queries
            # are blocking; keep them off the event loop).
            await asyncio.to_thread(preflight_service.validate_config, job_config)

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
            restored_at=job.get("restored_at"),
            discarded_at=job.get("discarded_at"),
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
                detail={
                    "code": "report_unreadable",
                    "message": "the persisted workflow report is unreadable",
                },
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
