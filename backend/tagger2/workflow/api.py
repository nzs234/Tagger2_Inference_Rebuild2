"""Workflow API routes.

The router is assembled by one ``register_*`` helper per route group; this
module keeps the public factory, the router-level setup (database default,
interrupted-job marking, preflight service) and re-exports every request/
response model plus ``_public_error_code`` so ``tagger2.workflow.api`` keeps
its historical import surface.
"""

import logging
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import APIRouter

from ..security import PathAllowlist
from .api_context import WorkflowRouteContext
from .api_models import (
    WorkflowCountConfirmRequest,
    WorkflowCountResolveBatchRequest,
    WorkflowCountResolveRequest,
    WorkflowJobCreateRequest,
    WorkflowJobCreateResponse,
    WorkflowJobIssueResponse,
    WorkflowJobReportResponse,
    WorkflowJobStatusResponse,
    WorkflowJobSummaryResponse,
    WorkflowPathBindingPreviewRequest,
    WorkflowPathBindingPreviewResponse,
    WorkflowPathBindingRequest,
    WorkflowPathRefResponse,
    WorkflowPathBindingResponse,
    WorkflowPinRequest,
    WorkflowResourceImportPreviewResponse,
    WorkflowResourceImportRequest,
    WorkflowRestoreRequest,
    WorkflowTokenReviewRequest,
)
from .api_shared import _public_error_code as _public_error_code
from .db import WorkflowDatabase, default_workflow_database_path
from .preflight import WorkflowPreflightService
from .resources import WorkflowResourceCatalog
from .routes_events import register_event_routes
from .routes_job_control import register_job_control_routes
from .routes_jobs import register_job_routes
from .routes_paths import register_path_binding_routes
from .routes_resources import register_resource_routes
from .routes_restore import register_restore_routes
from .routes_reviews import register_review_routes


logger = logging.getLogger("tagger2.workflow.api")

__all__ = [
    "create_workflow_router",
    "WorkflowCountConfirmRequest",
    "WorkflowCountResolveBatchRequest",
    "WorkflowCountResolveRequest",
    "WorkflowJobCreateRequest",
    "WorkflowJobCreateResponse",
    "WorkflowJobIssueResponse",
    "WorkflowJobReportResponse",
    "WorkflowJobStatusResponse",
    "WorkflowJobSummaryResponse",
    "WorkflowPathBindingPreviewRequest",
    "WorkflowPathBindingPreviewResponse",
    "WorkflowPathBindingRequest",
    "WorkflowPathRefResponse",
    "WorkflowPathBindingResponse",
    "WorkflowPinRequest",
    "WorkflowResourceImportPreviewResponse",
    "WorkflowResourceImportRequest",
    "WorkflowRestoreRequest",
    "WorkflowTokenReviewRequest",
]


def create_workflow_router(
    allowlist: PathAllowlist,
    resource_catalog: WorkflowResourceCatalog,
    database: WorkflowDatabase | None = None,
    token_counter: Callable[[Sequence[str]], Sequence[int]] | None = None,
    model_registry: Any | None = None,
    inference_engine: Any | None = None,
    storage: Any | None = None,
    root_registrar: Callable[..., Any] | None = None,
) -> APIRouter:
    """Create workflow API router.

    ``token_counter`` stays optional because the tokenizer resource is not
    bundled. Without it the token review actions that need a count fail closed
    with ``token_review_unavailable`` instead of guessing a length.
    """

    router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

    if database is None:
        database = WorkflowDatabase(default_workflow_database_path())

    # The workflow executor is currently process-local.  If the host was
    # restarted, queued/running rows no longer have a worker behind them.  Make
    # that fact durable before serving routes so operators can recover them
    # instead of observing jobs stuck forever in ``running``.
    database.mark_interrupted_jobs()

    preflight_service = WorkflowPreflightService(
        allowlist,
        resource_catalog,
        database,
        model_registry=model_registry,
        inference_engine=inference_engine,
    )

    ctx = WorkflowRouteContext(
        allowlist=allowlist,
        resources=resource_catalog,
        database=database,
        registry=model_registry,
        engine=inference_engine,
        storage=storage,
        root_registrar=root_registrar,
        token_counter=token_counter,
        preflight_service=preflight_service,
    )

    # One call per route group, in the historical registration order, so the
    # router's route table (order included) is unchanged by the split.
    register_path_binding_routes(router, ctx)
    register_resource_routes(router, ctx)
    register_job_control_routes(router, ctx)
    register_restore_routes(router, ctx)
    register_review_routes(router, ctx)
    register_job_routes(router, ctx)
    register_event_routes(router, ctx)

    return router
