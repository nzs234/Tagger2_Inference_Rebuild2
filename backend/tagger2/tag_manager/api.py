"""FastAPI routes for the tag manager workspace."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import ValidationError

from .contracts import (
    BatchOperationRequest,
    BatchOperationResponse,
    BatchPreviewResponse,
    CreateDatasetRequest,
    ImageEditRequest,
    ImageFilter,
    NlTranslateRequest,
    TagTranslateRequest,
    TranslationLookupRequest,
)
from .service import TagManagerError, TagManagerService


def _split_tag_query(values: Sequence[str]) -> list[str]:
    """Parse one or more tag query values into tags, losslessly.

    Each value is split on *unescaped* commas only: ``\\,`` is a literal comma
    and ``\\\\`` a literal backslash, so a tag containing a comma survives the
    round trip (``include_tags=a%5C,b`` selects the tag ``a,b``).  Values that
    contain no backslashes — everything sent by clients that never escape —
    parse exactly like the previous plain ``str.split(",")`` behaviour, and
    sending the parameter once per tag (repeated query parameters) works for
    both spellings.
    """

    tags: list[str] = []
    for raw in values:
        for part in _split_unescaped_commas(raw):
            tag = _unescape_tag(part.strip())
            if tag.strip():
                tags.append(tag)
    return tags


def _split_unescaped_commas(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == ",":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _unescape_tag(value: str) -> str:
    out: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            # Only the two escapes we document are rewritten; any other
            # ``\\x`` sequence stays verbatim so legacy values are unchanged.
            out.append(char if char in {",", "\\"} else f"\\{char}")
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:
        out.append("\\")
    return "".join(out)


def _error(exc: TagManagerError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def create_tag_manager_router(service: TagManagerService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tag-manager", tags=["tag-manager"])

    @router.post("/datasets", status_code=202)
    async def create_dataset(request: CreateDatasetRequest):
        # The blocking session insert runs on a worker thread, but the scan
        # scheduling must stay on the event loop thread: IndexScheduler binds
        # the scan future to the running loop (calling it from a worker
        # thread would run the whole scan inline instead of answering 202).
        try:
            session = await asyncio.to_thread(service.create_session, request)
        except TagManagerError as exc:
            raise _error(exc) from exc
        service.schedule_index(str(session["id"]))
        return session

    @router.get("/datasets")
    async def list_datasets():
        return {"items": await asyncio.to_thread(service.list_sessions)}

    @router.get("/datasets/{session_id}")
    async def get_dataset(session_id: str):
        try:
            return await asyncio.to_thread(service.get_session, session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.delete("/datasets/{session_id}", status_code=204)
    async def delete_dataset(session_id: str):
        # The delete takes the session write lock and drops queued scans;
        # IndexScheduler.discard marshals the asyncio cancel back onto the
        # loop that owns the future, so cancelling from this worker thread
        # stays event-loop safe.
        try:
            await asyncio.to_thread(service.delete_session, session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc
        return Response(status_code=204)

    @router.post("/datasets/{session_id}/refresh", status_code=202)
    async def refresh_dataset(session_id: str):
        # Unlike the other mutating routes this one stays on the event loop
        # thread on purpose: refresh_session only probes the write lock and
        # queues the scan, and IndexScheduler.schedule must observe the
        # running loop (offloading it would execute the whole scan inline
        # inside a worker thread before the 202 could be answered).  Its own
        # database reads are single indexed lookups.
        try:
            return service.refresh_session(session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/cancel")
    async def cancel_dataset_scan(session_id: str):
        # Cooperative scan cancel: only sets a threading.Event, so the worker
        # offload below is cheap.  Idempotent by design — with no scan in
        # flight it answers 200 {"cancelled": false} rather than an error, so
        # a stale retry can never turn into a user-visible failure.
        try:
            return await asyncio.to_thread(service.cancel_scan, session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/images")
    async def list_images(
        session_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        sort: str = Query(default="name", pattern="^(name|mtime|mtime_asc|tags|tag_count_asc)$"),
        include_tags: list[str] | None = Query(default=None),
        exclude_tags: list[str] | None = Query(default=None),
        include_mode: str = Query(default="all", pattern="^(all|any)$"),
        kind: str = Query(
            default="any",
            pattern="^(any|none|tag_txt|tags_json|standard_json|raw_e621_json)$",
        ),
        sidecar: str = Query(default="any", pattern="^(any|present|missing)$"),
    ):
        # Tag parameters arrive either comma-separated (legacy) or as repeated
        # query parameters with backslash-escaped commas (lossless); both are
        # parsed on the boundary so the validated ImageFilter stays the single
        # contract shape, including its per-tag length limits (stable 422).
        payload = {
            "include_tags": _split_tag_query(include_tags or ()),
            "exclude_tags": _split_tag_query(exclude_tags or ()),
            "include_mode": include_mode,
            "kind": kind,
            "sidecar": sidecar,
        }
        try:
            image_filter = ImageFilter.model_validate(payload)
        except ValidationError as exc:
            # Per-tag and total length limits end here as a stable 422 in the
            # same envelope shape the global handler produces for request
            # validation failures.
            fields: dict[str, list[str]] = {}
            for error in exc.errors():
                location = ".".join(str(part) for part in error.get("loc", ()))
                fields.setdefault(location or "filter", []).append(
                    str(error.get("msg") or "invalid value")
                )
            raise HTTPException(
                status_code=422,
                detail={"code": "validation_error", "message": "请求参数校验失败", "fields": fields},
            ) from exc
        try:
            return await asyncio.to_thread(
                service.list_images,
                session_id,
                image_filter=image_filter,
                sort=sort,
                offset=offset,
                limit=limit,
            )
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/images/{image_id}")
    async def get_image(session_id: str, image_id: int):
        try:
            return await asyncio.to_thread(service.get_image, session_id, image_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.patch("/datasets/{session_id}/images/{image_id}")
    async def save_image(session_id: str, image_id: int, edit: ImageEditRequest):
        try:
            return await asyncio.to_thread(service.save_image, session_id, image_id, edit)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/batch", response_model=BatchOperationResponse)
    async def batch_operation(session_id: str, request: BatchOperationRequest):
        try:
            return await asyncio.to_thread(service.batch_operation, session_id, request)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/batch/preview", response_model=BatchPreviewResponse)
    async def preview_batch(session_id: str, request: BatchOperationRequest):
        # Read-only, but it resolves targets and renders candidate sidecars, so
        # it runs off the event loop like the real batch.
        try:
            return await asyncio.to_thread(service.preview_batch, session_id, request)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/undo")
    async def undo(session_id: str):
        try:
            return await asyncio.to_thread(service.undo, session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/redo")
    async def redo(session_id: str):
        try:
            return await asyncio.to_thread(service.redo, session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/tags/stats")
    async def tag_stats(
        session_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        min_count: int = Query(default=1, ge=1),
    ):
        try:
            items = await asyncio.to_thread(
                service.tag_stats, session_id, limit=limit, min_count=min_count
            )
            return {"items": items}
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/images/{image_id}/thumbnail")
    async def thumbnail(
        session_id: str,
        image_id: int,
        size: int = Query(default=256, ge=32, le=512),
    ):
        try:
            path = await asyncio.to_thread(
                service.thumbnail, session_id, image_id, size=size
            )
        except TagManagerError as exc:
            raise _error(exc) from exc
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/tag-db")
    async def tag_db(
        profile: str = Query(default="e621", pattern="^(e621|danbooru)$"),
        query: str = Query(default="", max_length=128),
        limit: int = Query(default=20, ge=1, le=50),
        resource_id: str | None = Query(default=None, max_length=128),
    ):
        try:
            # ensure_loaded may open a full snapshot database: keep it off
            # the event loop.
            return await asyncio.to_thread(
                service.autocomplete, profile, query, limit=limit, resource_id=resource_id
            )
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/tag-db/info")
    async def tag_db_info():
        return await asyncio.to_thread(service.tag_db_info)

    @router.post("/translations/lookup")
    async def lookup_translations(request: TranslationLookupRequest):
        return await asyncio.to_thread(service.lookup_translations, request)

    @router.post("/translations/translate")
    async def translate_tags(request: TagTranslateRequest):
        try:
            return await service.translate_tags(request)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/nl/translate")
    async def translate_nl(request: NlTranslateRequest):
        try:
            return await service.translate_nl(request)
        except TagManagerError as exc:
            raise _error(exc) from exc

    return router


__all__ = ["create_tag_manager_router"]
