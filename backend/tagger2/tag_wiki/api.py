"""FastAPI routes for the local tag wiki.

Same mounting rules as every other module: the router is included before the
SPA catch-all and behind the shared ``authorize`` dependency. All errors are
``TagWikiError`` instances mapped to the app-wide error payload shape.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .contracts import AskRequest, BuildRequest, SearchRequest, TranslateRequest
from .service import TagWikiError, TagWikiService


def _error(exc: TagWikiError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    )


def create_tag_wiki_router(service: TagWikiService) -> APIRouter:
    router = APIRouter(prefix="/api/v1/tag-wiki", tags=["tag-wiki"])

    @router.get("/status")
    async def status():
        return service.status()

    @router.post("/build", status_code=202)
    async def build(request: BuildRequest):
        try:
            return await service.start_build(request)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.post("/translate", status_code=202)
    async def translate(request: TranslateRequest):
        try:
            return await service.start_translate(request)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.get("/translate/progress")
    async def translate_progress():
        return service.translate_progress()

    @router.get("/lookup")
    async def lookup(
        tag: str = Query(min_length=1, max_length=128),
        profile: str = Query(default="e621", pattern="^(e621|danbooru)$"),
    ):
        try:
            return await service.lookup(tag, profile=profile)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.post("/search")
    async def search(request: SearchRequest):
        try:
            return await service.search(request)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.post("/ask")
    async def ask(request: AskRequest):
        try:
            return await service.ask(request)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.get("/page/{title}")
    async def page(title: str):
        try:
            return await service.page(title)
        except TagWikiError as exc:
            raise _error(exc) from exc

    return router


__all__ = ["create_tag_wiki_router"]
