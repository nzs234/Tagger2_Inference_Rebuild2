"""FastAPI routes for the tag manager workspace."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse

from .contracts import (
    BatchOperationRequest,
    CreateDatasetRequest,
    ImageEditRequest,
    ImageFilter,
    NlTranslateRequest,
    TranslationLookupRequest,
)
from .service import TagManagerError, TagManagerService


def _error(exc: TagManagerError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def create_tag_manager_router(service: TagManagerService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tag-manager", tags=["tag-manager"])

    @router.post("/datasets", status_code=202)
    async def create_dataset(request: CreateDatasetRequest):
        try:
            session = service.create_session(request)
        except TagManagerError as exc:
            raise _error(exc) from exc
        service.schedule_index(str(session["id"]))
        return session

    @router.get("/datasets")
    async def list_datasets():
        return {"items": service.list_sessions()}

    @router.get("/datasets/{session_id}")
    async def get_dataset(session_id: str):
        try:
            return service.get_session(session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.delete("/datasets/{session_id}", status_code=204)
    async def delete_dataset(session_id: str):
        try:
            service.delete_session(session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc
        return Response(status_code=204)

    @router.post("/datasets/{session_id}/refresh", status_code=202)
    async def refresh_dataset(session_id: str):
        try:
            return service.refresh_session(session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/images")
    async def list_images(
        session_id: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        sort: str = Query(default="name", pattern="^(name|mtime|tags)$"),
        include_tags: str = Query(default="", max_length=4096),
        exclude_tags: str = Query(default="", max_length=4096),
        include_mode: str = Query(default="all", pattern="^(all|any)$"),
        kind: str = Query(
            default="any",
            pattern="^(any|none|tag_txt|tags_json|standard_json|raw_e621_json)$",
        ),
        sidecar: str = Query(default="any", pattern="^(any|present|missing)$"),
    ):
        # Query params arrive comma-separated; parse on the boundary so the
        # validated ImageFilter stays the single contract shape.
        payload = {
            "include_tags": [tag for tag in include_tags.split(",") if tag.strip()],
            "exclude_tags": [tag for tag in exclude_tags.split(",") if tag.strip()],
            "include_mode": include_mode,
            "kind": kind,
            "sidecar": sidecar,
        }
        try:
            image_filter = ImageFilter.model_validate(payload)
            return service.list_images(
                session_id, image_filter=image_filter, sort=sort, offset=offset, limit=limit
            )
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/images/{image_id}")
    async def get_image(session_id: str, image_id: int):
        try:
            return service.get_image(session_id, image_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.patch("/datasets/{session_id}/images/{image_id}")
    async def save_image(session_id: str, image_id: int, edit: ImageEditRequest):
        try:
            return service.save_image(session_id, image_id, edit)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/batch")
    async def batch_operation(session_id: str, request: BatchOperationRequest):
        try:
            return service.batch_operation(session_id, request)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/undo")
    async def undo(session_id: str):
        try:
            return service.undo(session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.post("/datasets/{session_id}/redo")
    async def redo(session_id: str):
        try:
            return service.redo(session_id)
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/tags/stats")
    async def tag_stats(
        session_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        min_count: int = Query(default=1, ge=1),
    ):
        try:
            return {
                "items": service.tag_stats(session_id, limit=limit, min_count=min_count)
            }
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/datasets/{session_id}/images/{image_id}/thumbnail")
    async def thumbnail(
        session_id: str,
        image_id: int,
        size: int = Query(default=256, ge=32, le=512),
    ):
        import asyncio

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
            return service.autocomplete(
                profile, query, limit=limit, resource_id=resource_id
            )
        except TagManagerError as exc:
            raise _error(exc) from exc

    @router.get("/tag-db/info")
    async def tag_db_info():
        return service.tag_db_info()

    @router.post("/translations/lookup")
    async def lookup_translations(request: TranslationLookupRequest):
        return service.lookup_translations(request)

    @router.post("/nl/translate")
    async def translate_nl(request: NlTranslateRequest):
        try:
            return await service.translate_nl(request)
        except TagManagerError as exc:
            raise _error(exc) from exc

    return router


__all__ = ["create_tag_manager_router"]
