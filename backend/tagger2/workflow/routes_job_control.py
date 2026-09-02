"""Job lifecycle-control routes for the workflow API.

These are the transition endpoints (pause/resume/start/cancel/repair/recover/
pin).  They register before the restore and review groups so the router keeps
its historical route order.
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from .api_context import WorkflowRouteContext
from .api_execution import _execute_job_async
from .api_models import WorkflowPinRequest
from .api_shared import _lifecycle


def register_job_control_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the job transition endpoints."""

    database = ctx.database

    @router.post("/jobs/{job_id}/pause")
    async def pause_job(job_id: str) -> dict[str, Any]:
        """Pause a running job so it can be resumed later."""
        from .lifecycle import LifecycleError

        lifecycle, _job = _lifecycle(ctx, job_id)
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

        lifecycle, job = _lifecycle(ctx, job_id)
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
            background_tasks.add_task(_execute_job_async, ctx, job_id)
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
        background_tasks.add_task(_execute_job_async, ctx, job_id)
        return {"job_id": job_id, "status": "queued"}

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel a job permanently. Cannot be resumed once cancelled."""
        from .lifecycle import LifecycleError

        lifecycle, _job = _lifecycle(ctx, job_id)
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
        lifecycle, job = _lifecycle(ctx, job_id)
        report = lifecycle.repair(Path(str(job["workspace_path"])))
        return {
            "job_id": job_id,
            **report.as_dict(),
            "resumable_samples": len(lifecycle.resumable_samples()),
        }

    @router.post("/jobs/{job_id}/recover")
    async def recover_job(job_id: str, background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Repair expired sample leases and queue an interrupted job."""

        lifecycle, job = _lifecycle(ctx, job_id)
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
        background_tasks.add_task(_execute_job_async, ctx, job_id)
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
