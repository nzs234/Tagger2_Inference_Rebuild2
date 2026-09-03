"""Pure helpers shared across the workflow API route modules.

The public-error-code mapping, the caption-model resolution and the job
lifecycle lookup are used by more than one route group, so they live here as
module-level functions taking :class:`WorkflowRouteContext` explicitly instead
of closing over factory state.
"""

import json
from typing import Any

from fastapi import HTTPException

from .api_context import WorkflowRouteContext
from .contracts import WorkflowJobConfigV2
from ..security import PathNotAllowedError
from .preflight import WorkflowPreflightError


def _build_public_error_codes() -> dict[type, str]:
    """Stable client-facing codes for the failures a run can raise."""

    from .commit import CommitError
    from .pipeline import PipelineError
    from .projection_checkpoint import ProjectionCheckpointError
    from .stages.policy import PolicyError
    from .stages.replacement import ReplacementError
    from .stages.token_budget import TokenBudgetError

    return {
        CommitError: "commit_failed",
        PipelineError: "pipeline_failed",
        ProjectionCheckpointError: "review_checkpoint_invalid",
        PolicyError: "policy_failed",
        ReplacementError: "replacement_failed",
        TokenBudgetError: "token_budget_failed",
        PathNotAllowedError: "path_not_allowed",
        FileNotFoundError: "input_unavailable",
        PermissionError: "permission_denied",
        OSError: "io_error",
    }


_PUBLIC_ERROR_CODES: dict[type, str] | None = None

# This was the old workflow-only placeholder.  It is not a model registry ID
# and must never be passed to LocalInferenceEngine.  Existing V1 snapshots may
# still contain it, so the API treats it as an instruction to use the host
# application's currently loaded local model.
_AUTO_CAPTION_MODEL_IDS = frozenset(
    {
        "caption-e621-eva02-large-full-v1",
    }
)


def _public_error_code(exc: BaseException) -> str:
    """Map an exception onto a stable public code via its MRO.

    Walking the MRO means a subclass of a mapped error still reports the
    specific code, and anything unmapped degrades to `internal_error` rather
    than exposing the exception text.
    """

    global _PUBLIC_ERROR_CODES
    if _PUBLIC_ERROR_CODES is None:
        _PUBLIC_ERROR_CODES = _build_public_error_codes()
    for klass in type(exc).__mro__:
        code = _PUBLIC_ERROR_CODES.get(klass)
        if code is not None:
            return code
    return "internal_error"


def _preflight_http_error(error: WorkflowPreflightError) -> HTTPException:
    """Translate a preflight failure into a client error.

    The reasons are folded into the message and ``fields`` because the
    application error envelope only forwards code, message and fields.
    """

    errors = [str(item) for item in error.details.get("errors", [])]
    message = error.message if not errors else f"{error.message}: " + "; ".join(errors)
    return HTTPException(
        status_code=400,
        detail={
            "code": error.code,
            "message": message,
            "fields": {"config": errors} if errors else None,
        },
    )


def _registered_model(ctx: WorkflowRouteContext, model_id: str) -> Any | None:
    """Resolve a model through the host registry without leaking paths."""

    model_registry = ctx.registry
    if model_registry is None:
        return None
    try:
        record = model_registry.get_model(model_id)
    except (AttributeError, KeyError, LookupError, ValueError, RuntimeError):
        record = None
    if record is not None:
        return record

    # Workbench/Batch accept the model display name and directory name as
    # selectors in addition to the opaque registry id.  Preserve that
    # compatibility for workflow callers, but persist the canonical id.
    try:
        records = list(model_registry.list())
    except (AttributeError, TypeError):
        return None
    folded = model_id.casefold()
    matches = [
        item
        for item in records
        if folded
        in {
            str(getattr(item, "model_id", "")).casefold(),
            str(getattr(item, "name", "")).casefold(),
            getattr(getattr(item, "path", None), "name", "").casefold(),
        }
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_caption_model(
    ctx: WorkflowRouteContext,
    config: WorkflowJobConfigV2,
    *,
    require_loaded: bool = True,
) -> WorkflowJobConfigV2:
    """Bind Caption to the existing local inference model configuration.

    The single-image and batch surfaces already establish the source of
    truth for local model loading, thresholds, preprocessing and adapters:
    ``ModelRegistry`` plus ``LocalInferenceEngine``.  Dataset Workflow
    must not invent a second model id or silently use a different default.
    A legacy placeholder (or omitted model id, which is normalized to that
    placeholder by the V1 reader) selects the first model currently loaded
    by the host runtime.  The canonical opaque id is persisted in the job
    snapshot, so a resumed job is deterministic.
    """

    model_registry = ctx.registry
    inference_engine = ctx.engine

    if not bool(config.caption.get("enabled")):
        return config
    if model_registry is None or inference_engine is None:
        raise ValueError(
            "caption stage requires the host local model runtime; "
            "load a local model from the Models page first"
        )

    requested = str(config.caption.get("model_id") or "").strip()
    record: Any | None = None
    if requested and requested not in _AUTO_CAPTION_MODEL_IDS:
        record = _registered_model(ctx, requested)
        if record is None:
            raise ValueError(f"caption local model is not registered: {requested}")
        loaded_id_set = {
            str(value)
            for value in getattr(inference_engine, "loaded_model_ids", ())
            if str(value)
        }
        if require_loaded and not bool(getattr(record, "loaded", False)) and str(
            getattr(record, "model_id", "")
        ) not in loaded_id_set:
            raise ValueError(
                "caption local model is not loaded; "
                "load it from the Models page before creating the job"
            )
    else:
        loaded_ids: list[str]
        try:
            loaded_ids = [
                str(value)
                for value in getattr(inference_engine, "loaded_model_ids", ())
                if str(value)
            ]
        except (TypeError, AttributeError):
            loaded_ids = []
        for model_id in loaded_ids:
            if (candidate := _registered_model(ctx, model_id)) is not None:
                record = candidate
                break

        # Some test/runtime adapters expose only ModelRecord.loaded.  It
        # is still the same host registry state, so accept that form too.
        if record is None:
            try:
                records = list(model_registry.list())
            except (AttributeError, TypeError):
                records = []
            record = next(
                (
                    item
                    for item in records
                    if bool(getattr(item, "loaded", False))
                    and str(getattr(item, "model_id", ""))
                ),
                None,
            )
        if record is None:
            raise ValueError(
                "caption stage requires a loaded local model; "
                "load one from the Models page before creating the job"
            )

    canonical_id = str(getattr(record, "model_id", "")).strip()
    if not canonical_id:
        raise ValueError("caption local model has no canonical registry id")
    values = config.to_dict()
    caption = dict(config.caption)
    caption["model_id"] = canonical_id
    values["caption"] = caption
    return WorkflowJobConfigV2.from_payload(values)


def _token_counter_for_config(ctx: WorkflowRouteContext, config: WorkflowJobConfigV2):
    """Resolve the immutable tokenizer resource for one job.

    Tests may inject a counter explicitly, but production jobs always load
    the content-addressed tokenizer from the resource catalog.  There is
    intentionally no repository/model-name fallback and no network lookup.
    """

    token_counter = ctx.token_counter
    resource_catalog = ctx.resources

    if not bool(config.token_budget.get("enabled")):
        return None
    if token_counter is not None:
        return token_counter
    resource_id = str(config.token_budget.get("tokenizer_resource_id") or "")
    if not resource_id:
        raise ValueError("token budget is enabled but no tokenizer resource is configured")
    manifest = resource_catalog.get_manifest(resource_id)
    path = resource_catalog.get_resource_path(resource_id)
    if manifest is None or manifest.category != "tokenizer":
        raise ValueError(f"tokenizer resource is unavailable: {resource_id}")
    if path is None:
        # Tokenizer packs are not packaged either; fetch on first use.
        from .resource_fetch import manager_for

        path = manager_for(resource_catalog).ensure(resource_id)
    from .tokenizer_resource import load_tokenizer_counter

    return load_tokenizer_counter(path)


def _token_counter_for_job(ctx: WorkflowRouteContext, job_id: str):
    database = ctx.database
    job = database.get_job(job_id)
    if job is None:
        return None
    try:
        config = WorkflowJobConfigV2.from_payload(json.loads(str(job["config_json"])))
        return _token_counter_for_config(ctx, config)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _lifecycle(ctx: WorkflowRouteContext, job_id: str):
    """Fetch a job with its lifecycle, or 404 with the stable error envelope."""

    from .lifecycle import JobLifecycle

    database = ctx.database
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
        )
    return JobLifecycle(database, job_id), job


def _count_store(ctx: WorkflowRouteContext, job_id: str):
    from .count_review import CountReviewStore

    database = ctx.database
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
        )
    return CountReviewStore(database, job_id)


def _token_store(ctx: WorkflowRouteContext, job_id: str):
    from .token_budget_review import TokenBudgetReviewStore

    database = ctx.database
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "job_not_found", "message": f"unknown job: {job_id}"},
        )
    return TokenBudgetReviewStore(database, job_id)
