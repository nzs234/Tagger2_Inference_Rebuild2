"""Background job execution for the workflow API.

``_execute_job_async`` was the largest closure of the old router factory.  It
now takes :class:`WorkflowRouteContext` explicitly; the freeze/verify helpers
stay nested because they close over the per-run fingerprint dicts.
"""

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks

from .api_context import WorkflowRouteContext
from .api_shared import (
    _lifecycle,
    _public_error_code,
    _registered_model,
    _resolve_caption_model,
    _token_counter_for_config,
)
from .contracts import WorkflowJobConfigV2, utc_now
from .policy_config_parser import parse_policy_config
from ..security import PathNotAllowedError


logger = logging.getLogger(__name__)


def _record_job_failure(ctx: WorkflowRouteContext, job_id: str, exc: BaseException, trace: str) -> None:
    """Fail a job without leaking internals to the client.

    The job row keeps a short, stable code so the UI can branch on it. The
    exception text and traceback go to `<workspace>/job_error.log`, which an
    operator can read but the API never returns.
    """

    from .lifecycle import JobLifecycle

    database = ctx.database
    code = _public_error_code(exc)
    try:
        job = database.get_job(job_id)
        if job is not None:
            log_path = Path(job["workspace_path"]) / "job_error.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{utc_now()} {type(exc).__name__}: {exc}\n\n{trace}",
                encoding="utf-8",
            )
    except OSError:
        # Losing the diagnostic file must not mask the original failure or
        # leave the job stuck in `running`.
        pass

    try:
        JobLifecycle(database, job_id).transition("failed")
    except Exception as cleanup_exc:  # noqa: BLE001
        logger.warning(
            "workflow job %s: lifecycle transition to failed failed during failure recording: %s",
            job_id,
            cleanup_exc,
        )
    try:
        database.update_job_status(job_id, status="failed", error=code)
    except Exception as cleanup_exc:  # noqa: BLE001
        logger.warning(
            "workflow job %s: status update to failed failed during failure recording: %s",
            job_id,
            cleanup_exc,
        )
    try:
        with database.connection() as conn:
            conn.execute(
                "UPDATE workflow_stage_runs SET status = 'failed', finished_at = ? "
                "WHERE job_id = ? AND status IN ('pending', 'running')",
                (utc_now(), job_id),
            )
    except Exception as cleanup_exc:  # noqa: BLE001
        logger.warning(
            "workflow job %s: stage-run cleanup failed during failure recording: %s",
            job_id,
            cleanup_exc,
        )


async def _execute_job_async(ctx: WorkflowRouteContext, job_id: str) -> None:
    """Execute a workflow job in the background, updating status and seeding reviews."""
    import asyncio
    import traceback

    from .count_review import CountReviewStore
    from .lifecycle import JobLifecycle
    from .pipeline import run_offline_pipeline
    from .projection_checkpoint import load_projection_checkpoint
    from .token_budget_review import TokenBudgetReviewStore

    database = ctx.database
    allowlist = ctx.allowlist
    resource_catalog = ctx.resources
    inference_engine = ctx.engine
    model_registry = ctx.registry
    storage = ctx.storage

    try:
        job = database.get_job(job_id)
        if job is None:
            return
        initial_status = str(job["status"])
        if initial_status == "cancelling":
            lifecycle = JobLifecycle(database, job_id)
            lifecycle.transition("cancelled")
            return
        if initial_status == "pausing":
            lifecycle = JobLifecycle(database, job_id)
            lifecycle.transition("paused")
            return
        if initial_status in {"cancelled", "paused", "interrupted"}:
            return

        lifecycle = JobLifecycle(database, job_id)
        # A queued job is the normal path.  Keep a compatibility fallback
        # for jobs created against the old pending->running lifecycle.
        try:
            lifecycle.transition("running")
        except Exception:
            if str(job["status"]) != "running":
                raise

        config = WorkflowJobConfigV2.from_payload(json.loads(job["config_json"]))
        # Older rows can still contain the workflow placeholder model id.
        # Rebind them at the execution boundary so recovery uses the same
        # local model selection as a newly-created job.
        config = _resolve_caption_model(ctx, config, require_loaded=False)
        workspace = Path(job["workspace_path"])

        # Resolve physical paths from root references
        source_path = None
        output_path = None
        try:
            source_ref = allowlist.resolve(
                config.source_root.root_id, config.source_root.relative_path
            )
            source_path = Path(source_ref)
            if config.output_root:
                output_ref = allowlist.resolve(
                    config.output_root.root_id, config.output_root.relative_path
                )
                output_path = Path(output_ref)
            elif config.work_mode == "in_place":
                output_path = source_path
            else:
                raise ValueError("full_copy requires output_root")
        except PathNotAllowedError as exc:
            _record_job_failure(ctx, job_id, exc, str(exc))
            return

        # Freeze content-addressed resources before stage execution.  The
        # digest is persisted with the job so recovery can detect drift.
        resource_fingerprints: dict[str, str] = {}

        frozen_manifests: dict[str, dict[str, Any]] = {}

        def freeze_resource(resource_id: str) -> None:
            manifest = resource_catalog.get_manifest(resource_id)
            path = resource_catalog.get_resource_path(resource_id)
            if manifest is None:
                raise ValueError(f"resource digest verification failed: {resource_id}")
            if path is None:
                # Model-class blobs download on first use; job threads can
                # afford to wait so the pass simply takes longer on first run.
                from .resource_fetch import manager_for

                path = manager_for(resource_catalog).ensure(resource_id)
            resource_fingerprints[resource_id] = manifest.resource_fingerprint
            frozen_manifests[resource_id] = dict(manifest.__dict__)

        def verify_frozen_resources() -> None:
            for resource_id, fingerprint in resource_fingerprints.items():
                if resource_id.startswith("model:") or resource_id.startswith("provider:"):
                    continue
                current = resource_catalog.get_manifest(resource_id)
                path = resource_catalog.get_resource_path(resource_id)
                if (
                    current is None
                    or path is None
                    or current.resource_fingerprint != fingerprint
                ):
                    raise ValueError(f"resource hash drift detected: {resource_id}")

        def verify_host_model() -> None:
            model_id = str(config.caption.get("model_id") or "")
            if not config.caption.get("enabled") or not model_id:
                return
            record = _registered_model(ctx, model_id)
            weight_path = (
                Path(str(getattr(record, "weight_path", "")))
                if record is not None
                else None
            )
            expected = resource_fingerprints.get(f"model:{model_id}")
            if expected and (weight_path is None or not weight_path.is_file()):
                raise ValueError(f"caption model weight is unavailable: {model_id}")
            if expected and weight_path is not None and resource_catalog.fingerprint_file(weight_path) != expected:
                raise ValueError(f"caption model hash drift detected: {model_id}")

        def verify_all_frozen_resources() -> None:
            verify_frozen_resources()
            verify_host_model()

        for section_name, resource_key in (
            ("classify", "resource_id"),
            ("replace", "resource_id"),
            ("ocr", "resource_id"),
            ("token_budget", "tokenizer_resource_id"),
        ):
            section = getattr(config, section_name)
            if section.get("enabled"):
                resource_id = str(section.get(resource_key, ""))
                if resource_id:
                    freeze_resource(resource_id)

        # Caption and NL are backed by host registries rather than the
        # workflow catalog.  Freeze their stable identities alongside the
        # content-addressed resources so a recovery report explains which
        # local model/provider was used.  Host model files remain outside
        # the workflow catalog and are never copied into a job workspace.
        caption_model_id = str(config.caption.get("model_id") or "")
        if config.caption.get("enabled") and caption_model_id:
            model_record = _registered_model(ctx, caption_model_id)
            if model_record is not None:
                weight_path = Path(str(getattr(model_record, "weight_path", "")))
                if weight_path.is_file():
                    model_digest = resource_catalog.fingerprint_file(weight_path)
                    resource_fingerprints[f"model:{caption_model_id}"] = (
                        model_digest
                    )
                    frozen_manifests[f"model:{caption_model_id}"] = {
                        "model_id": caption_model_id,
                        "name": str(getattr(model_record, "name", "")),
                        "backend": str(getattr(getattr(model_record, "backend", None), "value", "")),
                        "weight_name": weight_path.name,
                        "size_bytes": weight_path.stat().st_size,
                        "weight_digest": model_digest,
                    }

        provider_id = str(config.nl.get("provider_id") or "")
        if config.nl.get("enabled") and provider_id:
            frozen_manifests[f"provider:{provider_id}"] = {
                "provider_id": provider_id,
                "model": config.nl.get("model"),
            }

        resume_checkpoint = load_projection_checkpoint(
            workspace,
            job_id=job_id,
            config_hash=config.config_hash(),
            resource_fingerprints=resource_fingerprints,
        )
        resume_cursor = (
            None
            if resume_checkpoint is None
            else str(resume_checkpoint["stage_cursor"])
        )

        # Wire up resources from catalog
        replacement_index_path = None
        if config.replace.get("enabled") and resume_cursor is None:
            # Use the replacement index specified in the job config
            replace_resource_id = str(config.replace.get("resource_id", ""))
            if not replace_resource_id:
                raise ValueError("replace stage is enabled but no resource_id was configured")
            replacement_index_path = resource_catalog.get_resource_path(replace_resource_id)
            if replacement_index_path is None:
                raise ValueError(
                    f"replace stage is enabled but the replacement index is not registered: {replace_resource_id}"
                )

        # Wire up tag predictor from inference engine
        tag_predictor = None
        if (
            resume_cursor is None
            and config.caption.get("enabled")
            and inference_engine is not None
            and model_registry is not None
        ):
            from .stages.caption import EngineTagPredictor
            model_id = str(config.caption.get("model_id", ""))
            if model_id and model_registry.get_model(model_id) is not None:
                threshold_mode = str(config.caption.get("threshold_mode", "model_default"))
                tag_predictor = EngineTagPredictor(
                    engine=inference_engine,
                    model_id=model_id,
                    threshold=None if threshold_mode == "model_default" else float(config.caption.get("threshold", 0.35)),
                    category_thresholds=config.caption.get("category_thresholds"),
                    use_category_thresholds=bool(config.caption.get("use_category_thresholds", True)),
                )

        # Wire up classification rules from the registered snapshot. A
        # missing or unreadable snapshot fails the job instead of letting
        # Classify silently produce nothing.
        classification_rules = None
        if config.classify.get("enabled") and resume_cursor is None:
            from .classify_snapshot import (
                ClassifySnapshotError,
                load_classification_rules,
            )

            classify_resource_id = str(config.classify.get("resource_id", ""))
            if not classify_resource_id:
                raise ValueError("classify stage is enabled but no resource_id was configured")
            classify_path = resource_catalog.get_resource_path(classify_resource_id)
            if classify_path is None:
                raise ValueError(
                    "classify stage is enabled but the classification snapshot"
                    f" is not registered: {classify_resource_id}"
                )
            try:
                classification_rules = load_classification_rules(classify_path)
            except ClassifySnapshotError as exc:
                raise ValueError(f"failed to load classification rules: {exc}") from exc
            if classification_rules.profile != config.profile:
                raise ValueError(
                    "classification snapshot profile"
                    f" {classification_rules.profile!r} does not match the job profile"
                    f" {config.profile!r}"
                )

        # OCR runs in an isolated runtime. Building the engine is what
        # detects a missing runtime, and the stage turns that into a
        # non-blocking warning rather than failing the job.
        ocr_engine = None
        if config.ocr.get("enabled") and resume_cursor is None:
            from .ocr import load_ocr_engine_from_resource

            try:
                ocr_resource_id = str(config.ocr.get("resource_id") or "")
                ocr_resource_path = resource_catalog.get_resource_path(ocr_resource_id)
                if ocr_resource_path is None:
                    raise RuntimeError(f"OCR resource is unavailable: {ocr_resource_id}")
                ocr_engine = await asyncio.to_thread(
                    load_ocr_engine_from_resource, ocr_resource_path
                )
            except RuntimeError as exc:
                # Preflight normally catches this.  Re-checking at the
                # execution boundary prevents a cache/runtime drift race
                # from degrading into a warning-only OCR skip.
                raise ValueError(f"OCR resource failed execution probe: {exc}") from exc

        # Policy config converted to dataclass if enabled.  Resolved before
        # the NL adapter below so an invalid policy fails before an adapter
        # (and its dedicated event loop) exists and must be cleaned up.
        policy_config_arg = None
        if config.policy.get("enabled") and resume_cursor != "token_review":
            try:
                policy_config_arg = parse_policy_config(config.policy)
            except Exception as exc:
                raise ValueError(f"invalid policy configuration: {exc}") from exc

        # Resolve the frozen tokenizer resource unless a test explicitly
        # injected a deterministic counter.
        token_counter_arg = _token_counter_for_config(ctx, config)

        # NL client if provider configured
        nl_client = None
        if config.nl.get("enabled") and resume_cursor is None:
            provider_id = str(config.nl.get("provider_id", ""))
            if provider_id:
                from tagger2.providers import ProviderConfig, create_provider

                from .nl_adapter import ProviderNlAdapter

                assert storage is not None  # Type narrowing
                # Get provider profile from storage
                stored_profile = storage.get_provider_profile(provider_id)
                if stored_profile is None:
                    raise ValueError(f"Provider {provider_id} not found")
                if not bool(stored_profile.get("enabled", True)):
                    raise ValueError(f"Provider {provider_id} is disabled")

                # Build provider config
                cfg = dict(stored_profile.get("config") or {})
                cfg.update({
                    "id": provider_id,
                    "name": stored_profile.get("name"),
                    "kind": stored_profile.get("kind"),
                    "base_url": stored_profile.get("base_url")
                })
                cfg["model"] = cfg.pop("primary_model", cfg.get("model", ""))
                cfg["backup_model"] = cfg.pop("fallback_model", cfg.get("backup_model"))
                cfg["max_output_tokens"] = cfg.pop("max_tokens", cfg.get("max_output_tokens", 8192))

                # Get API keys from secret store
                secret_ref = stored_profile.get("secret_ref")
                keys = []
                if secret_ref:
                    try:
                        from tagger2.secrets import CompositeSecretStore
                        secret_store = CompositeSecretStore()
                        raw_keys = secret_store.get(secret_ref)
                        if raw_keys:
                            keys = [k.strip() for k in raw_keys.replace(",", "\n").split("\n") if k.strip()]
                    except Exception as secret_exc:  # noqa: BLE001
                        logger.warning(
                            "workflow job %s: provider secret %s unavailable, "
                            "continuing without API keys: %s",
                            job_id,
                            secret_ref,
                            secret_exc,
                        )
                cfg["api_keys"] = tuple(keys)

                # Create provider instance
                provider = create_provider(ProviderConfig.from_mapping(cfg))
                nl_client = ProviderNlAdapter(
                    provider,
                    model=(str(config.nl.get("model")) if config.nl.get("model") else None),
                )

        try:
            report = await asyncio.to_thread(
                run_offline_pipeline,
                config,
                source_root=source_path,
                output_root=output_path,
                workspace=workspace,
                replacement_index_path=replacement_index_path,
                resource_fingerprints=resource_fingerprints,
                resource_manifests=frozen_manifests,
                tag_predictor=tag_predictor,
                classification_rules=classification_rules,
                policy_config=policy_config_arg,
                token_counter=token_counter_arg,
                ocr_engine=ocr_engine,
                nl_client=nl_client,
                database=database,
                job_id=job_id,
                resource_verifier=verify_all_frozen_resources,
            )
        finally:
            # The adapter owns a dedicated event loop plus the provider's
            # pooled connections; it must drain even when the pipeline
            # raises, or the loop and client leak per job run.
            if nl_client is not None:
                nl_client.close()

        # Persist the stage report so the UI can read per-stage counters
        # (OCR included) without the pipeline holding process state.
        try:
            (workspace / "pipeline_report.json").write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            # A missing report must not fail an otherwise successful run.
            pass

        # Check if human review is needed before marking as completed
        latest = database.get_job(job_id)
        if latest is None:
            return
        latest_status = str(latest["status"])
        if latest_status == "cancelling":
            lifecycle.transition("cancelled")
            return
        if latest_status == "pausing":
            lifecycle.transition("paused")
            return
        if latest_status in {"cancelled", "paused", "interrupted"}:
            return
        count_store = CountReviewStore(database, job_id)
        token_store_check = TokenBudgetReviewStore(database, job_id)

        pending_count = count_store.pending_count()
        pending_token = token_store_check.unresolved_count()

        has_blocking = any(
            issue.blocking for issue in report.issues
        )

        # Determine final status based on blocking issues and pending reviews
        if has_blocking:
            final_status = "failed"
        elif bool(config.count_review.get("enabled")) and pending_count > 0:
            final_status = "waiting_count_review"
        elif bool(config.token_budget.get("enabled")) and pending_token > 0:
            final_status = "waiting_token_review"
        else:
            final_status = "completed"

        lifecycle.transition(final_status)

        # Issues are already persisted in the issues table via report

    except Exception as exc:  # noqa: BLE001
        # The traceback is operator data, not client data: write it to the
        # job workspace and store only a stable code on the job row.
        _record_job_failure(ctx, job_id, exc, traceback.format_exc())


async def _schedule_job(ctx: WorkflowRouteContext, job_id: str, background_tasks: BackgroundTasks) -> str:
    """Queue one execution, preserving idempotency for UI retries.

    Unwired in the current route set (kept from the pre-split factory), but
    retained so the idempotent queue entry point survives the refactor.
    """

    from .lifecycle import LifecycleError

    lifecycle, job = _lifecycle(ctx, job_id)
    status = str(job["status"])
    if status in {"completed", "cancelled", "failed", "running", "queued"}:
        if status == "queued":
            background_tasks.add_task(_execute_job_async, ctx, job_id)
        return status
    try:
        queued = lifecycle.transition("queued")
    except LifecycleError:
        # Compatibility with a database created by the pre-queued schema;
        # the current lifecycle implementation accepts ``queued``.
        queued = lifecycle.transition("running")
    background_tasks.add_task(_execute_job_async, ctx, job_id)
    return queued
