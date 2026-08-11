"""Workflow API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import PathAllowlist
from .contracts import WorkflowJobConfigV1, WorkflowPathRef
from .db import WorkflowDatabase, default_workflow_database_path
from .preflight import WorkflowPreflightError, WorkflowPreflightService
from .resources import WorkflowResourceCatalog


class WorkflowJobCreateRequest(BaseModel):
    """Request to create a workflow job."""
    config: dict[str, Any]


class WorkflowJobCreateResponse(BaseModel):
    """Response after creating a workflow job."""
    job_id: str
    status: str


class WorkflowJobStatusResponse(BaseModel):
    """Workflow job status response."""
    job_id: str
    status: str
    profile: str
    work_mode: str
    total_samples: int
    processed_samples: int
    succeeded_samples: int
    failed_samples: int
    skipped_samples: int
    current_module_id: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None


class WorkflowResourceImportPreviewRequest(BaseModel):
    """Request to preview resource import."""
    source_path: str
    resource_id: str
    category: str


class WorkflowResourceImportPreviewResponse(BaseModel):
    """Response for resource import preview."""
    valid: bool
    errors: list[str]
    warnings: list[str]
    line_count: int
    fingerprint: str | None


def create_workflow_router(
    allowlist: PathAllowlist,
    resource_catalog: WorkflowResourceCatalog,
    database: WorkflowDatabase | None = None,
) -> APIRouter:
    """Create workflow API router."""
    
    router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])
    
    if database is None:
        database = WorkflowDatabase(default_workflow_database_path())
    
    preflight_service = WorkflowPreflightService(allowlist, resource_catalog)

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
        request: WorkflowResourceImportPreviewRequest,
    ) -> WorkflowResourceImportPreviewResponse:
        """Preview resource import without applying."""
        from pathlib import Path
        
        source_path = Path(request.source_path)
        
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="Source file not found")
        
        # Validate CSV if it's a replace resource
        validation = resource_catalog.validate_csv_resource(source_path)
        
        fingerprint = None
        if validation["valid"]:
            fingerprint = resource_catalog.fingerprint_file(source_path)
        
        return WorkflowResourceImportPreviewResponse(
            valid=validation["valid"],
            errors=validation["errors"],
            warnings=[],
            line_count=validation["line_count"],
            fingerprint=fingerprint,
        )

    @router.post("/resources/import/apply")
    async def apply_resource_import(
        request: WorkflowResourceImportPreviewRequest,
    ) -> dict[str, Any]:
        """Import and register a resource."""
        from pathlib import Path
        
        source_path = Path(request.source_path)
        
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="Source file not found")
        
        # Validate first
        validation = resource_catalog.validate_csv_resource(source_path)
        if not validation["valid"]:
            raise HTTPException(
                status_code=400,
                detail={"code": "validation_failed", "errors": validation["errors"]}
            )
        
        # Import resource
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
        }

    @router.post("/jobs/preflight")
    async def preflight_job(config: dict[str, Any]) -> dict[str, Any]:
        """Validate job configuration before creation."""
        try:
            job_config = WorkflowJobConfigV1(**config)
            report = preflight_service.validate_config(job_config)
            return report
        except WorkflowPreflightError as e:
            raise HTTPException(
                status_code=400,
                detail={"code": e.code, "message": e.message, "details": e.details}
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_config", "message": str(e)}
            )

    @router.post("/jobs", response_model=WorkflowJobCreateResponse)
    async def create_job(request: WorkflowJobCreateRequest) -> WorkflowJobCreateResponse:
        """Create a new workflow job."""
        try:
            job_config = WorkflowJobConfigV1(**request.config)
            
            # Run preflight validation
            preflight_service.validate_config(job_config)
            
            # Create workspace directory
            from ..config import get_settings
            settings = get_settings()
            workspace_root = settings.data_dir / "workflows" / "jobs"
            workspace_root.mkdir(parents=True, exist_ok=True)
            
            # Create job in database
            job_id = database.create_job(
                config_json=request.config,
                config_hash=job_config.config_hash(),
                profile=job_config.profile,
                work_mode=job_config.work_mode,
                overwrite_mode=job_config.overwrite_mode,
                source_root_id=job_config.source_root.root_id,
                output_root_id=job_config.output_root.root_id if job_config.output_root else None,
                workspace_path=str(workspace_root / job_id),
            )
            
            return WorkflowJobCreateResponse(job_id=job_id, status="pending")
        
        except WorkflowPreflightError as e:
            raise HTTPException(
                status_code=400,
                detail={"code": e.code, "message": e.message, "details": e.details}
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail={"code": "job_creation_failed", "message": str(e)}
            )

    @router.get("/jobs")
    async def list_jobs(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """List workflow jobs."""
        jobs = database.list_jobs(limit=limit, offset=offset)
        return jobs

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
            error=job["error"],
        )

    @router.get("/jobs/{job_id}/issues")
    async def list_job_issues(job_id: str, blocking_only: bool = False) -> list[dict[str, Any]]:
        """List issues for a workflow job."""
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        issues = database.list_issues(job_id, blocking_only=blocking_only)
        return issues

    return router


__all__ = ["create_workflow_router"]
