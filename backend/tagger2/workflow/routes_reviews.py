"""Count-review and token-budget-review routes for the workflow API."""

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException

from .api_context import WorkflowRouteContext
from .api_execution import _execute_job_async
from .api_models import (
    WorkflowCountConfirmRequest,
    WorkflowCountResolveBatchRequest,
    WorkflowCountResolveRequest,
    WorkflowTokenReviewRequest,
)
from .api_shared import _count_store, _lifecycle, _token_counter_for_job, _token_store


def _review_gate(ctx: WorkflowRouteContext, job_id: str, section: str, waiting_status: str) -> bool:
    """Validate the immutable review gate before a confirm transition.

    Review rows can exist in old databases (and in operator-created test
    fixtures) even when the corresponding stage is disabled.  Their
    presence must not turn a disabled stage into an implicit gate.  The
    status check is deliberately performed here, before reading the review
    rows, so a confirm request can never requeue a job from ``pending`` or
    a different checkpoint.
    """

    _lifecycle_obj, job = _lifecycle(ctx, job_id)
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
    ctx: WorkflowRouteContext,
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

    lifecycle, job = _lifecycle(ctx, job_id)
    status = str(job["status"])
    if expected_status is not None and status != expected_status:
        raise LifecycleError(
            f"review checkpoint changed from {expected_status!r} to {status!r}"
        )
    # JobLifecycle.transition uses update_job_status(... expected_status=...)
    # so this is a compare-and-set, not a read-then-write transition.
    queued = lifecycle.transition("queued")
    background_tasks.add_task(_execute_job_async, ctx, job_id)
    return queued


def register_review_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the count-review and token-review endpoints."""

    database = ctx.database

    @router.get("/jobs/{job_id}/count-review")
    async def list_count_review(
        job_id: str,
        limit: int = 50,
        offset: int = 0,
        pending_only: bool = False,
    ) -> dict[str, Any]:
        """Page through count decisions with the evidence a reviewer needs."""
        store = _count_store(ctx, job_id)
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

        store = _count_store(ctx, job_id)
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

        store = _count_store(ctx, job_id)
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
        strict_gate = _review_gate(ctx, job_id, "count_review", "waiting_count_review")
        store = _count_store(ctx, job_id)
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
                ctx,
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

    @router.get("/jobs/{job_id}/token-review")
    async def list_token_review(
        job_id: str,
        limit: int = 50,
        offset: int = 0,
        unresolved_only: bool = False,
    ) -> dict[str, Any]:
        """Page through captions that overflow the token budget."""
        store = _token_store(ctx, job_id)
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

        store = _token_store(ctx, job_id)
        effective_token_counter = _token_counter_for_job(ctx, job_id)
        if effective_token_counter is None and request.action != "apply":
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
                count_tokens=effective_token_counter,
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
        strict_gate = _review_gate(ctx, job_id, "token_budget", "waiting_token_review")
        store = _token_store(ctx, job_id)
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
                ctx,
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
