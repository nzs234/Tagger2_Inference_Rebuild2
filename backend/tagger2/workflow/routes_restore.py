"""Restore and discard routes for the workflow API."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from .api_context import WorkflowRouteContext
from .api_models import WorkflowRestoreRequest
from .api_shared import _lifecycle
from ..security import PathNotAllowedError


logger = logging.getLogger(__name__)


async def _restore_job(
    ctx: WorkflowRouteContext,
    job_id: str,
    request: WorkflowRestoreRequest | None,
) -> dict[str, Any]:
    """Restore original annotations from a backup archive (body of the route)."""

    from .commit import CommitError, restore_annotation_backup

    database = ctx.database
    allowlist = ctx.allowlist

    _lifecycle_obj, job = _lifecycle(ctx, job_id)
    status = str(job["status"])

    # Only allow restore from terminal or cancelled states
    if status not in ("completed", "failed", "cancelled", "interrupted", "rollback_required"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_state_for_restore",
                "message": f"Cannot restore from state: {status}"
            }
        )

    if str(job["work_mode"]) != "in_place":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "restore_not_applicable",
                "message": "restore is only available for in_place jobs",
            },
        )

    if job.get("discarded_at"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "job_discarded",
                "message": "the job workspace has been discarded",
            },
        )

    workspace = Path(str(job["workspace_path"]))
    backup_zip = workspace / "backup" / "annotations.zip"
    if not backup_zip.exists():
        # Compatibility with archives created by the first workflow
        # vertical.  New commits always use the nested artifact path.
        legacy = workspace / "backup.zip"
        backup_zip = legacy if legacy.exists() else backup_zip

    root_id = str(job["source_root_id"])
    operation_id = str(request.operation_id).strip() if request and request.operation_id else ""
    if operation_id and (
        len(operation_id) > 128
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
            for character in operation_id
        )
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_restore_operation",
                "message": "operation_id contains unsupported characters",
            },
        )
    restore_operation_key = f"restore:{job_id}:{operation_id or backup_zip.name}"
    prior_restore = database.get_operation(
        job_id,
        "restore",
        restore_operation_key,
    )
    if prior_restore is not None and prior_restore.get("status") == "completed":
        payload = dict(prior_restore.get("payload") or {})
        return {
            "job_id": job_id,
            "root_id": root_id,
            "restored_files": int(payload.get("restored_files", 0)),
            "replayed": True,
        }

    if not backup_zip.exists():
        raise HTTPException(
            status_code=404,
            detail={
                "code": "backup_not_found",
                "message": "Backup archive not found for this job"
            }
        )

    # Restore is intentionally limited to in_place jobs.  A full-copy
    # result has an independent output dataset and is never allowed to
    # mutate the source during restore.
    if not root_id:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "missing_dataset_root",
                "message": "Job has no dataset root to restore into",
            },
        )

    try:
        payload = json.loads(str(job["config_json"]))
        source = payload.get("source_root", {}) if isinstance(payload, dict) else {}
        relative_path = str(source.get("relative_path", "")) if isinstance(source, dict) else ""
        dataset_root = Path(allowlist.resolve(root_id, relative_path, must_exist=True, expect="dir"))
    except PathNotAllowedError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_dataset_root",
                "message": "Dataset root is no longer registered",
            },
        ) from exc

    restore_started = False
    def _mark_restore_failed() -> None:
        """Leave a failed restore retryable without retaining its lock."""

        try:
            database.update_job_status(
                job_id,
                "rollback_required",
                error="restore_failed",
                expected_status="restoring",
            )
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning(
                "workflow job %s: restore failure status update failed: %s",
                job_id,
                cleanup_exc,
            )
        try:
            database.record_operation(
                job_id,
                "restore",
                idempotency_key=restore_operation_key,
                status="failed",
                payload={"code": "restore_failed"},
            )
            database.record_event(job_id, "restore_failed", payload={"code": "restore_failed"})
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning(
                "workflow job %s: restore failure operation record failed: %s",
                job_id,
                cleanup_exc,
            )
        try:
            database.release_dataset_locks(job_id)
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning(
                "workflow job %s: restore failure lock release failed: %s",
                job_id,
                cleanup_exc,
            )

    try:
        # Serialize Restore against new starts. The terminal job released
        # its execution lock, so reacquire the exact source scope at the
        # operation boundary before touching dataset files.
        if not database.start_job(job_id, expected_status=status):
            raise HTTPException(
                status_code=409,
                detail={"code": "restore_locked", "message": "dataset is busy"},
            )
        restore_started = True
        if not database.update_job_status(job_id, "restoring", expected_status="queued"):
            database.release_dataset_locks(job_id)
            raise HTTPException(
                status_code=409,
                detail={"code": "restore_state_race", "message": "job changed during restore"},
            )
        restored_count = await asyncio.to_thread(
            restore_annotation_backup, backup_zip, dataset_root
        )
        database.record_operation(
            job_id,
            "restore",
            idempotency_key=restore_operation_key,
            payload={"restored_files": restored_count},
        )
        database.record_event(job_id, "restore_completed", payload={"restored_files": restored_count})
        database.mark_job_restored(job_id)
        if not database.update_job_status(job_id, "completed", expected_status="restoring"):
            raise CommitError("restore state changed before completion")
        # ``completed`` normally releases these rows in the database CAS;
        # keep this explicit so the contract survives older DB adapters.
        database.release_dataset_locks(job_id)
        return {
            "job_id": job_id,
            "root_id": root_id,
            "restored_files": restored_count,
            "replayed": False,
        }
    except CommitError as exc:
        if restore_started:
            _mark_restore_failed()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "restore_failed",
                "message": "restore failed; recovery is required",
            }
        ) from exc
    except HTTPException:
        # Only release a lock acquired by this request.  If start_job lost
        # the CAS race, another restore/recovery may own the job's lock;
        # releasing it here would silently un-serialize that operation.
        if restore_started:
            database.release_dataset_locks(job_id)
        raise
    except Exception as exc:  # noqa: BLE001
        if restore_started:
            _mark_restore_failed()
        raise HTTPException(
            status_code=500,
            detail={
                "code": "restore_failed",
                "message": "restore failed; recovery is required",
            },
        ) from exc


async def _discard_job(ctx: WorkflowRouteContext, job_id: str) -> dict[str, Any]:
    """Discard a job's workspace and intermediate files (body of the route)."""

    import shutil

    database = ctx.database

    _lifecycle_obj, job = _lifecycle(ctx, job_id)
    status = str(job["status"])
    workspace = Path(str(job["workspace_path"]))

    if job.get("discarded_at"):
        if not workspace.exists():
            return {
                "job_id": job_id,
                "discarded": False,
                "message": "Workspace already removed",
            }
        try:
            # A process may have stopped after persisting the marker but
            # before removing the directory.  Repeating the same discard
            # completes that cleanup without changing job history.
            # Workspace removal can be a multi-gigabyte recursive delete;
            # keep it off the event loop.
            await asyncio.to_thread(shutil.rmtree, workspace)
            database.record_operation(
                job_id,
                "discard",
                idempotency_key=f"discard:{job_id}",
                payload={"workspace": workspace.name},
            )
            database.record_event(job_id, "workspace_discarded")
            return {
                "job_id": job_id,
                "discarded": True,
                "message": "Workspace removed",
            }
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "discard_failed",
                    "message": "failed to discard the job workspace",
                },
            ) from exc

    if database.is_job_pinned(job_id):
        raise HTTPException(
            status_code=409,
            detail={"code": "job_pinned", "message": "unpin the job before discarding its workspace"},
        )

    # Only allow discard from terminal states
    if status not in ("completed", "failed", "cancelled", "interrupted", "rollback_required"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_state_for_discard",
                "message": f"Cannot discard job in state: {status}"
            }
        )

    if not workspace.exists():
        if not database.mark_job_discarded(job_id, expected_status=status):
            refreshed = database.get_job(job_id)
            if refreshed is None or not refreshed.get("discarded_at"):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "discard_state_race",
                        "message": "job changed while discarding its workspace",
                    },
                )
        database.record_operation(
            job_id,
            "discard",
            idempotency_key=f"discard:{job_id}",
            payload={"workspace": workspace.name},
        )
        database.record_event(job_id, "workspace_discarded")
        return {
            "job_id": job_id,
            "discarded": False,
            "message": "Workspace already removed"
        }

    try:
        if not database.mark_job_discarded(job_id, expected_status=status):
            raise RuntimeError("discard state changed before workspace removal")
        # The durable marker and dataset-lock release happen first.  If
        # power is lost during removal, the idempotent branch above will
        # finish deleting the marked workspace on the next request.
        # Workspace removal can be a multi-gigabyte recursive delete;
        # keep it off the event loop.
        await asyncio.to_thread(shutil.rmtree, workspace)
        database.record_operation(
            job_id,
            "discard",
            idempotency_key=f"discard:{job_id}",
            payload={"workspace": workspace.name},
        )
        database.record_event(job_id, "workspace_discarded")

        return {
            "job_id": job_id,
            "discarded": True,
            "removed_path": str(workspace.name),  # Only return relative name
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "discard_failed",
                "message": "failed to discard the job workspace",
            }
        ) from exc


def register_restore_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the restore/discard endpoints."""

    @router.post("/jobs/{job_id}/restore")
    async def restore_job(
        job_id: str,
        request: WorkflowRestoreRequest | None = None,
    ) -> dict[str, Any]:
        """Restore original annotations from backup archive.

        This operation restores the dataset to its pre-workflow state using
        the backup created during job initialization. The job must be in a
        terminal state (completed or failed) or explicitly cancelled.
        """
        return await _restore_job(ctx, job_id, request)

    @router.post("/jobs/{job_id}/discard")
    async def discard_job(job_id: str) -> dict[str, Any]:
        """Discard a job's workspace and intermediate files.

        This permanently removes the job's workspace directory including all
        intermediate files, staged outputs, and backups. The job record remains
        in the database for audit purposes but cannot be restored or resumed.

        The job must be in a terminal state (completed, failed, cancelled).
        """
        return await _discard_job(ctx, job_id)
