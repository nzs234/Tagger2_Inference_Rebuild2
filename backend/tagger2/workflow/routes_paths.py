"""Manual path-binding routes for the workflow API."""

import stat
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from .api_context import WorkflowRouteContext
from .api_models import (
    WorkflowPathBindingPreviewRequest,
    WorkflowPathBindingPreviewResponse,
    WorkflowPathBindingRequest,
    WorkflowPathBindingResponse,
    WorkflowPathRefResponse,
)
from ..security import PathNotAllowedError


def _manual_path(raw: str, field: str) -> Path:
    """Validate a user-entered absolute directory without exposing it."""

    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if "\x00" in text:
        raise ValueError(f"{field} contains NUL")
    if text.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError(f"{field} uses an unsupported device path")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    current = candidate
    while current != current.parent:
        try:
            attributes = getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0)
            if current.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise ValueError(f"{field} contains an unsupported reparse point")
        except FileNotFoundError:
            pass
        current = current.parent
    resolved = candidate.resolve(strict=False)
    if resolved.parent == resolved:
        raise ValueError(f"{field} cannot be a filesystem root")
    return resolved


def _path_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        try:
            second.relative_to(first)
            return True
        except ValueError:
            return False


def _register_manual_root(
    ctx: WorkflowRouteContext,
    path: Path,
    *,
    kind: Literal["input", "output"],
    writable: bool | None = None,
) -> Any:
    registrar = ctx.root_registrar
    if registrar is not None:
        return registrar(
            path,
            name="手动输入目录" if kind == "input" else "手动输出目录",
            kind=kind,
            writable=writable,
        )
    return ctx.allowlist.register(
        path,
        kind=kind,
        label="手动输入目录" if kind == "input" else "手动输出目录",
        writable=kind == "output" if writable is None else writable,
    )


def _bind_manual_path(
    ctx: WorkflowRouteContext,
    path: Path,
    *,
    kind: Literal["input", "output"],
    allow_register: bool,
    writable: bool | None = None,
) -> tuple[Any | None, str]:
    match = ctx.allowlist.find_root_for_path(
        path,
        kind=kind,
        writable=True if kind == "output" or writable is True else None,
    )
    if match is not None:
        return match
    if not allow_register:
        return None, ""
    root = _register_manual_root(ctx, path, kind=kind, writable=writable)
    return root, ""


def _preview_manual_paths(
    ctx: WorkflowRouteContext, request: WorkflowPathBindingPreviewRequest
) -> tuple[Path, Path | None, Any | None, Any | None, bool, list[str]]:
    source = _manual_path(request.source_path, "source_path")
    if not source.is_dir():
        raise ValueError("source_path does not exist")
    source_binding, _source_relative = _bind_manual_path(
        ctx,
        source,
        kind="input",
        allow_register=False,
        writable=request.work_mode == "in_place",
    )
    output: Path | None = None
    output_binding: Any | None = None
    output_create_required = False
    errors: list[str] = []
    if request.work_mode == "full_copy":
        if not request.output_path:
            errors.append("output_path_required")
        else:
            output = _manual_path(request.output_path, "output_path")
            if _path_overlap(source, output):
                errors.append("source_output_overlap")
            if output.exists() and not output.is_dir():
                errors.append("output_path_not_directory")
            if output.exists():
                output_binding, _output_relative = _bind_manual_path(
                    ctx, output, kind="output", allow_register=False
                )
                if output_binding is not None and not output_binding.writable:
                    errors.append("output_path_not_writable")
            else:
                output_create_required = True
                parent = output.parent
                if not parent.is_dir():
                    errors.append("output_parent_not_found")
    return source, output, source_binding, output_binding, output_create_required, errors


def register_path_binding_routes(router: APIRouter, ctx: WorkflowRouteContext) -> None:
    """Register the manual path-binding endpoints."""

    @router.post("/path-bindings/preview", response_model=WorkflowPathBindingPreviewResponse)
    async def preview_path_binding(
        request: WorkflowPathBindingPreviewRequest,
    ) -> WorkflowPathBindingPreviewResponse:
        """Validate manually entered paths without registering or creating them."""

        try:
            _source, _output, source_binding, output_binding, create_required, errors = (
                _preview_manual_paths(ctx, request)
            )
        except (TypeError, ValueError, OSError, PathNotAllowedError) as exc:
            message = str(exc)
            if isinstance(exc, (OSError, PathNotAllowedError)):
                message = "path_binding_io_error" if isinstance(exc, OSError) else "path_not_allowed"
            return WorkflowPathBindingPreviewResponse(
                status="not_applicable",
                source_bound=False,
                output_bound=False,
                output_create_required=False,
                errors=[message],
            )
        if errors:
            return WorkflowPathBindingPreviewResponse(
                status="not_applicable",
                source_bound=source_binding is not None,
                output_bound=output_binding is not None,
                output_create_required=create_required,
                errors=errors,
            )
        return WorkflowPathBindingPreviewResponse(
            status="create_required" if create_required else "ready",
            source_bound=source_binding is not None,
            output_bound=output_binding is not None,
            output_create_required=create_required,
        )

    @router.post("/path-bindings", response_model=WorkflowPathBindingResponse)
    async def bind_manual_paths(
        request: WorkflowPathBindingRequest,
    ) -> WorkflowPathBindingResponse:
        """Bind complete paths to the existing workflow path-reference contract."""

        try:
            source, output, _source_binding, _output_binding, create_required, errors = (
                _preview_manual_paths(ctx, request)
            )
            if errors:
                raise ValueError("; ".join(errors))
            if create_required and not request.create_output:
                raise ValueError("output_creation_confirmation_required")
            created_output_path: Path | None = None
            try:
                if output is not None and create_required:
                    output.mkdir(parents=True, exist_ok=False)
                    created_output_path = output
                source_root, source_relative = _bind_manual_path(
                    ctx,
                    source,
                    kind="input",
                    allow_register=True,
                    writable=request.work_mode == "in_place",
                )
                if source_root is None:
                    raise ValueError("source_path_binding_failed")
                output_ref: WorkflowPathRefResponse | None = None
                output_created = False
                if output is not None:
                    output_root, output_relative = _bind_manual_path(
                        ctx, output, kind="output", allow_register=True
                    )
                    if output_root is None:
                        raise ValueError("output_path_binding_failed")
                    output_ref = WorkflowPathRefResponse(
                        root_id=str(output_root.root_id), relative_path=output_relative
                    )
                    output_created = create_required
                return WorkflowPathBindingResponse(
                    status="ready" if request.work_mode == "full_copy" else "not_applicable",
                    source=WorkflowPathRefResponse(
                        root_id=str(source_root.root_id), relative_path=source_relative
                    ),
                    output=output_ref,
                    output_created=output_created,
                )
            except Exception:
                # Never remove a pre-existing directory.  If a later
                # registration/persistence step fails, clean up only the
                # empty directory this request created.
                if created_output_path is not None:
                    try:
                        if not any(created_output_path.iterdir()):
                            created_output_path.rmdir()
                    except OSError:
                        pass
                raise
        except (TypeError, ValueError, OSError, PathNotAllowedError) as exc:
            message = str(exc)
            if isinstance(exc, (OSError, PathNotAllowedError)):
                message = "path_binding_io_error" if isinstance(exc, OSError) else "path_not_allowed"
            raise HTTPException(
                status_code=400,
                detail={"code": "path_binding_failed", "message": message},
            ) from exc
