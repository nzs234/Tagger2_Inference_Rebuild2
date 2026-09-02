"""Request/response DTOs for the workflow API.

Moved verbatim from ``tagger2.workflow.api``.  ``api.py`` re-exports every
model, so ``tagger2.workflow.api.<Model>`` keeps resolving for callers and the
response-DTO contract test keeps discovering them.
"""

from typing import Any, Literal

from pydantic import BaseModel


class WorkflowJobCreateRequest(BaseModel):
    """Request to create a workflow job."""
    config: dict[str, Any]


class WorkflowPathBindingPreviewRequest(BaseModel):
    """Absolute paths entered by the local UI before root binding."""

    source_path: str
    output_path: str | None = None
    work_mode: Literal["full_copy", "in_place"] = "full_copy"


class WorkflowPathBindingRequest(WorkflowPathBindingPreviewRequest):
    """Resolve manual paths and optionally create the output directory."""

    create_output: bool = False


class WorkflowPathRefResponse(BaseModel):
    """Internal path reference; never contains an absolute filesystem path."""

    root_id: str
    relative_path: str


class WorkflowPathBindingPreviewResponse(BaseModel):
    status: Literal["ready", "create_required", "not_applicable"]
    source_bound: bool
    output_bound: bool
    output_create_required: bool
    warnings: list[str] = []
    errors: list[str] = []


class WorkflowPathBindingResponse(BaseModel):
    status: Literal["ready", "not_applicable"]
    source: WorkflowPathRefResponse
    output: WorkflowPathRefResponse | None = None
    output_created: bool = False


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
    restored_at: str | None = None
    discarded_at: str | None = None
    # A stable public code, never an exception message or traceback. The full
    # diagnosis stays server-side in the job workspace.
    error_code: str | None


class WorkflowJobSummaryResponse(BaseModel):
    """One row of the job list.

    Deliberately narrower than the ``workflow_jobs`` table: `workspace_path`,
    `config_json` and `config_hash` are server-side details and must not cross
    the API boundary.
    """

    job_id: str
    status: str
    profile: str
    work_mode: str
    overwrite_mode: str
    source_root_id: str
    output_root_id: str | None
    current_module_id: str | None
    total_samples: int
    processed_samples: int
    succeeded_samples: int
    failed_samples: int
    skipped_samples: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    restored_at: str | None = None
    discarded_at: str | None = None
    error_code: str | None
    pinned: bool = False


class WorkflowJobIssueResponse(BaseModel):
    """One recorded issue, addressed by relative path only."""

    issue_id: str
    sample_id: int | None
    relative_image_path: str | None
    module_id: str
    code: str
    message: str
    severity: str
    blocking: bool
    created_at: str | None


class WorkflowJobReportResponse(BaseModel):
    """Per-stage report for a finished job."""

    job_id: str
    available: bool
    report: dict[str, Any] | None = None


def _job_summary(job: dict[str, Any], *, pinned: bool = False) -> WorkflowJobSummaryResponse:
    """Project a `workflow_jobs` row onto the public summary.

    Whitelists fields explicitly, so a future column added to the table cannot
    silently start crossing the API boundary.
    """

    return WorkflowJobSummaryResponse(
        job_id=str(job["job_id"]),
        status=str(job["status"]),
        profile=str(job["profile"]),
        work_mode=str(job["work_mode"]),
        overwrite_mode=str(job["overwrite_mode"]),
        source_root_id=str(job["source_root_id"]),
        output_root_id=job["output_root_id"],
        current_module_id=job["current_module_id"],
        total_samples=int(job["total_samples"]),
        processed_samples=int(job["processed_samples"]),
        succeeded_samples=int(job["succeeded_samples"]),
        failed_samples=int(job["failed_samples"]),
        skipped_samples=int(job["skipped_samples"]),
        created_at=str(job["created_at"]),
        started_at=job["started_at"],
        finished_at=job["finished_at"],
        restored_at=job.get("restored_at"),
        discarded_at=job.get("discarded_at"),
        error_code=job["error"],
        pinned=pinned,
    )


class WorkflowCountResolveRequest(BaseModel):
    """Record a reviewed count for one sample."""

    sample_id: int
    expected_version: int
    count: str
    source: str = "manual"


class WorkflowCountResolveBatchRequest(BaseModel):
    """Record several reviewed counts in one call."""

    items: list[WorkflowCountResolveRequest]


class WorkflowCountConfirmRequest(BaseModel):
    """Explicitly confirm that count review is complete."""

    confirmed: bool


class WorkflowPinRequest(BaseModel):
    """Pin/unpin a job for workspace retention."""

    pinned: bool = True


class WorkflowRestoreRequest(BaseModel):
    """Optional explicit operation identity for a new restore request."""

    operation_id: str | None = None


class WorkflowTokenReviewRequest(BaseModel):
    """Record one token budget review action for a sample."""

    sample_id: int
    action: Literal["edit", "recount", "rewrite_short", "apply"]
    expected_status: Literal["overflow", "edited", "recounted", "rewritten", "applied"]
    text: str | None = None


class WorkflowResourceImportRequest(BaseModel):
    """Request to preview or apply a resource import.

    The source file is addressed by root id + relative path so a client can never
    name an arbitrary absolute path on the server.
    """

    root_id: str
    relative_path: str
    resource_id: str
    category: str


class WorkflowResourceImportPreviewResponse(BaseModel):
    """Response for a resource import preview.

    ``rule_count`` is the category-neutral "usable rows" count: executable rules
    for a replacement index, tags for a classification snapshot. The snapshot
    specific counters stay optional so a replacement preview is unchanged.
    """

    valid: bool
    errors: list[str]
    warnings: list[str]
    rule_count: int
    action_counts: dict[str, int]
    passthrough_count: int
    fingerprint: str | None
    profile: str | None = None
    tag_count: int | None = None
    alias_count: int | None = None
    implication_count: int | None = None
    category_counts: dict[str, int] | None = None
