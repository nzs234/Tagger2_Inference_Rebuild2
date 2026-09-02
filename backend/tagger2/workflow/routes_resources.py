"""Capability and resource-import routes for the workflow API."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .api_context import WorkflowRouteContext
from .api_models import WorkflowResourceImportPreviewResponse, WorkflowResourceImportRequest
from ..security import PathNotAllowedError


def _resolve_resource_source(ctx: WorkflowRouteContext, request: WorkflowResourceImportRequest) -> Path:
    try:
        return ctx.allowlist.resolve(
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


def register_resource_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the capability and resource-import endpoints."""

    resource_catalog = ctx.resources

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

    @router.post("/resources/import/preview")
    async def preview_resource_import(
        request: WorkflowResourceImportRequest,
    ) -> WorkflowResourceImportPreviewResponse:
        """Validate a resource file without importing it."""
        source_path = _resolve_resource_source(ctx, request)
        report = resource_catalog.validate_resource(source_path, request.category)

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
            profile=report.get("profile") or None,
            tag_count=report.get("tag_count"),
            alias_count=report.get("alias_count"),
            implication_count=report.get("implication_count"),
            category_counts=report.get("category_counts"),
        )

    @router.post("/resources/import/apply")
    async def apply_resource_import(
        request: WorkflowResourceImportRequest,
    ) -> dict[str, Any]:
        """Import and register a resource after re-validating it."""
        source_path = _resolve_resource_source(ctx, request)

        report = resource_catalog.validate_resource(source_path, request.category)
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
