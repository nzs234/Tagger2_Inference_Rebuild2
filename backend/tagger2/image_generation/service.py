"""Async durable image-generation service."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..artifacts import atomic_write_bytes
from ..config import AppConfig
from ..providers import APIKeyPool, ProviderConfig, ProviderError
from ..providers.image import PreparedImage, prepare_image
from ..security import SecurityError, UploadValidationError, validate_provider_url
from ..secrets import CompositeSecretStore
from ..storage import config_digest
from .capabilities import (
    OPENAI_CUSTOM_SIZE_SENTINEL,
    SIZE_PATTERN,
    ImageCapability,
    capability_for_style,
    capability_from_public,
    capability_object,
)
from .client import ImageGenerationClient, ImageRequest
from .contracts import ImageJobConfig
from .storage import ImageGenerationStorage, JOB_STATES, TERMINAL_STATES, hash_bytes


class ImageGenerationServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class ImageGenerationService:
    def __init__(
        self,
        settings: AppConfig,
        *,
        provider_profiles: Callable[[str], Mapping[str, Any] | None],
        secrets: CompositeSecretStore,
    ) -> None:
        data_dir = settings.data_dir or settings.project_root / "data"
        self.root = data_dir / "image_generation"
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.storage = ImageGenerationStorage(self.root / "image_generation.sqlite3")
        self.settings = settings
        self.provider_profiles = provider_profiles
        self.secrets = secrets
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._key_pools: dict[str, APIKeyPool] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._closing = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._closing = False
        self.storage.recover_interrupted()
        self._cleanup_incomplete_creates()
        for job_id in self.storage.job_ids("deleting"):
            try:
                await self.delete_job(job_id)
            except ImageGenerationServiceError:
                # Keep a deleting tombstone for an operator retry when the
                # previous process could not remove a locked file.
                continue
        for state in ("interrupted", "queued"):
            for job_id in self.storage.job_ids(state):
                await self.start_job(job_id)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._key_pools.clear()
        self.storage.close()

    def capabilities(self, *, provider_id: str | None = None, model: str | None = None) -> dict[str, Any]:
        if provider_id:
            profile = self.provider_profiles(provider_id)
            if profile is None:
                raise ImageGenerationServiceError("provider 不存在", code="image_provider_not_found", status_code=404)
            config = dict(profile.get("config") or {})
            selected_model = (model or config.get("primary_model") or "").strip()
            protocol = str(config.get("protocol") or "openai")
            capability = capability_object(
                kind=str(profile.get("kind") or "custom"),
                protocol=protocol,
                model=selected_model,
                configured_family=config.get("image_family"),
            )
            style = self._effective_style(capability, config)
            public = capability_for_style(capability, style).public(
                model=selected_model,
                provider_id=provider_id,
            )
            public["api_style"] = style
            return public
        from .capabilities import capability_catalog

        return {"schema_version": "image-capabilities-v1", "families": capability_catalog()}

    async def create_job(self, config: ImageJobConfig, uploads: Sequence[tuple[str, bytes, str]]) -> dict[str, Any]:
        profile = self.provider_profiles(config.provider_id)
        if profile is None:
            raise ImageGenerationServiceError("provider 不存在", code="image_provider_not_found", status_code=404)
        if not bool(profile.get("enabled", True)):
            raise ImageGenerationServiceError("provider 已禁用", code="image_provider_disabled", status_code=409)
        profile_config = dict(profile.get("config") or {})
        if profile_config.get("image_enabled") is False:
            raise ImageGenerationServiceError("provider 未启用图像生成", code="image_generation_disabled", status_code=409)
        protocol = str(profile_config.get("protocol") or ("gemini" if profile.get("kind") in {"gemini", "antigravity"} else "openai"))
        capability = capability_object(
            kind=str(profile.get("kind") or "custom"),
            protocol=protocol,
            model=config.model,
            configured_family=profile_config.get("image_family"),
        )
        image_style = self._effective_style(capability, profile_config)
        capability = capability_for_style(capability, image_style)
        self._validate_config(config, capability, len(uploads), image_style)
        provider_snapshot = self._provider_snapshot(
            profile,
            profile_config,
            protocol=protocol,
            style=image_style,
        )
        prepared = await self._prepare_uploads(uploads)
        job_id = uuid.uuid4().hex
        job_dir = self.jobs_root / job_id
        creating_marker = job_dir / ".creating"
        references: list[dict[str, Any]] = []
        try:
            atomic_write_bytes(creating_marker, b"image-generation-job\n")
            for index, image in enumerate(prepared):
                relative = Path("references") / f"reference-{index:03d}.jpg"
                destination = self._safe_job_path(job_id, relative)
                atomic_write_bytes(destination, image.data)
                references.append({
                    "ordinal": index,
                    "relative_path": relative.as_posix(),
                    "sha256": image.sha256,
                    "mime_type": image.mime_type,
                    "width": image.width,
                    "height": image.height,
                    "size_bytes": len(image.data),
                })
            normalized = self._normalized_config(
                config,
                capability,
                provider_snapshot=provider_snapshot,
            )
            attempt_count = config.n if config.multi_image_strategy == "parallel" else 1
            record = self.storage.create_job(
                job_id=job_id,
                provider_id=config.provider_id,
                model=config.model,
                family=capability.family,
                operation=config.operation,
                requested_count=config.n,
                config=normalized,
                attempts=attempt_count,
                references=references,
            )
            try:
                creating_marker.unlink(missing_ok=True)
            except OSError:
                # The marker is only startup cleanup metadata.  A committed
                # database row remains authoritative if antivirus/file locks
                # briefly prevent marker removal on Windows.
                pass
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        await self.start_job(job_id)
        return self.public_job(record)

    async def start_job(self, job_id: str) -> None:
        async with self._lock:
            current = self._tasks.get(job_id)
            if current is not None and not current.done():
                return
            if self.storage.get_job(job_id) is None:
                raise ImageGenerationServiceError("任务不存在", code="image_job_not_found", status_code=404)
            record = self.storage.get_job(job_id)
            if record is not None and str(record["state"]) not in {"queued", "interrupted", "running"}:
                raise ImageGenerationServiceError(
                    "任务当前不可启动",
                    code="image_job_not_startable",
                    status_code=409,
                )
            event = self._cancel_events.setdefault(job_id, asyncio.Event())
            event.clear()
            task = asyncio.create_task(self._run(job_id), name=f"tagger2-image-{job_id}")
            self._tasks[job_id] = task
            def cleanup(done: asyncio.Task[None]) -> None:
                if self._tasks.get(job_id) is done:
                    self._tasks.pop(job_id, None)
                    self._key_pools.pop(job_id, None)

            task.add_done_callback(cleanup)

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        record = self.storage.get_job(job_id)
        if record is None:
            raise ImageGenerationServiceError("任务不存在", code="image_job_not_found", status_code=404)
        if str(record["state"]) in TERMINAL_STATES:
            return self.public_job(record)
        self._cancel_events.setdefault(job_id, asyncio.Event()).set()
        record = self.storage.request_cancel(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        latest = self.storage.get_job(job_id)
        if latest is not None and str(latest["state"]) not in TERMINAL_STATES and str(latest["state"]) != "deleting":
            record = self.storage.finalize_job(job_id, state="cancelled")
        else:
            record = latest or record
        return self.public_job(record)

    async def retry_job(self, job_id: str) -> dict[str, Any]:
        record = self.storage.get_job(job_id)
        if record is None:
            raise ImageGenerationServiceError("任务不存在", code="image_job_not_found", status_code=404)
        if str(record["state"]) not in {"failed", "partial", "cancelled", "interrupted"}:
            raise ImageGenerationServiceError("任务当前不可重试", code="image_job_not_retryable", status_code=409)
        count = self.storage.reset_retryable(job_id)
        if not count and str(record["state"]) == "interrupted":
            await self.start_job(job_id)
        elif count:
            await self.start_job(job_id)
        latest = self.storage.get_job(job_id) or record
        return self.public_job(latest)

    async def delete_job(self, job_id: str) -> None:
        record = self.storage.get_job(job_id)
        if record is None:
            return
        if str(record["state"]) not in TERMINAL_STATES and str(record["state"]) != "deleting":
            raise ImageGenerationServiceError("任务尚未结束", code="image_job_not_terminal", status_code=409)
        self._tasks.pop(job_id, None)
        job_dir = self.jobs_root / job_id
        if not job_dir.resolve(strict=False).is_relative_to(self.jobs_root.resolve(strict=False)):
            raise ImageGenerationServiceError("任务目录无效", code="image_job_path_invalid", status_code=400)
        try:
            self.storage.mark_deleting(job_id)
            if job_dir.exists():
                # A job directory holds every generated image of the job, so
                # the recursive delete can be long-running; keep it off the
                # event loop.
                await asyncio.to_thread(shutil.rmtree, job_dir)
            self.storage.delete_job(job_id)
        except (OSError, ValueError, KeyError) as exc:
            raise ImageGenerationServiceError(
                "任务产物清理失败",
                code="image_job_cleanup_failed",
                status_code=500,
                retryable=True,
            ) from exc

    def list_jobs(self, *, limit: int, offset: int, query: str, state: str | None) -> dict[str, Any]:
        items, total = self.storage.list_jobs(limit=limit, offset=offset, query=query, state=state)
        return {"items": [self.public_job(item) for item in items], "total": total, "next_cursor": offset + len(items) if offset + len(items) < total else None}

    def get_job(self, job_id: str) -> dict[str, Any]:
        record = self.storage.get_job(job_id)
        if record is None:
            raise ImageGenerationServiceError("任务不存在", code="image_job_not_found", status_code=404)
        return self.public_job(record)

    def artifact_data(self, artifact_id: str) -> tuple[bytes, str, str] | None:
        artifact = self.storage.get_artifact(artifact_id)
        if artifact is None:
            return None
        job_id = str(artifact["job_id"])
        path = self._safe_job_path(job_id, Path(str(artifact["relative_path"])))
        if not path.is_file() or path.is_symlink():
            return None
        try:
            with path.open("rb") as stream:
                data = stream.read(self.settings.max_upload_bytes + 1)
        except OSError:
            return None
        if (
            len(data) > self.settings.max_upload_bytes
            or len(data) != int(artifact["size_bytes"])
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
        ):
            return None
        return data, str(artifact["mime_type"]), path.name

    async def _run(self, job_id: str) -> None:
        try:
            current = self.storage.get_job(job_id)
            if current is None:
                return
            snapshot = dict(current.get("config") or {}).get("_provider_snapshot")
            requested_workers = int(dict(snapshot or {}).get("max_concurrency", 1) or 1)
            pending = int(current.get("attempt_counts", {}).get("pending", 0) or 0)
            worker_count = max(1, min(8, requested_workers, pending or 1))
            workers = [
                asyncio.create_task(
                    self._run_attempt_worker(job_id),
                    name=f"tagger2-image-{job_id}-{index}",
                )
                for index in range(worker_count)
            ]
            await asyncio.gather(*workers)

            current = self.storage.get_job(job_id)
            if current is None:
                return
            if self._cancel_events.setdefault(job_id, asyncio.Event()).is_set() or current["state"] == "cancelling":
                self.storage.finalize_job(job_id, state="cancelled")
                return
            counts = current.get("attempt_counts", {})
            if counts.get("pending", 0):
                # A retry may have queued work while the first worker group was
                # winding down. Drain it in this owning task before finalizing.
                await self._run_attempt_worker(job_id)
                current = self.storage.get_job(job_id)
                if current is None:
                    return
                counts = current.get("attempt_counts", {})
            if counts.get("running", 0):
                raise RuntimeError("image attempts remained running after workers exited")
            if counts.get("failed", 0):
                state = "partial" if current.get("completed_count", 0) else "failed"
            else:
                state = (
                    "succeeded"
                    if current.get("completed_count", 0) >= int(current.get("requested_count", 0))
                    else "partial"
                )
            self.storage.finalize_job(
                job_id,
                state=state,
                error_code=None if state == "succeeded" else "image_generation_no_output",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            current = self.storage.get_job(job_id)
            if current and current["state"] not in TERMINAL_STATES:
                self.storage.finalize_job(job_id, state="failed", error_code="image_generation_internal_error", error_message="image generation worker failed")

    async def _run_attempt_worker(self, job_id: str) -> None:
        while True:
            if self._cancel_events.setdefault(job_id, asyncio.Event()).is_set():
                return
            current = self.storage.get_job(job_id)
            if current is None or current["state"] == "cancelling":
                return
            attempt = self.storage.claim_attempt(job_id)
            if attempt is None:
                return
            try:
                if await asyncio.to_thread(self._attempt_has_complete_artifacts, job_id, attempt):
                    self.storage.finish_attempt(
                        attempt["id"],
                        state="succeeded",
                        parser_route="recovered_artifacts",
                    )
                    continue
                result = await self._execute_attempt(job_id, attempt)
                self.storage.finish_attempt(
                    attempt["id"],
                    state="succeeded",
                    parser_route=result.parser_route,
                    finish_reason=result.finish_reason,
                    texts=result.texts,
                )
            except asyncio.CancelledError:
                if self._cancel_events.setdefault(job_id, asyncio.Event()).is_set():
                    self.storage.finish_attempt(
                        attempt["id"],
                        state="cancelled",
                        error_code="image_job_cancelled",
                        error_message="cancelled",
                    )
                else:
                    # Application shutdown is resumable and must not turn an
                    # in-flight attempt into a user cancellation.
                    self.storage.release_attempt(attempt["id"])
                raise
            except ProviderError as exc:
                self.storage.finish_attempt(
                    attempt["id"],
                    state="failed",
                    error_code=exc.code,
                    error_message=exc.message,
                )
            except (OSError, ValueError, UploadValidationError) as exc:
                self.storage.finish_attempt(
                    attempt["id"],
                    state="failed",
                    error_code="image_artifact_write_failed",
                    error_message=str(exc),
                )
            await asyncio.sleep(0)

    async def _execute_attempt(self, job_id: str, attempt: Mapping[str, Any]):
        record = self.storage.get_job(job_id)
        if record is None:
            raise ProviderError("image job disappeared", code="image_job_not_found")
        if config_digest(dict(record.get("config") or {})) != str(record.get("config_hash") or ""):
            raise ProviderError(
                "image job snapshot digest mismatch",
                code="image_job_snapshot_tampered",
            )
        config_values = {
            key: value for key, value in dict(record["config"]).items()
            if key in ImageJobConfig.model_fields
        }
        config = ImageJobConfig.model_validate(config_values)
        snapshot = record["config"].get("_provider_snapshot")
        capability_snapshot = record["config"].get("_capability_snapshot")
        if not isinstance(snapshot, Mapping) or not isinstance(capability_snapshot, Mapping):
            raise ProviderError("image job snapshot is invalid", code="image_job_snapshot_invalid")
        capability = capability_from_public(capability_snapshot)
        protocol = str(snapshot.get("protocol") or "openai")
        keys = self._provider_keys(str(snapshot.get("secret_ref") or ""))
        base_url = str(snapshot.get("base_url") or "").strip()
        allow_local = bool(snapshot.get("allow_local", False))
        try:
            base_url = validate_provider_url(base_url, allow_local=allow_local, resolve_dns=True)
        except SecurityError as exc:
            raise ProviderError("image provider URL rejected", code="image_provider_url_blocked") from exc
        provider_config = ProviderConfig.from_mapping({
            "id": config.provider_id,
            "name": snapshot.get("name") or config.provider_id,
            "kind": snapshot.get("kind") or "custom",
            "protocol": protocol,
            "base_url": base_url,
            "model": config.model,
            "api_keys": keys,
            "temperature": snapshot.get("temperature", 0.7),
            "top_p": snapshot.get("top_p", 0.95),
            "top_k": snapshot.get("top_k", 40),
            "timeout_seconds": snapshot.get("timeout_seconds", 120),
            "max_concurrency": snapshot.get("max_concurrency", 1),
            "max_retries": snapshot.get("max_retries", 2),
            "retry_base_seconds": snapshot.get("retry_base_seconds", 1.0),
            "key_cooldown_seconds": snapshot.get("key_cooldown_seconds", 30.0),
            "allow_local": allow_local,
            "headers": snapshot.get("headers") or {},
        })
        # Reference images are independent file reads (plus a hash each), so
        # load them concurrently. gather() without return_exceptions preserves
        # the sequential behavior: the first failure propagates and the attempt
        # fails; result order matches the stored reference order.
        reference_items = self.storage.get_references(job_id)
        references = list(
            await asyncio.gather(
                *(asyncio.to_thread(self._load_reference, job_id, item) for item in reference_items)
            )
        )
        request = self._client_request(config, attempt)
        style = str(snapshot.get("image_api_style") or "auto")
        client = ImageGenerationClient(
            provider_config,
            family=capability.family,
            base_url=base_url,
            api_style=style,
            capability=capability,
            max_output_bytes=self.settings.max_upload_bytes,
            max_pixels=self.settings.max_image_pixels,
            max_edge=self.settings.max_image_edge,
            max_response_bytes=self.settings.max_request_bytes,
            key_pool=self._key_pools.setdefault(job_id, APIKeyPool(keys)),
        )
        result = await client.generate(request, references)
        first_ordinal = int(attempt.get("ordinal") or 0) * int(attempt.get("requested_count") or 1)
        for index, image in enumerate(result.images[: int(attempt.get("requested_count") or config.n)]):
            extension = {
                "image/jpeg": "jpg",
                "image/jpg": "jpg",
                "image/webp": "webp",
                "image/gif": "gif",
            }.get(image.mime_type.lower(), "png")
            relative = Path("outputs") / f"{first_ordinal + index:04d}.{extension}"
            destination = self._safe_job_path(job_id, relative)
            atomic_write_bytes(destination, image.data)
            self.storage.record_artifact(
                job_id=job_id,
                attempt_id=str(attempt["id"]),
                ordinal=first_ordinal + index,
                relative_path=relative.as_posix(),
                mime_type=image.mime_type,
                width=image.width,
                height=image.height,
                data=image.data,
                source=image.source,
            )
        return result

    def _client_request(self, config: ImageJobConfig, attempt: Mapping[str, Any]) -> ImageRequest:
        requested = int(attempt.get("requested_count") or 1)
        return ImageRequest(
            model=config.model,
            prompt=config.prompt,
            operation=config.operation,
            n=requested,
            aspect_ratio=config.aspect_ratio,
            image_size=config.image_size,
            resolution=config.resolution,
            multi_image_strategy=config.multi_image_strategy,
            include_text_modality=config.include_text_modality,
            system_instruction=config.system_instruction,
            temperature=config.temperature,
            top_p=config.top_p,
            top_k=config.top_k,
            size=config.size,
            quality=config.quality,
            background=config.background,
            output_format=config.output_format,
            output_compression=config.output_compression,
            moderation=config.moderation,
            input_fidelity=config.input_fidelity,
            response_format=config.response_format,
        )

    async def _prepare_uploads(self, uploads: Sequence[tuple[str, bytes, str]]) -> list[PreparedImage]:
        result: list[PreparedImage] = []
        for _name, data, _mime in uploads:
            if len(data) > self.settings.max_upload_bytes:
                raise ImageGenerationServiceError("参考图超过大小限制", code="image_reference_too_large")
            try:
                result.append(await asyncio.to_thread(
                    prepare_image,
                    data,
                    max_bytes=self.settings.online_image_bytes,
                    max_source_bytes=self.settings.max_upload_bytes,
                    max_pixels=self.settings.max_image_pixels,
                    max_dimension=self.settings.online_image_edge,
                ))
            except (ValueError, OSError) as exc:
                raise ImageGenerationServiceError("参考图无法读取", code="image_reference_invalid") from exc
        return result

    def _load_reference(self, job_id: str, item: Mapping[str, Any]) -> PreparedImage:
        path = self._safe_job_path(job_id, Path(str(item["relative_path"])))
        data = path.read_bytes()
        if hash_bytes(data) != str(item["sha256"]):
            raise ProviderError("参考图校验失败", code="image_reference_tampered")
        return PreparedImage(
            data=data,
            mime_type=str(item["mime_type"]),
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            sha256=hash_bytes(data),
        )

    def _provider_keys(self, reference: str) -> tuple[str, ...]:
        reference = reference.strip()
        if not reference:
            raise ProviderError("image provider secret unavailable", code="image_provider_secret_unavailable")
        try:
            return tuple(self.secrets.get_many(reference))
        except Exception as exc:
            raise ProviderError("image provider secret unavailable", code="image_provider_secret_unavailable") from exc

    @staticmethod
    def _effective_style(capability: ImageCapability, profile_config: Mapping[str, Any]) -> str:
        style = str(profile_config.get("image_api_style") or "auto").strip().lower()
        if style == "auto":
            return "native" if capability.family == "google_gemini" else "openai_images"
        return style

    def _validate_config(
        self,
        config: ImageJobConfig,
        capability: ImageCapability,
        reference_count: int,
        image_style: str,
    ) -> None:
        if image_style == "native" and capability.family != "google_gemini":
            raise ImageGenerationServiceError(
                "Native 图像请求只适用于 Gemini 能力族",
                code="image_api_style_incompatible",
            )
        if config.operation not in capability.operations:
            raise ImageGenerationServiceError("操作类型不受模型支持", code="image_operation_unsupported")
        if reference_count > capability.max_references:
            raise ImageGenerationServiceError("参考图数量超过模型限制", code="image_reference_count_exceeded")
        values = config.model_dump(exclude_none=True)
        ignored = {"provider_id", "model", "operation", "prompt", "n", "multi_image_strategy"}
        if values.get("include_text_modality") is False:
            # This is the Pydantic default and has no wire effect for vendors
            # that do not expose Gemini's TEXT + IMAGE response toggle.
            values.pop("include_text_modality", None)
        supported = set(capability.parameters)
        if (
            image_style in {"openai_chat", "chat"}
            and capability.family != "google_gemini"
        ):
            # Chat-compatible gateways have no portable image parameter
            # envelope.  Do not silently drop a user-selected size, quality,
            # or Gemini generationConfig field.
            supported &= {"temperature", "top_p", "system_instruction"}
        unsupported = sorted(key for key in values if key not in ignored and key not in supported)
        if unsupported:
            raise ImageGenerationServiceError("模型不支持请求参数", code="image_parameter_unsupported")
        if config.n > capability.max_outputs:
            raise ImageGenerationServiceError("输出数量超过模型限制", code="image_output_count_exceeded")
        if (
            config.multi_image_strategy == "candidate_count"
            and "multi_image_strategy" not in capability.parameters
        ):
            raise ImageGenerationServiceError(
                "模型不支持 Candidate count 多图策略",
                code="image_parameter_unsupported",
            )
        for key, choices in capability.enums.items():
            value = values.get(key)
            if value is None:
                continue
            if (
                key == "size"
                and OPENAI_CUSTOM_SIZE_SENTINEL in choices
                and SIZE_PATTERN.fullmatch(str(value))
            ):
                # gpt-image-2 accepts arbitrary WIDTHxHEIGHT sizes; the finer
                # divisibility and range rules stay enforced upstream.
                continue
            if value not in choices:
                raise ImageGenerationServiceError("参数值不受模型支持", code="image_parameter_invalid")
        if config.operation == "edit" and reference_count == 0:
            raise ImageGenerationServiceError("编辑操作需要参考图", code="image_reference_required")
        if config.input_fidelity is not None and config.operation != "edit":
            raise ImageGenerationServiceError(
                "输入保真度只适用于图像编辑",
                code="image_parameter_invalid",
            )
        if config.output_compression is not None and config.output_format not in {"jpeg", "webp"}:
            raise ImageGenerationServiceError(
                "压缩参数只适用于 JPEG 或 WebP",
                code="image_parameter_invalid",
            )
        if config.background == "transparent" and config.output_format == "jpeg":
            raise ImageGenerationServiceError(
                "JPEG 不支持透明背景",
                code="image_parameter_invalid",
            )
        if (
            reference_count
            and image_style not in {"openai_chat", "chat", "native"}
            and capability.family != "google_gemini"
            and config.operation != "edit"
        ):
            raise ImageGenerationServiceError(
                "该图像端点的参考图请求必须使用编辑操作",
                code="image_edit_operation_required",
            )

    def _normalized_config(
        self,
        config: ImageJobConfig,
        capability: ImageCapability,
        *,
        provider_snapshot: Mapping[str, Any],
    ) -> dict[str, Any]:
        values = config.model_dump(exclude_none=True)
        for key, value in capability.defaults.items():
            values.setdefault(key, value)
        values.update({
            "provider_id": config.provider_id,
            "model": config.model,
            "family": capability.family,
            "operation": config.operation,
            "_provider_snapshot": dict(provider_snapshot),
            "_capability_snapshot": capability.public(model=config.model, provider_id=config.provider_id),
        })
        return values

    def _provider_snapshot(
        self,
        profile: Mapping[str, Any],
        profile_config: Mapping[str, Any],
        *,
        protocol: str,
        style: str,
    ) -> dict[str, Any]:
        kind = str(profile.get("kind") or "custom")
        allow_local = kind in {"lm_studio", "antigravity"} or self.settings.allow_local_providers
        raw_top_k = profile_config.get("top_k", 40)
        base_url = str(profile_config.get("image_base_url") or profile.get("base_url") or "").strip()
        try:
            base_url = validate_provider_url(base_url, allow_local=allow_local, resolve_dns=True)
        except SecurityError as exc:
            raise ImageGenerationServiceError(
                "图像 Provider 地址无效",
                code="invalid_image_provider_url",
                status_code=400,
            ) from exc
        headers: dict[str, str] = {}
        raw_headers = profile_config.get("headers")
        if isinstance(raw_headers, Mapping):
            sensitive_fragments = (
                "apikey",
                "auth",
                "authorization",
                "cookie",
                "credential",
                "key",
                "password",
                "secret",
                "session",
                "signature",
                "token",
            )
            for key, value in raw_headers.items():
                name = str(key).strip()
                compact_name = "".join(character for character in name.casefold() if character.isalnum())
                if any(fragment in compact_name for fragment in sensitive_fragments):
                    continue
                text = str(value)
                if name and len(name) <= 128 and len(text) <= 4096:
                    headers[name] = text
        return {
            "id": str(profile.get("id") or ""),
            "name": str(profile.get("name") or ""),
            "kind": kind,
            "protocol": protocol,
            "base_url": base_url,
            "image_api_style": style,
            "secret_ref": str(profile.get("secret_ref") or f"provider_{profile.get('id')}"),
            "allow_local": allow_local,
            "temperature": float(profile_config.get("temperature", 0.7)),
            "top_p": float(profile_config.get("top_p", 0.95)),
            "top_k": int(raw_top_k) if raw_top_k is not None else None,
            "timeout_seconds": float(profile_config.get("timeout_seconds", 120)),
            "max_concurrency": max(1, min(8, int(profile_config.get("max_concurrency", 3) or 3))),
            "max_retries": max(0, min(10, int(profile_config.get("retries", 2) or 2))),
            "retry_base_seconds": float(profile_config.get("retry_base_seconds", 1.0)),
            "key_cooldown_seconds": float(profile_config.get("key_cooldown_seconds", 30.0)),
            "headers": headers,
        }

    def public_job(self, value: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(value)
        public_config = dict(data.get("config") or {})
        public_config.pop("_provider_snapshot", None)
        public_config.pop("_capability_snapshot", None)
        data["config"] = public_config
        data["artifacts"] = [
            {**dict(item), "download_url": f"/api/v1/image-generation/artifacts/{item['id']}"}
            for item in value.get("artifacts", [])
        ]
        data.pop("error_message", None)
        return data

    def _safe_job_path(self, job_id: str, relative: Path) -> Path:
        if not job_id.isalnum() or relative.is_absolute() or ".." in relative.parts:
            raise SecurityError("invalid image job path")
        root = (self.jobs_root / job_id).resolve(strict=False)
        candidate = (root / relative).resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise SecurityError("invalid image job path")
        return candidate

    def _attempt_has_complete_artifacts(self, job_id: str, attempt: Mapping[str, Any]) -> bool:
        requested = int(attempt.get("requested_count") or 1)
        artifacts = self.storage.list_attempt_artifacts(str(attempt["id"]))
        if len(artifacts) < requested:
            return False
        for artifact in artifacts[:requested]:
            path = self._safe_job_path(job_id, Path(str(artifact["relative_path"])))
            if not path.is_file() or path.is_symlink():
                return False
            try:
                if hash_bytes(path.read_bytes()) != str(artifact["sha256"]):
                    return False
            except OSError:
                return False
        return True

    def _cleanup_incomplete_creates(self) -> None:
        """Remove only directories explicitly marked as interrupted creates."""

        known: set[str] = set()
        for state in JOB_STATES:
            known.update(self.storage.job_ids(state))
        for directory in self.jobs_root.iterdir():
            if not directory.is_dir() or directory.name in known:
                continue
            marker = directory / ".creating"
            if marker.is_file():
                shutil.rmtree(directory, ignore_errors=True)


__all__ = ["ImageGenerationService", "ImageGenerationServiceError"]
