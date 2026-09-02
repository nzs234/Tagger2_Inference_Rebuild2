"""Workflow event routes: JSON replay pages and the SSE stream."""

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .api_context import WorkflowRouteContext


def register_event_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the job event endpoints."""

    database = ctx.database

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
    async def stream_job_events(
        job_id: str,
        request: Request,
        after_event_id: int = 0,
    ):
        """Stream durable workflow events until the job reaches a terminal state.

        ``Last-Event-ID`` wins over the query parameter on reconnect.  Empty
        heartbeat frames keep proxies and browsers from treating an idle job as
        a dead connection; the JSON cursor endpoint above remains the fallback
        for clients that cannot hold an authenticated stream open.
        """

        from fastapi.responses import StreamingResponse

        if database.get_job(job_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
            )
        header_cursor = request.headers.get("last-event-id")
        if header_cursor is not None:
            try:
                after_event_id = int(header_cursor)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_event_cursor", "message": "Last-Event-ID must be an integer"},
                )
        if after_event_id < 0:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_event_cursor", "message": "after_event_id must be non-negative"},
            )

        async def body():
            cursor = int(after_event_id)
            while not await request.is_disconnected():
                events = database.list_events(job_id, after_event_id=cursor, limit=500)
                for event in events:
                    cursor = int(event["event_id"])
                    yield (
                        f"id: {cursor}\n"
                        "event: workflow\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )

                job = database.get_job(job_id)
                if job is None:
                    return
                terminal = str(job.get("status", "")) in {
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                    "rollback_required",
                } or bool(job.get("discarded_at"))
                if terminal:
                    # A terminal status and its event are committed together;
                    # re-read once to close the transaction/stream race.
                    if database.list_events(job_id, after_event_id=cursor, limit=1):
                        continue
                    return

                yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
