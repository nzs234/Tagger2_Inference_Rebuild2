"""FastAPI routes for the image-generation workspace."""

from __future__ import annotations

import json

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from .contracts import ImageJobConfig
from .service import ImageGenerationService, ImageGenerationServiceError


def _error(exc: ImageGenerationServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def create_image_generation_router(service: ImageGenerationService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/image-generation", tags=["image-generation"])

    @router.get("/capabilities")
    async def capabilities(provider_id: str | None = None, model: str | None = None):
        try:
            return service.capabilities(provider_id=provider_id, model=model)
        except ImageGenerationServiceError as exc:
            raise _error(exc) from exc

    @router.post("/jobs", status_code=202)
    async def create_job(
        config: str = Form(..., min_length=2, max_length=100_000),
        references: list[UploadFile] = File(default=[]),
    ):
        try:
            payload = ImageJobConfig.model_validate(json.loads(config))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"code": "image_config_invalid", "message": "图像生成配置无效"}) from exc
        uploads: list[tuple[str, bytes, str]] = []
        total = 0
        if len(references) > 16:
            raise HTTPException(status_code=413, detail={"code": "image_reference_count_exceeded", "message": "参考图数量超过限制"})
        for upload in references:
            data = await upload.read(service.settings.max_upload_bytes + 1)
            total += len(data)
            if total > service.settings.max_request_bytes:
                raise HTTPException(status_code=413, detail={"code": "image_request_too_large", "message": "图像请求超过大小限制"})
            uploads.append((upload.filename or "reference", data, upload.content_type or "application/octet-stream"))
        try:
            return await service.create_job(payload, uploads)
        except ImageGenerationServiceError as exc:
            raise _error(exc) from exc

    @router.get("/jobs")
    async def list_jobs(
        limit: int = Query(default=50, ge=1, le=200),
        cursor: int = Query(default=0, ge=0),
        q: str = Query(default="", max_length=200),
        state: str | None = Query(default=None, max_length=32),
    ):
        return service.list_jobs(limit=limit, offset=cursor, query=q, state=state)

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        try:
            return service.get_job(job_id)
        except ImageGenerationServiceError as exc:
            raise _error(exc) from exc

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        try:
            return await service.cancel_job(job_id)
        except ImageGenerationServiceError as exc:
            raise _error(exc) from exc

    @router.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        try:
            return await service.retry_job(job_id)
        except ImageGenerationServiceError as exc:
            raise _error(exc) from exc

    @router.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: str):
        try:
            await service.delete_job(job_id)
        except ImageGenerationServiceError as exc:
            raise _error(exc) from exc
        return Response(status_code=204)

    @router.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str):
        result = service.artifact_data(artifact_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "image_artifact_not_found", "message": "图像产物不存在"})
        data, mime, filename = result
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


__all__ = ["create_image_generation_router"]
