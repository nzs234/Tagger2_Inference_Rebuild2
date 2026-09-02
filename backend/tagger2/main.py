"""Tagger2 Inference Rebuild application.

The HTTP layer is intentionally thin: paths and credentials are validated at
the boundary, jobs are persisted by ``SQLiteStorage`` and the mode-specific
work is delegated to ``LocalInferenceEngine`` or ``VisionProvider``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Iterable, Mapping, Sequence, cast

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .artifacts import (
    ArtifactManager,
    numbered_name,
)
from .classifiers import AestheticClassifier, ClassifierConfig, ClassifierName
from .config import AppConfig, configure_cache_environment
from .jobs import JobManager, ProcessResult
from .local_inference import (
    LocalInferenceEngine,
    LocalPrediction,
    UnsafeModelError,
    select_device,
)
from .model_registry import ModelRegistry
from .model_downloads import ModelDownloadManager
from .processors import (
    DEFAULT_JSON_PROMPT,
    DEFAULT_NL_PROMPT,
    DEFAULT_PROMPT,
    DEFAULT_TAG_PROMPT,
    ProcessorHost,
    _safe_error,
)
from .providers import ProviderConfig, ProviderError, create_provider
from .secrets import CompositeSecretStore, SecretStoreUnavailable, get_secret_metadata
from .security import (
    PathAllowlist,
    PathNotAllowedError,
    PathRoot,
    SecurityError,
    atomic_write_bytes,
    opaque_id,
    sanitize_filename,
    validate_bearer_token,
    validate_image_bytes,
    validate_provider_url,
)
from .storage import JobItemRecord, JobRecord, SQLiteStorage, config_digest
from .user_settings import UserSettingsStore
from .workflow.api import create_workflow_router
from .workflow.db import WorkflowDatabase
from .workflow.resources import WorkflowResourceCatalog
from .image_generation import ImageGenerationService
from .image_generation.api import create_image_generation_router
from .video_prompts import (
    build_video_prompt_system_prompt,
    build_video_prompt_user_message,
    normalize_fl2va_single_image_role,
    normalize_video_prompt_mode,
    parse_current_package_json,
    parse_video_prompt_response,
    resolve_h3_base_mode,
)


logger = logging.getLogger("tagger2.main")


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ThresholdValue = Annotated[float, Field(ge=0, le=1)]


class RootCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern=r"^(input|output|model)$")
    path: str = Field(min_length=1, max_length=2048)


class ProviderCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str
    protocol: str | None = None
    base_url: str = Field(min_length=1, max_length=2048)
    primary_model: str = Field(min_length=1, max_length=256)
    fallback_model: str | None = None
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.95, ge=0, le=1)
    top_k: int | None = Field(default=40, ge=0, le=1000)
    max_tokens: int = Field(default=8192, ge=1, le=131072)
    timeout_seconds: float = Field(default=120, gt=0, le=900)
    retries: int = Field(default=2, ge=0, le=10)
    image_enabled: bool = True
    image_family: str = Field(default="auto", pattern=r"^(auto|google_gemini|openai_gpt_image|xai_grok_image|unknown)$")
    image_base_url: str | None = Field(default=None, max_length=2048)
    image_api_style: str = Field(default="auto", pattern=r"^(auto|native|openai_images|openai_chat)$")
    enabled: bool = True


class ProviderPatch(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    kind: str | None = None
    protocol: str | None = None
    base_url: str | None = Field(default=None, max_length=2048)
    primary_model: str | None = Field(default=None, max_length=256)
    fallback_model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=1000)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)
    timeout_seconds: float | None = Field(default=None, gt=0, le=900)
    retries: int | None = Field(default=None, ge=0, le=10)
    image_enabled: bool | None = None
    image_family: str | None = Field(default=None, pattern=r"^(auto|google_gemini|openai_gpt_image|xai_grok_image|unknown)$")
    image_base_url: str | None = Field(default=None, max_length=2048)
    image_api_style: str | None = Field(default=None, pattern=r"^(auto|native|openai_images|openai_chat)$")
    enabled: bool | None = None


class ProviderDiscovery(APIModel):
    kind: str
    protocol: str | None = None
    base_url: str = Field(min_length=1, max_length=2048)
    api_keys: list[str] = Field(default_factory=list, max_length=64)
    timeout_seconds: float = Field(default=120, gt=0, le=900)


class SecretPayload(APIModel):
    keys: list[str] = Field(default_factory=list, max_length=64)


class JobSource(APIModel):
    type: str = Field(pattern=r"^(upload|scan)$")
    upload_id: str | None = None
    root_id: str | None = None
    relative_path: str = ""
    recursive: bool = True
    patterns: list[str] = Field(default_factory=list, max_length=32)


class ScanPayload(APIModel):
    root_id: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(default="", max_length=2048)
    recursive: bool = True
    patterns: list[str] = Field(default_factory=list, max_length=32)
    page_size: int = Field(default=500, ge=1, le=20_000)
    cursor: int = Field(default=0, ge=0)


class JobOutput(APIModel):
    root_id: str | None = None
    relative_path: str = ""
    # ``json`` is the wire name; using a non-shadowing Python attribute keeps
    # Pydantic and static type checkers quiet while preserving the API.
    json_output: bool = Field(default=True, alias="json")
    txt: bool = False
    txt_include_tags: bool = False
    replace_underscores: bool = False
    include_rating: bool = False
    escape_parentheses: bool = True
    conflict: str = Field(default="validate-skip", pattern=r"^(validate-skip|overwrite|rename)$")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelLoadPayload(APIModel):
    device: str | None = Field(default=None, pattern=r"^(auto|cpu|cuda(?::\d+)?)$")
    adapter_type: str = Field(default="none", pattern=r"^(none|lora|lokr)$")
    adapter_path: str | None = Field(default=None, max_length=2048)
    adapter_scale: float = Field(default=1.0, ge=0, le=4)
    trusted_pickle: bool = False
    allow_unsafe_pickle: bool = False


class ModelPatchPayload(APIModel):
    trusted_pickle: bool | None = None
    threshold: float | None = Field(default=None, ge=0, le=1)
    thresholds: dict[str, ThresholdValue] | None = Field(default=None, max_length=128)
    reset_thresholds: bool = False


class ModelDownloadPayload(APIModel):
    url: str = Field(min_length=1, max_length=2048)
    revision: str | None = Field(default=None, max_length=200)


class JobCreate(APIModel):
    mode: str = Field(pattern=r"^(local|online)$")
    # Hybrid jobs intentionally persist as ``local`` jobs.  This keeps the
    # established SQLite mode constraint stable while reusing local batching.
    hybrid: bool = False
    source: JobSource
    output: JobOutput = Field(default_factory=JobOutput)
    provider_id: str | None = None
    provider_model: str | None = Field(default=None, max_length=256)
    model_ids: list[str] = Field(default_factory=list, max_length=32)
    thresholds: dict[str, ThresholdValue | dict[str, ThresholdValue]] = Field(default_factory=dict, max_length=128)
    classifiers: list[str] = Field(default_factory=list, max_length=1)
    separate_models: bool = False
    trigger_artist: str = Field(default="", max_length=256)
    prompt: str | None = Field(default=None, max_length=20000)
    tag_prompt: str | None = Field(default=None, max_length=20000)
    nl_prompt: str | None = Field(default=None, max_length=20000)
    json_prompt: str | None = Field(default=None, max_length=20000)
    online_response: str | None = Field(
        default=None,
        pattern=r"^(json|nl|nl_tags)$",
    )
    online_concurrency: int = Field(default=3, ge=1, le=128)
    batch_size: int = Field(default=16, ge=1, le=512)


class SettingsPayload(APIModel):
    input_root_id: str | None = None
    output_root_id: str | None = None
    default_mode: str = Field(default="online", pattern=r"^(local|online)$")
    default_threshold: float = Field(default=0.35, ge=0, le=1)
    default_json: bool = True
    default_txt: bool = False
    bind_host: str = "127.0.0.1"
    lan_enabled: bool = False
    production: bool = True
    max_upload_mb: int = Field(default=32, ge=1, le=512)
    max_image_pixels: int = Field(default=80_000_000, ge=1_000_000)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _job_public(record: JobRecord) -> dict[str, Any]:
    config = record.config or {}
    return {
        "id": record.id,
        "mode": record.mode,
        "state": record.state,
        "phase": record.phase,
        "processed": record.processed,
        "total": record.total,
        "succeeded": record.succeeded,
        "skipped": record.skipped,
        "failed": record.failed,
        "current_item": config.get("current_item"),
        "provider_id": config.get("provider_id"),
        "model_ids": config.get("model_ids", []),
        "hybrid": bool(config.get("hybrid", False)),
        "source_root_id": record.source_root_id,
        "output_root_id": record.output_root_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "error": record.error,
        "rate": config.get("rate"),
        "eta": config.get("eta"),
    }


def _error_payload(
    *,
    request: Request,
    code: str,
    message: str,
    fields: Mapping[str, Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    """Build the one error shape exposed by every API route."""

    return {
        "code": code,
        "message": message,
        "fields": dict(fields) if fields else None,
        "request_id": getattr(request.state, "request_id", ""),
        "retryable": bool(retryable),
    }


class Runtime:
    """Process-local services shared by FastAPI routes and job workers."""

    def __init__(self, settings: AppConfig):
        self.settings = settings
        configure_cache_environment(settings)
        settings.ensure_directories()
        data_dir = settings.data_dir or settings.project_root / "data"
        self.allowlist = PathAllowlist()
        self.user_settings = UserSettingsStore(
            settings_file=data_dir / "settings.json",
            allowlist=self.allowlist,
        )
        self._register_initial_roots()
        model_root = settings.project_root / "models"
        model_root.mkdir(parents=True, exist_ok=True)
        self.registry = ModelRegistry([model_root], allowlist=self.allowlist)
        self.registry.discover()
        self.engine = LocalInferenceEngine(
            self.registry,
            device=os.getenv("TAGGER2_DEVICE", "auto"),
            allow_unsafe_pickle=settings.allow_unsafe_pickle,
            max_loaded_models=settings.max_loaded_models,
            memory_budget_mb=settings.model_memory_budget_mb,
        )
        self.model_downloads = ModelDownloadManager(
            model_root,
            self.registry,
            loader=self._load_downloaded_model,
        )
        classifier_cache = (settings.cache_dir or settings.project_root / "data_cache") / "huggingface"
        self.classifiers = {
            "aesthetic": AestheticClassifier(
                ClassifierConfig(
                    project_dir=settings.project_root,
                    models_dir=model_root,
                    cache_dir=classifier_cache,
                    device=self.engine.device,
                )
            ),
        }
        self.storage = SQLiteStorage(settings.database_path or data_dir / "tagger2.sqlite3")
        self._load_model_profiles()
        self.artifacts = ArtifactManager(self.storage)
        # The workflow module owns an isolated database and resource library so
        # the existing tagger2.sqlite3 and model assets are never rewritten.
        workflow_dir = data_dir / "workflows"
        self.workflow_resources = WorkflowResourceCatalog(workflow_dir / "resources")
        self.workflow_database = WorkflowDatabase(workflow_dir / "workflows.sqlite3")
        self.secrets = CompositeSecretStore()
        # Bounded LRU of live VisionProvider instances keyed by stable profile
        # identity (see Runtime.provider). Evicted providers are closed.
        self.providers: OrderedDict[str, Any] = OrderedDict()
        self.provider_configs: dict[str, dict[str, Any]] = {}
        self._provider_lock = threading.RLock()
        self._provider_close_tasks: set[asyncio.Task[None]] = set()
        self.upload_index: dict[str, list[dict[str, Any]]] = {}
        self._upload_lock = threading.RLock()
        self._load_upload_index()
        self._load_provider_profiles()
        self._ensure_default_providers()
        self.image_generation = ImageGenerationService(
            settings,
            provider_profiles=self.storage.get_provider_profile,
            secrets=self.secrets,
        )
        from .tag_manager import (
            TagDatabase,
            TagManagerService,
            TagManagerStore,
            ThumbnailService,
            default_tag_manager_database_path,
        )

        tag_manager_data_dir = settings.data_dir or settings.project_root / "data"
        self.tag_manager = TagManagerService(
            store=TagManagerStore(default_tag_manager_database_path()),
            allowlist=self.allowlist,
            thumbnails=ThumbnailService(tag_manager_data_dir / "tag_manager" / "thumbnails"),
            tag_database=TagDatabase(),
        )
        self.processors = ProcessorHost(self)
        self.job_manager = JobManager(self.storage)
        self.job_manager.register_processor("local", self.processors.local)
        self.job_manager.register_batch_processor("local", self.processors.local_batch)
        self.job_manager.register_processor("online", self.processors.online)
        self.gpu_lock = asyncio.Lock()

    def _register_initial_roots(self) -> None:
        project = self.settings.project_root
        upload = self.settings.upload_dir or project / "data" / "uploads"
        artifacts = self.settings.artifact_dir or project / "data" / "artifacts"
        upload.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        model = project / "models"
        model.mkdir(parents=True, exist_ok=True)
        self.allowlist.register(model, root_id=opaque_id(model, prefix="root"), label="本地模型", kind="model", writable=False)
        self.allowlist.register(upload, root_id=opaque_id(upload, prefix="root"), label="上传缓存", kind="input", writable=True)
        self.allowlist.register(artifacts, root_id=opaque_id(artifacts, prefix="root"), label="任务产物", kind="output", writable=True)
        for configured in self.settings.roots:
            if configured.path.is_dir():
                self.allowlist.register(
                    configured.path,
                    root_id=configured.root_id,
                    label=configured.label,
                    kind=configured.kind,
                    writable=configured.writable,
                )
        input_env = os.getenv("TAGGER2_INPUT_ROOTS", "")
        output_env = os.getenv("TAGGER2_OUTPUT_ROOTS", "")
        for index, raw in enumerate(filter(None, (item.strip() for item in input_env.split(os.pathsep)))):
            path = Path(raw).expanduser()
            if path.is_dir():
                self.allowlist.register(path, label=f"输入目录 {index + 1}", kind="input", writable=False)
        for index, raw in enumerate(filter(None, (item.strip() for item in output_env.split(os.pathsep)))):
            path = Path(raw).expanduser()
            if path.is_dir():
                self.allowlist.register(path, label=f"输出目录 {index + 1}", kind="output", writable=True)

        self.user_settings.load_persistent_roots()

    def read_settings_document(self) -> dict[str, Any]:
        return self.user_settings.read_settings_document()

    def save_user_settings(self, values: Mapping[str, Any]) -> None:
        self.user_settings.save_user_settings(values)

    def register_persistent_root(
        self,
        path: Path,
        *,
        name: str,
        kind: str,
        writable: bool | None = None,
    ) -> PathRoot:
        return self.user_settings.register_persistent_root(
            path, name=name, kind=kind, writable=writable
        )

    def resolve_root(self, root_id: str, *, kind: str | None = None, writable: bool | None = None) -> PathRoot:
        root = self.allowlist.get(root_id)
        if kind and root.kind != kind:
            raise PathNotAllowedError("root 类型不匹配")
        if writable and not root.writable:
            raise PathNotAllowedError("root 不可写")
        return root

    def _upload_index_path(self) -> Path:
        return (self.settings.data_dir or self.settings.project_root / "data") / "uploads.json"

    def _load_upload_index(self) -> None:
        path = self._upload_index_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if isinstance(value, dict):
                self.upload_index = value
        except Exception:
            self.upload_index = {}

    def _save_upload_index(self) -> None:
        path = self._upload_index_path()
        atomic_write_bytes(path, json.dumps(self.upload_index, ensure_ascii=False, indent=2).encode("utf-8"))

    def _load_provider_profiles(self) -> None:
        for profile in self.storage.list_provider_profiles():
            self.provider_configs[profile["id"]] = profile

    def _load_model_profiles(self) -> None:
        for profile in self.storage.list_model_profiles():
            try:
                record = self.registry.get(str(profile["id"]))
                config = profile.get("config") or {}
                thresholds = config.get("thresholds") if isinstance(config, Mapping) else None
                if isinstance(thresholds, Mapping):
                    record.set_thresholds(thresholds)
            except (KeyError, TypeError, ValueError):
                continue

    def _load_downloaded_model(self, model_id: str) -> None:
        """Load a newly downloaded safe model without bypassing pickle policy."""

        try:
            self.engine.load(
                model_id,
                allow_unsafe_pickle=self.settings.allow_unsafe_pickle,
            )
            self.registry.mark_loaded(model_id, True)
        except Exception:
            self.registry.mark_loaded(model_id, False)
            raise

    def _ensure_default_providers(self) -> None:
        marker = "default_providers_initialized"
        if self.storage.get_metadata(marker) == "1":
            return
        # Databases created by earlier releases already have their defaults.
        # Mark them initialized so a user deletion remains deleted on restart.
        if self.provider_configs:
            self.storage.set_metadata(marker, "1")
            return
        defaults = [
            ("gemini", "Gemini 官方", "gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini-2.0-flash-exp"),
            ("openai", "OpenAI 兼容", "openai", "https://api.openai.com/v1", "gpt-4.1-mini"),
            ("lmstudio", "LM Studio", "lm_studio", "http://127.0.0.1:1234/v1", "gemma-4-31b-jang_4m-crack"),
            ("antigravity", "Antigravity", "antigravity", "http://127.0.0.1:8045", "gemini-3-flash"),
        ]
        for pid, name, kind, base, model in defaults:
            if pid in self.provider_configs:
                continue
            try:
                validate_provider_url(
                    base,
                    allow_local=kind in {"lm_studio", "antigravity"}
                    or self.settings.allow_local_providers,
                )
            except SecurityError:
                continue
            profile = self.storage.upsert_provider_profile(
                pid,
                name=name,
                kind=kind,
                base_url=base,
                config={
                    "primary_model": model,
                    "protocol": "gemini" if kind in {"gemini", "antigravity"} else "openai",
                    "fallback_model": "",
                    "temperature": 0.7,
                    "top_p": 0.95,
                    "top_k": 40,
                    "max_tokens": 8192,
                    "timeout_seconds": 120,
                    "retries": 2,
                },
                secret_ref=f"provider_{pid}",
            )
            self.provider_configs[pid] = profile
        self.storage.set_metadata(marker, "1")

    def list_roots(self) -> list[dict[str, Any]]:
        result = []
        for root in self.allowlist.list_public():
            result.append({
                "id": root["root_id"],
                "name": root["label"],
                "kind": root["kind"],
                "writable": root["writable"],
                "path_hint": "项目目录" if root["kind"] in {"model", "upload", "output"} else "已授权",
            })
        return result

    def resolve_item_path(self, item: JobItemRecord) -> Path:
        return ProcessorHost(self).resolve_item_path(item)

    def _output_path(self, item: JobItemRecord, job: JobRecord, suffix: str) -> Path:
        return ProcessorHost(self)._output_path(item, job, suffix)

    def _conflict_path(self, path: Path, policy: str, *, valid: bool = False) -> tuple[Path, bool]:
        return ProcessorHost(self)._conflict_path(path, policy, valid=valid)

    async def local_processor(self, item: JobItemRecord, job: JobRecord) -> ProcessResult:
        return await ProcessorHost(self).local_processor(item, job)

    async def local_batch_processor(
        self,
        items: Sequence[JobItemRecord],
        job: JobRecord,
    ) -> list[ProcessResult]:
        return await ProcessorHost(self).local_batch_processor(items, job)

    async def _hybrid_batch_processor(
        self,
        items: Sequence[JobItemRecord],
        job: JobRecord,
    ) -> list[ProcessResult]:
        return await ProcessorHost(self)._hybrid_batch_processor(items, job)

    def _hybrid_output_paths(
        self,
        item: JobItemRecord,
        job: JobRecord,
    ) -> tuple[Path, Path | None]:
        return ProcessorHost(self)._hybrid_output_paths(item, job)

    def _hybrid_outputs_current(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
    ) -> bool:
        return ProcessorHost(self)._hybrid_outputs_current(item, job, source)

    def _hybrid_skipped_result(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
    ) -> ProcessResult:
        return ProcessorHost(self)._hybrid_skipped_result(item, job, source)

    async def _write_hybrid_result(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
        local_result: ProcessResult,
    ) -> ProcessResult:
        return await ProcessorHost(self)._write_hybrid_result(item, job, source, local_result)

    def _local_model_ids(self, config: Mapping[str, Any]) -> list[str]:
        return ProcessorHost(self)._local_model_ids(config)

    def _local_processor_sync(self, item: JobItemRecord, job: JobRecord) -> ProcessResult:
        return ProcessorHost(self)._local_processor_sync(item, job)

    def _local_batch_processor_sync(
        self,
        items: Sequence[JobItemRecord],
        job: JobRecord,
    ) -> list[ProcessResult]:
        return ProcessorHost(self)._local_batch_processor_sync(items, job)

    def _read_current_local_prediction(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
    ) -> LocalPrediction | None:
        return ProcessorHost(self)._read_current_local_prediction(item, job, source)

    def _run_local_classifiers(
        self,
        images: Sequence[Any],
        predictions: Sequence[LocalPrediction],
        config: Mapping[str, Any],
    ) -> None:
        ProcessorHost(self)._run_local_classifiers(images, predictions, config)

    def _write_local_result(
        self,
        item: JobItemRecord,
        job: JobRecord,
        source: Path,
        prediction: LocalPrediction,
    ) -> ProcessResult:
        return ProcessorHost(self)._write_local_result(item, job, source, prediction)

    async def online_processor(self, item: JobItemRecord, job: JobRecord) -> ProcessResult:
        return await ProcessorHost(self).online_processor(item, job)

    def provider(
        self,
        provider_id: str,
        *,
        profile_override: Mapping[str, Any] | None = None,
    ):
        if not provider_id:
            raise ProviderError("未选择在线 provider", code="provider_required")
        # Cache identity is the provider's STABLE profile only. Per-job
        # snapshots freeze max_concurrency (the requested online_concurrency),
        # which must not multiply cached instances: two jobs sharing a provider
        # profile share one VisionProvider regardless of per-job concurrency.
        cache_key = (
            provider_id
            if profile_override is None
            else f"{provider_id}:{config_digest(_stable_provider_profile(profile_override))[:20]}"
        )
        with self._provider_lock:
            existing = self.providers.get(cache_key)
            if existing is not None:
                self.providers.move_to_end(cache_key)
                return existing
            stored_profile = self.provider_configs.get(provider_id) or self.storage.get_provider_profile(provider_id)
            profile = dict(profile_override) if profile_override is not None else stored_profile
            if profile is None:
                raise ProviderError("provider 不存在", code="provider_not_found")
            if not bool(profile.get("enabled", True)):
                raise ProviderError("provider 已禁用", code="provider_disabled")
            cfg = dict(profile.get("config") or {})
            cfg.update({"id": provider_id, "name": profile.get("name"), "kind": profile.get("kind"), "base_url": profile.get("base_url")})
            cfg["model"] = cfg.pop("primary_model", cfg.get("model", ""))
            cfg["backup_model"] = cfg.pop("fallback_model", cfg.get("backup_model"))
            cfg["max_output_tokens"] = cfg.pop("max_tokens", cfg.get("max_output_tokens", 8192))
            # Concurrency is a per-batch-task setting. Ignore the legacy
            # provider-level field so old profiles cannot constrain new jobs.
            cfg.pop("concurrency", None)
            if profile_override is not None:
                # Option (a): the snapshot's max_concurrency is a volatile
                # per-job override, so it is excluded from the cache identity
                # above AND from the built config. The provider profile's own
                # max_concurrency governs the shared instance's semaphore;
                # per-job parallelism is already enforced by the job manager's
                # worker count (_worker_concurrency) and the hybrid batch
                # semaphore, so nothing needs a per-job provider semaphore.
                cfg.pop("max_concurrency", None)
                stored_cfg = dict((stored_profile or {}).get("config") or {})
                if "max_concurrency" in stored_cfg:
                    cfg["max_concurrency"] = stored_cfg["max_concurrency"]
            cfg["max_concurrency"] = cfg.get("max_concurrency", 3)
            cfg["max_retries"] = cfg.pop("retries", cfg.get("max_retries", 2))
            # Image-generation routing is handled by its dedicated adapter;
            # the text provider must not receive these profile-only fields.
            for image_key in ("image_enabled", "image_family", "image_base_url", "image_api_style"):
                cfg.pop(image_key, None)
            cfg["allow_local"] = bool(profile.get("kind") in {"lm_studio", "antigravity"} or self.settings.allow_local_providers)
            secret_ref = (
                (stored_profile or {}).get("secret_ref")
                or profile.get("secret_ref")
                or f"provider_{provider_id}"
            )
            keys = self.secrets.get_many(secret_ref)
            cfg["api_keys"] = tuple(keys)
            try:
                instance = create_provider(ProviderConfig.from_mapping(cfg))
            except Exception as exc:
                raise ProviderError(f"provider 配置无效: {exc}", code="provider_config_invalid") from exc
            self.providers[cache_key] = instance
            self._evict_providers_locked()
            return instance

    def _evict_providers_locked(self) -> None:
        """Drop least-recently-used providers beyond the cache limit.

        Caller must hold ``self._provider_lock``. Each evicted provider owns an
        httpx connection pool, so schedule its ``aclose()`` instead of just
        dropping the reference.
        """

        while len(self.providers) > _PROVIDER_CACHE_LIMIT:
            _key, instance = self.providers.popitem(last=False)
            self._schedule_provider_close(instance)

    def _schedule_provider_close(self, instance: Any) -> None:
        async def _close() -> None:
            try:
                await instance.aclose()
            except Exception:
                logger.exception("closing evicted provider client failed")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            # No running loop (provider() is sync and may be called outside
            # async contexts, e.g. from tests): close on a temporary loop.
            try:
                asyncio.run(_close())
            except Exception:
                logger.exception("closing evicted provider client failed")
            return
        task = loop.create_task(_close())
        self._provider_close_tasks.add(task)
        task.add_done_callback(self._provider_close_tasks.discard)

    async def invalidate_provider(self, provider_id: str) -> None:
        """Close live and snapshotted clients for a changed provider."""

        with self._provider_lock:
            keys = [
                key
                for key in self.providers
                if key == provider_id or key.startswith(provider_id + ":")
            ]
            instances = [self.providers.pop(key) for key in keys]
        for instance in instances:
            await instance.aclose()

    async def close(self) -> None:
        await self.image_generation.close()
        await self.job_manager.shutdown()
        await self.model_downloads.close()
        pending = [task for task in self._provider_close_tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for provider in list(self.providers.values()):
            try:
                await provider.aclose()
            except Exception:
                pass
        self.providers.clear()
        for classifier in self.classifiers.values():
            classifier.unload()
        self.engine.close()
        self.storage.close()


def _provider_public(runtime: Runtime, profile: Mapping[str, Any]) -> dict[str, Any]:
    cfg = dict(profile.get("config") or {})
    secret_ref = profile.get("secret_ref") or f"provider_{profile['id']}"
    meta = get_secret_metadata(runtime.secrets, secret_ref)
    kind = str(profile.get("kind") or "openai")
    protocol = cfg.get("protocol") or ("gemini" if kind in {"gemini", "antigravity"} else "claude" if kind == "claude" else "openai")
    return {
        "id": profile["id"],
        "name": profile.get("name") or profile["id"],
        "kind": "lmstudio" if kind == "lm_studio" else kind,
        "protocol": protocol,
        "base_url": profile.get("base_url", ""),
        "primary_model": cfg.get("primary_model", ""),
        "fallback_model": cfg.get("fallback_model") or None,
        "temperature": cfg.get("temperature", 0.7),
        "top_p": cfg.get("top_p", 0.95),
        "top_k": cfg.get("top_k", 40),
        "max_tokens": cfg.get("max_tokens", 8192),
        "timeout_seconds": cfg.get("timeout_seconds", 120),
        "retries": cfg.get("retries", 2),
        "image_enabled": bool(cfg.get("image_enabled", True)),
        "image_family": cfg.get("image_family", "auto"),
        "image_base_url": cfg.get("image_base_url") or None,
        "image_api_style": cfg.get("image_api_style", "auto"),
        "configured": bool(meta.get("configured")),
        "key_hint": meta.get("key_suffix"),
        "enabled": bool(profile.get("enabled", True)),
        "last_test": None,
    }


def _model_public(record: Any, engine: LocalInferenceEngine | None = None) -> dict[str, Any]:
    loaded = bool(record.loaded)
    return {
        "id": record.model_id,
        "name": record.name,
        "backend": record.backend.value,
        "architecture": record.architecture,
        "input_size": list(record.input_size) if isinstance(record.input_size, tuple) else record.input_size,
        "loaded": loaded,
        "device": engine.device if loaded and engine is not None else None,
        "memory_mb": round(float(record.estimated_memory_mb), 1) if loaded else None,
        "threshold": float(record.thresholds.get("default", 0.35)),
        "thresholds": dict(record.thresholds),
        "preset_thresholds": dict(record.preset_thresholds),
        "threshold_source": "model" if dict(record.thresholds) == dict(record.preset_thresholds) else "custom",
        "trusted_pickle": bool(record.trusted),
        "adapters": [{"id": value, "name": value, "type": value, "enabled": False, "weight": 1.0} for value in record.adapter_types],
        "classifiers": ["aesthetic"],
        "status": record.load_error,
    }


def _run_directory_scan(
    base: Path,
    root_id: str,
    *,
    recursive: bool,
    patterns: Sequence[str],
    cursor: int,
    page_size: int,
    max_items: int,
    image_extensions: frozenset[str] | set[str],
    allowlist: PathAllowlist,
    scan_id: str,
) -> dict[str, Any]:
    """Walk ``base`` and build one scan page. Blocking; run via to_thread."""
    regexes: list[re.Pattern[str]] = []
    for pattern in patterns:
        if len(pattern) > 128:
            raise _safe_error("过滤表达式过长", "invalid_pattern")
        expression = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        regexes.append(re.compile(expression, re.IGNORECASE))
    page: list[dict[str, Any]] = []
    total = 0
    iterator = base.rglob("*") if recursive else base.glob("*")
    for path in iterator:
        if total >= max_items:
            break
        if not path.is_file() or path.suffix.casefold() not in image_extensions:
            continue
        if regexes and not any(regex.match(path.name) or regex.match(path.as_posix()) for regex in regexes):
            continue
        rel = allowlist.relative_path(root_id, path)
        if cursor <= total < cursor + page_size:
            stat = path.stat()
            page.append({
                "id": opaque_id(path, prefix="image"),
                "relative_path": rel,
                "file_name": path.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            })
        total += 1
    end = min(cursor + len(page), total)
    return {
        "scan_id": scan_id,
        "items": page,
        "total": total,
        "next_cursor": str(end) if end < total else None,
    }


# Per-job fields that must not multiply cached VisionProvider instances.
# create_job freezes the requested ``online_concurrency`` into the snapshot's
# ``max_concurrency``; it is a job-scoped override, not provider identity.
_PROVIDER_VOLATILE_CONFIG_KEYS = ("max_concurrency", "concurrency")

# Upper bound on live cached VisionProvider instances (each owns an
# httpx.AsyncClient with a connection pool).
_PROVIDER_CACHE_LIMIT = 8


def _stable_provider_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return the durable parts of a provider profile for cache identity."""

    stable = dict(profile)
    config = dict(stable.get("config") or {})
    for key in _PROVIDER_VOLATILE_CONFIG_KEYS:
        config.pop(key, None)
    stable["config"] = config
    return stable


def create_app(settings: AppConfig | None = None) -> FastAPI:
    runtime = Runtime(settings or AppConfig.from_env())
    docs = None if runtime.settings.production else "/docs"

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime.settings.validate_runtime()
        # A process restart cannot leave a worker attached to queued/running
        # rows.  Mark those rows interrupted before serving requests so the
        # operator can explicitly recover them; review/paused states remain
        # durable operator decisions.
        runtime.workflow_database.mark_interrupted_jobs()
        await runtime.image_generation.start()
        yield
        await runtime.close()

    app = FastAPI(
        title="Tagger2 Inference Rebuild",
        version=__version__,
        docs_url=docs,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, Mapping) else {}
        message = str(detail.get("message") or exc.detail or "请求失败")
        content = _error_payload(
            request=request,
            code=str(detail.get("code") or f"http_{exc.status_code}"),
            message=message,
            fields=detail.get("fields") if isinstance(detail.get("fields"), Mapping) else None,
            retryable=bool(detail.get("retryable", False)),
        )
        return JSONResponse(status_code=exc.status_code, content=content, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, list[str]] = {}
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            fields.setdefault(location or "body", []).append(str(error.get("msg") or "invalid value"))
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                request=request,
                code="validation_error",
                message="请求参数校验失败",
                fields=fields,
            ),
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied_request_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", supplied_request_id)
            else uuid.uuid4().hex
        )
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except HTTPException:
            raise
        except SecurityError as exc:
            response = JSONResponse(
                status_code=400,
                content=_error_payload(
                    request=request,
                    code="security_error",
                    message=str(exc),
                ),
            )
        except ProviderError as exc:
            response = JSONResponse(
                status_code=502,
                content=_error_payload(
                    request=request,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                ),
            )
        except Exception:
            logger.exception(
                "unhandled server error [request_id=%s] %s %s",
                request_id,
                request.method,
                request.url.path,
            )
            response = JSONResponse(
                status_code=500,
                content=_error_payload(
                    request=request,
                    code="internal_error",
                    message="服务器内部错误",
                ),
            )
        response.headers["x-request-id"] = request_id
        return response

    async def authorize(request: Request) -> None:
        if not runtime.settings.allow_lan:
            return
        expected = os.getenv(runtime.settings.access_token_env, "").strip()
        # Tokens in query strings leak through browser history and access logs.
        supplied = request.headers.get("authorization")
        if not validate_bearer_token(expected, supplied):
            raise _safe_error("需要有效的访问 token", "unauthorized", False, 401)

    @app.get("/api/v1/health")
    async def health(_: None = Depends(authorize)):
        return {"status": "ok", "version": __version__, "models": len(runtime.registry.list()), "device": runtime.engine.device}

    @app.get("/api/v1/prompts/defaults")
    async def prompt_defaults(_: None = Depends(authorize)):
        return {
            "tag_prompt": DEFAULT_TAG_PROMPT,
            "nl_prompt": DEFAULT_NL_PROMPT,
            "json_prompt": DEFAULT_JSON_PROMPT,
        }

    @app.get("/api/v1/roots")
    async def roots(_: None = Depends(authorize)):
        return {"items": runtime.list_roots()}

    @app.post("/api/v1/roots")
    async def add_root(payload: RootCreate, _: None = Depends(authorize)):
        path = Path(payload.path).expanduser().resolve(strict=False)
        if not path.is_dir():
            raise _safe_error("目录不存在", "root_not_found")
        try:
            root = runtime.register_persistent_root(
                path, name=payload.name, kind=payload.kind
            )
        except SecurityError as exc:
            raise _safe_error(str(exc), "root_conflict", False, 409)
        return {"id": root.root_id, "name": root.label, "kind": root.kind, "writable": root.writable, "path_hint": "已授权"}

    @app.post("/api/v1/uploads")
    async def upload_files(files: list[UploadFile] = File(...), _: None = Depends(authorize)):
        if not files:
            raise _safe_error("至少上传一张图片", "upload_empty")
        upload_id = uuid.uuid4().hex
        target_dir = (runtime.settings.upload_dir or runtime.settings.project_root / "data" / "uploads") / upload_id
        target_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        artifact_names: set[str] = set()
        total = 0
        try:
            for index, upload in enumerate(files):
                data = await upload.read(runtime.settings.max_upload_bytes + 1)
                total += len(data)
                if total > runtime.settings.max_request_bytes:
                    raise _safe_error("请求体超过限制", "request_too_large")
                validate_image_bytes(data, max_bytes=runtime.settings.max_upload_bytes, max_pixels=runtime.settings.max_image_pixels, max_edge=runtime.settings.max_image_edge)
                filename = sanitize_filename(upload.filename or f"image-{index + 1}.png")
                path = target_dir / f"{index:04d}_{filename}"
                atomic_write_bytes(path, data)
                image_id = uuid.uuid4().hex
                artifact_name = filename
                duplicate = 2
                while artifact_name.casefold() in artifact_names:
                    artifact_name = numbered_name(filename, duplicate)
                    duplicate += 1
                artifact_names.add(artifact_name.casefold())
                records.append({
                    "id": image_id,
                    "name": filename,
                    "path": str(path),
                    "size": len(data),
                    "relative_path": filename,
                    "artifact_name": artifact_name,
                })
            with runtime._upload_lock:
                runtime.upload_index[upload_id] = records
                try:
                    runtime._save_upload_index()
                except Exception:
                    runtime.upload_index.pop(upload_id, None)
                    raise
        except BaseException:
            # The upload id owns a fresh directory, so a failed or cancelled
            # multipart request can remove it without touching another batch.
            await asyncio.to_thread(shutil.rmtree, target_dir, ignore_errors=True)
            raise
        return {"upload_id": upload_id, "files": [{"id": x["id"], "name": x["name"], "size": x["size"]} for x in records]}

    @app.post("/api/v1/video-prompts/generate")
    async def generate_video_prompt(
        images: list[UploadFile] = File(default=[]),
        image: UploadFile | None = File(default=None),
        provider_id: str = Form(...),
        provider_model: str | None = Form(default=None),
        instruction: str = Form(...),
        current_package_json: str | None = Form(default=None),
        prompt_mode: str = Form(default="ref2va"),
        fl2va_single_image_role: str = Form(default="first"),
        _: None = Depends(authorize),
    ):
        """Generate one stateless, structured image-to-video prompt package."""

        try:
            selected_mode = normalize_video_prompt_mode(prompt_mode)
        except ValueError as exc:
            raise _safe_error("提示词预设必须是 ref2va 或 fl2va", "invalid_prompt_mode") from exc

        uploads = list(images)
        if image is not None:
            if uploads:
                raise _safe_error("请使用 images 或兼容的 image 字段之一", "ambiguous_reference_images")
            uploads = [image]
        reference_image_count = len(uploads)
        base_mode = None
        if selected_mode == "ref2va":
            if not 1 <= reference_image_count <= 9:
                raise _safe_error("H3 Ref2VA 需要 1 到 9 张参考图片", "invalid_reference_image_count")
        else:
            try:
                single_image_role = normalize_fl2va_single_image_role(fl2va_single_image_role)
                base_mode = resolve_h3_base_mode(reference_image_count, single_image_role)
            except ValueError as exc:
                raise _safe_error("H3 FL2VA 最多支持两张图片，单图角色需为 first 或 last", "invalid_reference_image_count") from exc

        selected_provider = provider_id.strip()
        if not selected_provider:
            raise _safe_error("视频提示词需要选择 Provider", "provider_required")
        if len(selected_provider) > 128:
            raise _safe_error("Provider 标识过长", "invalid_provider_id")
        selected_model = (provider_model or "").strip()
        if len(selected_model) > 256:
            raise _safe_error("模型标识过长", "invalid_provider_model")
        clean_instruction = instruction.strip()
        if not clean_instruction:
            raise _safe_error("请输入生成要求", "instruction_required")
        if len(clean_instruction) > 8_000:
            raise _safe_error("生成要求过长", "instruction_too_long")

        current_package = None
        if current_package_json is not None and current_package_json.strip():
            if len(current_package_json.encode("utf-8")) > 64 * 1024:
                raise _safe_error("当前提示词版本过大", "current_package_too_large")
            try:
                current_package = parse_current_package_json(
                    current_package_json,
                    selected_mode,
                    base_mode,
                    reference_image_count=reference_image_count,
                )
            except ValueError as exc:
                raise _safe_error("当前提示词版本无效", "invalid_current_package", fields={"current_package_json": str(exc)}) from exc

        if selected_provider not in runtime.provider_configs and runtime.storage.get_provider_profile(selected_provider) is None:
            raise _safe_error("Provider 不存在", "provider_not_found", False, 404)
        image_data: list[bytes] = []
        for index, upload in enumerate(uploads, start=1):
            data = await upload.read(runtime.settings.max_upload_bytes + 1)
            try:
                validate_image_bytes(
                    data,
                    max_bytes=runtime.settings.max_upload_bytes,
                    max_pixels=runtime.settings.max_image_pixels,
                    max_edge=runtime.settings.max_image_edge,
                )
            except ValueError as exc:
                raise _safe_error(
                    f"第 {index} 张参考图片无效",
                    "invalid_reference_image",
                    fields={"images": str(exc)},
                ) from exc
            image_data.append(data)
        prompt = build_video_prompt_user_message(
            clean_instruction,
            current_package,
            selected_mode,
            reference_image_count=reference_image_count,
            base_mode=base_mode,
        )
        provider = runtime.provider(selected_provider)
        def validator(value: str) -> dict[str, Any]:
            package = parse_video_prompt_response(
                value,
                selected_mode,
                base_mode,
                reference_image_count=reference_image_count,
            )
            return package.model_dump(mode="json")
        response = await provider.generate(
            image_data,
            prompt,
            model=selected_model or None,
            validator=validator,
            system_prompt=build_video_prompt_system_prompt(
                selected_mode,
                reference_image_count=reference_image_count,
                base_mode=base_mode,
            ),
        )
        try:
            return parse_video_prompt_response(
                response,
                selected_mode,
                base_mode,
                reference_image_count=reference_image_count,
            ).model_dump(mode="json")
        except ValueError as exc:
            # VisionProvider validates and retries this before returning. Keep
            # the API contract stable if a custom provider bypasses validation.
            raise ProviderError(
                "provider returned an invalid video prompt package",
                code="provider_invalid_video_prompt",
            ) from exc

    @app.post("/api/v1/scans")
    async def scan(payload: ScanPayload, _: None = Depends(authorize)):
        root_id = payload.root_id
        relative = payload.relative_path
        patterns = [str(x).strip() for x in payload.patterns if str(x).strip()]
        page_size = min(runtime.settings.scan_page_size_max, payload.page_size)
        runtime.resolve_root(root_id, kind="input")
        base = runtime.allowlist.resolve(root_id, relative, must_exist=True, expect="dir")
        # The walk can touch tens of thousands of files with per-file stat
        # calls; keep it off the event loop. Root/path validation above still
        # raises the same HTTP errors from the async context.
        return await asyncio.to_thread(
            _run_directory_scan,
            base,
            root_id,
            recursive=payload.recursive,
            patterns=patterns,
            cursor=payload.cursor,
            page_size=page_size,
            max_items=runtime.settings.max_batch_items,
            image_extensions=runtime.settings.image_extensions,
            allowlist=runtime.allowlist,
            scan_id=uuid.uuid4().hex,
        )

    @app.get("/api/v1/models")
    async def models(_: None = Depends(authorize)):
        return {
            "items": [
                _model_public(record, runtime.engine)
                for record in runtime.registry.list()
                if record.tags
            ]
        }

    @app.post("/api/v1/models/downloads", status_code=202)
    async def start_model_download(payload: ModelDownloadPayload, _: None = Depends(authorize)):
        try:
            record = runtime.model_downloads.start(payload.url, payload.revision)
        except ValueError as exc:
            raise _safe_error(str(exc), "invalid_huggingface_url")
        return record.public()

    @app.get("/api/v1/models/downloads/{download_id}")
    async def model_download(download_id: str, _: None = Depends(authorize)):
        record = runtime.model_downloads.get(download_id)
        if record is None:
            raise _safe_error("下载任务不存在", "model_download_not_found", False, 404)
        return record.public()

    @app.get("/api/v1/classifiers")
    async def classifiers(_: None = Depends(authorize)):
        return {
            "items": [
                {"id": name, **runtime.classifiers[name].status()[name]}
                for name in ("aesthetic",)
            ]
        }

    @app.post("/api/v1/classifiers/{classifier_name}/load")
    async def load_classifier(classifier_name: str, _: None = Depends(authorize)):
        service = runtime.classifiers.get(classifier_name)
        if service is None:
            raise _safe_error("分类器不存在", "classifier_not_found", False, 404)
        await asyncio.to_thread(service.load, cast(ClassifierName, classifier_name))
        return {"id": classifier_name, **service.status()[classifier_name]}

    @app.post("/api/v1/classifiers/{classifier_name}/unload")
    async def unload_classifier(classifier_name: str, _: None = Depends(authorize)):
        service = runtime.classifiers.get(classifier_name)
        if service is None:
            raise _safe_error("分类器不存在", "classifier_not_found", False, 404)
        await asyncio.to_thread(service.unload, cast(ClassifierName, classifier_name))
        return {"id": classifier_name, **service.status()[classifier_name]}

    @app.post("/api/v1/models/{model_id}/load")
    async def load_model(model_id: str, payload: ModelLoadPayload, _: None = Depends(authorize)):
        record = runtime.registry.get(model_id)
        trusted = bool(payload.trusted_pickle or payload.allow_unsafe_pickle)
        try:
            if payload.device:
                requested_device = select_device(payload.device)
                if requested_device != runtime.engine.device:
                    if runtime.engine.loaded_model_ids:
                        raise ValueError("切换设备前请先卸载所有本地模型")
                    runtime.engine.device = requested_device
            adapter_path: Path | None = None
            if payload.adapter_type != "none":
                if payload.adapter_type not in record.adapter_types:
                    raise ValueError("模型未注册所选 Adapter 类型")
                if payload.adapter_path:
                    relative = Path(payload.adapter_path)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise PathNotAllowedError("Adapter 仅接受模型目录内的相对路径")
                    adapter_path = (record.path / relative).resolve(strict=False)
                    if not adapter_path.is_relative_to(record.path.resolve(strict=False)):
                        raise PathNotAllowedError("Adapter 路径越界")
                else:
                    adapter_path = record.path
            runtime.engine.load(
                model_id,
                adapter_type=payload.adapter_type,
                adapter_path=adapter_path,
                adapter_scale=payload.adapter_scale,
                allow_unsafe_pickle=trusted,
            )
            runtime.registry.mark_loaded(model_id, True)
        except UnsafeModelError as exc:
            raise _safe_error(str(exc), "unsafe_weights", False, 409)
        except Exception as exc:
            runtime.registry.mark_loaded(model_id, False, str(exc))
            raise _safe_error(str(exc), "model_load_failed", False, 400)
        return _model_public(record, runtime.engine)

    @app.post("/api/v1/models/{model_id}/unload")
    async def unload_model(model_id: str, _: None = Depends(authorize)):
        runtime.engine.unload(model_id)
        record = runtime.registry.get(model_id)
        return _model_public(record, runtime.engine)

    @app.patch("/api/v1/models/{model_id}")
    async def update_model(model_id: str, payload: ModelPatchPayload, _: None = Depends(authorize)):
        record = runtime.registry.get(model_id)
        if payload.trusted_pickle is not None:
            runtime.registry.trust(model_id, payload.trusted_pickle)
        if payload.reset_thresholds:
            record.set_thresholds(reset=True)
        elif payload.thresholds is not None:
            record.set_thresholds(payload.thresholds)
        elif payload.threshold is not None:
            record.set_thresholds({"default": payload.threshold})
        if dict(record.thresholds) == dict(record.preset_thresholds):
            runtime.storage.delete_model_profile(model_id)
        else:
            runtime.storage.upsert_model_profile(
                model_id,
                name=record.name,
                config={"thresholds": dict(record.thresholds)},
            )
        return _model_public(record, runtime.engine)

    @app.get("/api/v1/providers")
    async def providers(_: None = Depends(authorize)):
        return {"items": [_provider_public(runtime, profile) for profile in runtime.storage.list_provider_profiles()]}

    @app.post("/api/v1/providers")
    async def create_provider_route(payload: ProviderCreate, _: None = Depends(authorize)):
        kind = payload.kind.replace("lmstudio", "lm_studio").strip().lower()
        if kind not in {"custom", "openai", "xai", "gemini", "claude", "lm_studio", "antigravity"}:
            raise _safe_error("不支持的 provider 类型", "invalid_provider_kind")
        protocol = (payload.protocol or "openai").strip().lower()
        if kind != "custom":
            protocol = "gemini" if kind in {"gemini", "antigravity"} else "claude" if kind == "claude" else "openai"
        if protocol not in {"openai", "gemini", "claude"}:
            raise _safe_error("不支持的 API 协议", "invalid_provider_protocol")
        allow_local = kind in {"lm_studio", "antigravity"} or runtime.settings.allow_local_providers
        try:
            base = validate_provider_url(
                payload.base_url,
                allow_local=allow_local,
                resolve_dns=True,
            )
        except SecurityError as exc:
            raise _safe_error(str(exc), "invalid_provider_url")
        image_base = payload.image_base_url
        if image_base:
            try:
                image_base = validate_provider_url(
                    image_base,
                    allow_local=allow_local,
                    resolve_dns=True,
                )
            except SecurityError as exc:
                raise _safe_error(str(exc), "invalid_image_provider_url")
        pid = re.sub(r"[^a-z0-9_-]+", "-", payload.name.casefold()).strip("-") or uuid.uuid4().hex[:8]
        if pid in runtime.provider_configs:
            pid = f"{pid}-{uuid.uuid4().hex[:6]}"
        config = payload.model_dump(exclude={"name", "kind", "base_url", "enabled", "protocol"})
        config["image_base_url"] = image_base
        config["protocol"] = protocol
        profile = runtime.storage.upsert_provider_profile(pid, name=payload.name, kind=kind, base_url=base, config=config, secret_ref=f"provider_{pid}", enabled=payload.enabled)
        runtime.provider_configs[pid] = profile
        return _provider_public(runtime, profile)

    @app.patch("/api/v1/providers/{provider_id}")
    async def patch_provider(provider_id: str, payload: ProviderPatch, _: None = Depends(authorize)):
        current = runtime.storage.get_provider_profile(provider_id)
        if current is None:
            raise _safe_error("provider 不存在", "provider_not_found", False, 404)
        body = payload.model_dump(exclude_unset=True)
        requested_kind = body.pop("kind", None)
        kind = str(requested_kind or current.get("kind")).replace("lmstudio", "lm_studio").strip().lower()
        if kind not in {"custom", "openai", "xai", "gemini", "claude", "lm_studio", "antigravity"}:
            raise _safe_error("不支持的 provider 类型", "invalid_provider_kind")
        requested_protocol = body.pop("protocol", None)
        protocol = str(requested_protocol or (current.get("config") or {}).get("protocol") or "openai").strip().lower()
        if kind != "custom":
            protocol = "gemini" if kind in {"gemini", "antigravity"} else "claude" if kind == "claude" else "openai"
        if protocol not in {"openai", "gemini", "claude"}:
            raise _safe_error("不支持的 API 协议", "invalid_provider_protocol")
        base = body.pop("base_url", current.get("base_url"))
        try:
            base = validate_provider_url(
                str(base),
                allow_local=kind in {"lm_studio", "antigravity"}
                or runtime.settings.allow_local_providers,
                resolve_dns=True,
            )
        except SecurityError as exc:
            raise _safe_error(str(exc), "invalid_provider_url")
        config = dict(current.get("config") or {})
        config["protocol"] = protocol
        mapping = {"primary_model": "primary_model", "fallback_model": "fallback_model", "temperature": "temperature", "top_p": "top_p", "top_k": "top_k", "max_tokens": "max_tokens", "timeout_seconds": "timeout_seconds", "retries": "retries"}
        name = body.pop("name", current.get("name"))
        enabled = body.pop("enabled", current.get("enabled", True))
        if "image_base_url" in body:
            image_base = body.pop("image_base_url")
            if image_base:
                try:
                    image_base = validate_provider_url(
                        str(image_base),
                        allow_local=kind in {"lm_studio", "antigravity"} or runtime.settings.allow_local_providers,
                        resolve_dns=True,
                    )
                except SecurityError as exc:
                    raise _safe_error(str(exc), "invalid_image_provider_url")
                config["image_base_url"] = image_base
            else:
                config.pop("image_base_url", None)
        config.update({mapping[key]: value for key, value in body.items() if key in mapping})
        for key in ("image_enabled", "image_family", "image_api_style"):
            if key in body and body[key] is not None:
                config[key] = body[key]
        profile = runtime.storage.upsert_provider_profile(provider_id, name=name, kind=kind, base_url=base, config=config, secret_ref=current.get("secret_ref"), enabled=enabled)
        runtime.provider_configs[provider_id] = profile
        await runtime.invalidate_provider(provider_id)
        return _provider_public(runtime, profile)

    @app.delete("/api/v1/providers/{provider_id}", status_code=204)
    async def delete_provider(provider_id: str, _: None = Depends(authorize)):
        current = runtime.storage.get_provider_profile(provider_id)
        if current is None:
            raise _safe_error("provider 不存在", "provider_not_found", False, 404)
        await runtime.invalidate_provider(provider_id)
        secret_ref = current.get("secret_ref") or f"provider_{provider_id}"
        metadata = get_secret_metadata(runtime.secrets, secret_ref)
        if metadata.get("source") == "keyring":
            try:
                runtime.secrets.delete(secret_ref)
            except SecretStoreUnavailable as exc:
                raise _safe_error(str(exc), "secret_store_unavailable", False, 503)
        runtime.storage.delete_provider_profile(provider_id)
        runtime.provider_configs.pop(provider_id, None)
        return Response(status_code=204)

    @app.post("/api/v1/providers/{provider_id}/secret")
    async def set_provider_secret(provider_id: str, payload: SecretPayload, _: None = Depends(authorize)):
        profile = runtime.storage.get_provider_profile(provider_id)
        if profile is None:
            raise _safe_error("provider 不存在", "provider_not_found", False, 404)
        ref = profile.get("secret_ref") or f"provider_{provider_id}"
        try:
            runtime.secrets.set_many(ref, payload.keys)
        except SecretStoreUnavailable as exc:
            raise _safe_error(str(exc), "secret_store_unavailable", False, 503)
        await runtime.invalidate_provider(provider_id)
        meta = get_secret_metadata(runtime.secrets, ref)
        return {"configured": bool(meta.get("configured")), "key_hint": meta.get("key_suffix")}

    @app.post("/api/v1/providers/{provider_id}/test")
    async def test_provider(provider_id: str, request: Request, _: None = Depends(authorize)):
        start = time.perf_counter()
        try:
            result = await runtime.provider(provider_id).test()
            return {"ok": True, "message": f"连接成功，可用模型 {len(result.get('models', []))}", "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
        except ProviderError as exc:
            return JSONResponse(
                status_code=502,
                content=_error_payload(
                    request=request,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                ),
            )
        except Exception:
            logger.exception("provider %s connection test failed", provider_id)
            return JSONResponse(
                status_code=502,
                content=_error_payload(
                    request=request,
                    code="provider_test_failed",
                    message="provider 连接测试失败",
                ),
            )

    @app.get("/api/v1/providers/{provider_id}/models")
    async def provider_models(provider_id: str, _: None = Depends(authorize)):
        values = await runtime.provider(provider_id).discover_models()
        return {"items": [{"id": value, "name": value} for value in values]}

    @app.post("/api/v1/providers/discover-models")
    async def discover_unsaved_provider_models(
        payload: ProviderDiscovery,
        request: Request,
        _: None = Depends(authorize),
    ):
        kind = payload.kind.replace("lmstudio", "lm_studio").strip().lower()
        if kind not in {"custom", "openai", "xai", "gemini", "claude", "lm_studio", "antigravity"}:
            raise _safe_error("不支持的 provider 类型", "invalid_provider_kind")
        protocol = (payload.protocol or "openai").strip().lower()
        if kind != "custom":
            protocol = "gemini" if kind in {"gemini", "antigravity"} else "claude" if kind == "claude" else "openai"
        if protocol not in {"openai", "gemini", "claude"}:
            raise _safe_error("不支持的 API 协议", "invalid_provider_protocol")
        allow_local = kind in {"lm_studio", "antigravity"} or runtime.settings.allow_local_providers
        try:
            base_url = validate_provider_url(
                payload.base_url,
                allow_local=allow_local,
                resolve_dns=True,
            )
            keys = tuple(dict.fromkeys(key.strip() for key in payload.api_keys if key.strip()))
            provider = create_provider({
                "id": "unsaved-discovery",
                "name": "unsaved-discovery",
                "kind": kind,
                "protocol": protocol,
                "base_url": base_url,
                "model": "discovery",
                "api_keys": keys,
                "timeout_seconds": payload.timeout_seconds,
                "max_concurrency": 1,
                "allow_local": allow_local,
            })
            try:
                values = await provider.discover_models()
            finally:
                await provider.aclose()
        except SecurityError as exc:
            raise _safe_error(str(exc), "invalid_provider_url")
        except ProviderError as exc:
            return JSONResponse(
                status_code=502,
                content=_error_payload(
                    request=request,
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                ),
            )
        except Exception:
            return JSONResponse(
                status_code=502,
                content=_error_payload(
                    request=request,
                    code="provider_discovery_failed",
                    message="获取可用模型失败",
                ),
            )
        return {"items": [{"id": value, "name": value} for value in values]}

    def _chunks(values: Iterable[dict[str, Any]], size: int = 500):
        iterator = iter(values)
        while batch := list(itertools.islice(iterator, size)):
            yield batch

    @app.post("/api/v1/jobs")
    async def create_job(payload: JobCreate, _: None = Depends(authorize)):
        if payload.hybrid and payload.mode != "local":
            raise _safe_error(
                "本地 + 在线任务必须使用本地模式创建",
                "invalid_hybrid_mode",
            )
        if (
            payload.mode != "online"
            and payload.online_response is not None
            and not payload.hybrid
        ):
            raise _safe_error(
                "本地任务不支持在线响应格式",
                "invalid_online_response",
            )
        if payload.hybrid and payload.online_response not in {"nl", "json"}:
            raise _safe_error(
                "本地 + 在线任务需要选择 NL 或 Anima JSON 响应",
                "invalid_online_response",
            )
        if payload.hybrid and not payload.output.txt:
            raise _safe_error(
                "本地 + 在线任务必须输出本地 TAG TXT",
                "hybrid_txt_required",
            )
        if (
            payload.online_response in {"nl", "nl_tags"}
            and payload.output.json_output
        ):
            raise _safe_error(
                "NL 响应不能用于生成 Anima JSON 文件",
                "invalid_online_response",
            )
        if payload.hybrid and payload.online_response == "json" and not payload.output.json_output:
            raise _safe_error(
                "Anima JSON 模式必须输出 JSON 文件",
                "hybrid_json_required",
            )
        unknown_classifiers = set(payload.classifiers) - set(runtime.classifiers)
        if unknown_classifiers:
            raise _safe_error("包含未知分类器", "classifier_not_found")
        if payload.mode != "local" and payload.classifiers:
            raise _safe_error("在线任务不支持本地分类器", "invalid_classifier_mode")
        if payload.source.type == "scan":
            if payload.output.root_id:
                runtime.resolve_root(payload.output.root_id, kind="output", writable=True)
        elif payload.output.root_id:
            runtime.resolve_root(payload.output.root_id, kind="output", writable=True)
        items, source_root = runtime.processors.build_job_items(payload.source)
        model_ids = list(payload.model_ids)
        if payload.mode == "local" and not model_ids:
            model_ids = [record.model_id for record in runtime.registry.list() if record.tags]
        if payload.mode == "local":
            if not model_ids:
                raise _safe_error("没有可用本地打标模型", "model_required")
            for model_id in model_ids:
                runtime.registry.get(model_id)
        needs_provider = payload.mode == "online" or payload.hybrid
        if needs_provider and not payload.provider_id:
            raise _safe_error("在线任务需要 provider", "provider_required")
        provider_profile: Mapping[str, Any] | None = None
        if needs_provider:
            # Validate provider before creating a persistent job.
            runtime.provider(payload.provider_id or "")
            provider_profile = runtime.provider_configs.get(payload.provider_id or "")
        config = payload.model_dump(mode="json", by_alias=True)
        config["source"] = payload.source.model_dump(mode="json", by_alias=True)
        config["output"] = payload.output.model_dump(mode="json", by_alias=True)
        if payload.mode == "local" and not payload.hybrid:
            # Local jobs only write the flat TXT artifact.  Upload/workbench
            # jobs may still disable TXT entirely because their result is web-only.
            config["output"]["json"] = False
        elif payload.hybrid:
            # Hybrid NL owns the sole TXT; hybrid JSON keeps local TAG TXT and
            # online Anima JSON under the same source stem.
            config["output"]["txt"] = True
            config["output"]["txt_include_tags"] = False
            config["output"]["json"] = payload.online_response == "json"
        config["model_ids"] = model_ids
        config["provider_id"] = payload.provider_id
        config["prompt"] = payload.prompt or DEFAULT_PROMPT
        if provider_profile:
            provider_config = provider_profile.get("config") or {}
            snapshot_config = dict(provider_config)
            snapshot_config.pop("concurrency", None)
            snapshot_config["max_concurrency"] = payload.online_concurrency
            config["provider_snapshot"] = {
                "id": provider_profile.get("id"),
                "name": provider_profile.get("name"),
                "kind": provider_profile.get("kind"),
                "base_url": provider_profile.get("base_url"),
                "enabled": bool(provider_profile.get("enabled", True)),
                "config": snapshot_config,
            }
            config["_worker_concurrency"] = min(
                runtime.settings.max_online_concurrency,
                payload.online_concurrency,
            )
        # Building scan items walks the source directory and the chunked
        # inserts hit SQLite; both block, so run the whole persistence step in
        # a worker thread. Root/path validation and provider validation above
        # already ran on the event loop and keep raising their HTTP errors
        # from the async context; errors raised inside the thread propagate
        # unchanged through the await below.
        def _persist_job() -> JobRecord:
            if payload.source.type == "scan":
                iterator = iter(items)
                try:
                    first = next(iterator)
                except StopIteration:
                    raise _safe_error("没有找到可处理的图片", "no_images")
                record = runtime.storage.create_job(
                    payload.mode,
                    config,
                    (),
                    source_root_id=source_root,
                    output_root_id=payload.output.root_id,
                )
                for chunk in _chunks(itertools.chain((first,), iterator)):
                    runtime.storage.add_items(record.id, chunk)
                refreshed = runtime.storage.get_job(record.id)
                if refreshed is not None:
                    return refreshed
                return record
            upload_items = list(items)
            if not upload_items:
                raise _safe_error("没有找到可处理的图片", "no_images")
            return runtime.storage.create_job(
                payload.mode,
                config,
                upload_items,
                source_root_id=source_root,
                output_root_id=payload.output.root_id,
            )

        record = await asyncio.to_thread(_persist_job)
        await runtime.job_manager.start(record.id)
        return _job_public(record)

    @app.get("/api/v1/jobs")
    async def list_jobs(limit: int = 50, _: None = Depends(authorize)):
        values = runtime.storage.list_jobs(limit=max(1, min(1000, limit)))
        return {"items": [_job_public(value) for value in values], "total": len(values)}

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str, _: None = Depends(authorize)):
        record = runtime.storage.get_job(job_id)
        if record is None:
            raise _safe_error("任务不存在", "job_not_found", False, 404)
        return _job_public(record)

    @app.get("/api/v1/jobs/{job_id}/results")
    async def job_results(job_id: str, _: None = Depends(authorize)):
        if runtime.storage.get_job(job_id) is None:
            raise _safe_error("任务不存在", "job_not_found", False, 404)
        result_items: list[dict[str, Any]] = []
        for item in runtime.storage.list_items(job_id, limit=runtime.settings.max_batch_items):
            result = dict(item.result or {})
            result.setdefault("image_id", item.image_id)
            result.setdefault("file_name", Path(item.relative_path).name)
            result.setdefault("status", item.status)
            result.setdefault("tags", [])
            result.setdefault("artifacts", [])
            result.setdefault("warnings", [])
            result.setdefault("timing", {"total_ms": item.duration_ms} if item.duration_ms is not None else {})
            if item.error:
                result["error"] = item.error
            result_items.append(result)
        return {"items": result_items, "total": len(result_items)}

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, _: None = Depends(authorize)):
        if runtime.storage.get_job(job_id) is None:
            raise _safe_error("任务不存在", "job_not_found", False, 404)
        try:
            last = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            last = 0

        async def stream() -> AsyncIterator[str]:
            async for event in runtime.job_manager.event_stream(job_id, last_event_id=last):
                if await request.is_disconnected():
                    break
                seq = int(event.get("seq", 0))
                yield f"id: {seq}\nevent: job\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def action(job_id: str, action_name: str):
        try:
            if action_name == "pause":
                record = await runtime.job_manager.pause(job_id)
            elif action_name == "resume":
                record = await runtime.job_manager.resume(job_id)
            elif action_name == "cancel":
                record = await runtime.job_manager.cancel(job_id)
            else:
                count = await runtime.job_manager.retry_failed(job_id, start=True)
                retry_record = runtime.storage.get_job(job_id)
                if retry_record is None:
                    raise KeyError(job_id)
                if count == 0:
                    raise ValueError("没有可重试的失败项")
                record = retry_record
            return _job_public(record)
        except KeyError:
            raise _safe_error("任务不存在", "job_not_found", False, 404)
        except ValueError as exc:
            raise _safe_error(str(exc), "invalid_job_action")

    for action_name in ("pause", "resume", "cancel", "retry-failed"):
        async def route(job_id: str, request: Request, _action: str = action_name, _: None = Depends(authorize)):
            return await action(job_id, _action)
        app.add_api_route(f"/api/v1/jobs/{{job_id}}/{action_name}", route, methods=["POST"], name=f"job_{action_name}")

    @app.get("/api/v1/settings")
    async def get_settings(_: None = Depends(authorize)):
        value = {
            "input_root_id": None,
            "output_root_id": None,
            "default_mode": "online",
            "default_threshold": 0.35,
            "default_json": True,
            "default_txt": False,
            "bind_host": runtime.settings.host,
            "lan_enabled": runtime.settings.allow_lan,
            "access_token_configured": runtime.settings.access_token_configured,
            "production": runtime.settings.production,
            "max_upload_mb": runtime.settings.max_upload_bytes // (1024 * 1024),
            "max_image_pixels": runtime.settings.max_image_pixels,
        }
        stored = runtime.read_settings_document()
        value.update({key: stored[key] for key in value if key in stored and key not in {"bind_host", "lan_enabled", "access_token_configured", "production"}})
        return value

    @app.put("/api/v1/settings")
    async def put_settings(payload: SettingsPayload, _: None = Depends(authorize)):
        data = payload.model_dump(mode="json")
        data.pop("bind_host", None)
        data.pop("lan_enabled", None)
        data.pop("production", None)
        runtime.save_user_settings(data)
        return await get_settings()

    # Dataset Workflow module.  Mounted before the SPA catch-all so the
    # frontend route cannot shadow it, and behind the same authorize dependency
    # as every other API route.
    app.include_router(
        create_workflow_router(
            allowlist=runtime.allowlist,
            resource_catalog=runtime.workflow_resources,
            database=runtime.workflow_database,
            model_registry=runtime.registry,
            inference_engine=runtime.engine,
            storage=runtime.storage,
            root_registrar=runtime.register_persistent_root,
        ),
        dependencies=[Depends(authorize)],
    )
    app.include_router(
        create_image_generation_router(runtime.image_generation),
        dependencies=[Depends(authorize)],
    )
    # Tag manager module.  Same mounting rules: before the SPA catch-all and
    # behind the shared authorize dependency.
    from .tag_manager.api import create_tag_manager_router

    app.include_router(
        create_tag_manager_router(runtime.tag_manager),
        dependencies=[Depends(authorize)],
    )

    frontend_dist = runtime.settings.project_root / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets"), check_dir=False), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            if path.startswith("api/"):
                raise HTTPException(status_code=404)
            candidate = (frontend_dist / path).resolve(strict=False)
            if candidate.is_file() and candidate.is_relative_to(frontend_dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app


settings = AppConfig.from_env()
app = create_app(settings)


def main() -> None:
    import uvicorn

    uvicorn.run("tagger2.main:app", host=settings.host, port=settings.port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
