"""FastAPI routes for the local tag wiki.

Same mounting rules as every other module: the router is included before the
SPA catch-all and behind the shared ``authorize`` dependency. All errors are
``TagWikiError`` instances mapped to the app-wide error payload shape.

Route order matters: the specific ``/catalog/...`` routes are declared
before the generic ``/page/{title}`` route so a page can never shadow them
(the sets are disjoint today, but the ordering keeps that true if either
grows).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .contracts import (
    CATALOG_DEFAULT_PAGE_SIZE,
    CATALOG_MAX_PAGE_SIZE,
    AskRequest,
    BuildRequest,
    SearchRequest,
    TranslateRequest,
)
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

    # -- read-only tag catalog ------------------------------------------------

    @router.get("/catalog/categories")
    async def catalog_categories(
        profile: str = Query(default="e621", pattern="^(e621|danbooru)$"),
    ):
        try:
            return await service.catalog_categories(profile=profile)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.get("/catalog/tags")
    async def catalog_tags(
        profile: str = Query(default="e621", pattern="^(e621|danbooru)$"),
        category: str | None = Query(default=None, max_length=64),
        group: str | None = Query(default=None, max_length=64),
        q: str | None = Query(default=None, max_length=128),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=CATALOG_DEFAULT_PAGE_SIZE, ge=1, le=CATALOG_MAX_PAGE_SIZE),
    ):
        try:
            return await service.catalog_browse(
                profile=profile,
                category=category,
                group=group,
                q=q,
                offset=offset,
                limit=limit,
            )
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.get("/catalog/tags/{title}")
    async def catalog_tag_detail(
        title: str,
        profile: str = Query(default="e621", pattern="^(e621|danbooru)$"),
    ):
        try:
            return await service.catalog_tag_detail(title, profile=profile)
        except TagWikiError as exc:
            raise _error(exc) from exc

    @router.get("/page/{title}")
    async def page(
        title: str,
        profile: str = Query(default="e621", pattern="^(e621|danbooru)$"),
    ):
        try:
            return await service.page(title, profile=profile)
        except TagWikiError as exc:
            raise _error(exc) from exc

    return router


__all__ = ["create_tag_wiki_router"]
