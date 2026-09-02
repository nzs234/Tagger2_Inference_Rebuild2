"""Background Hugging Face model downloads with strict repository URL parsing."""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from huggingface_hub import snapshot_download

from .common import utc_now
from .model_registry import ModelRegistry, ModelRegistryError


_REPO_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_TERMINAL = frozenset({"succeeded", "failed"})


def parse_huggingface_url(value: str) -> tuple[str, str | None]:
    """Return ``(repo_id, revision)`` for a canonical Hugging Face model URL."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("请输入 Hugging Face 模型地址")
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or (parts.hostname or "").casefold() not in {
        "huggingface.co",
        "www.huggingface.co",
    }:
        raise ValueError("仅支持 https://huggingface.co/ 模型地址")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("模型地址不能包含凭据、查询参数或片段")
    segments = [unquote(item).strip() for item in parts.path.split("/") if item.strip()]
    if len(segments) < 2:
        raise ValueError("模型地址必须包含作者和仓库名")
    owner, name = segments[:2]
    if name.endswith(".git"):
        name = name[:-4]
    if not _REPO_PART.fullmatch(owner) or not _REPO_PART.fullmatch(name):
        raise ValueError("Hugging Face 仓库名称无效")
    revision: str | None = None
    if len(segments) > 2:
        if segments[2] != "tree" or len(segments) < 4:
            raise ValueError("请输入仓库主页或 /tree/<revision> 地址")
        revision = "/".join(segments[3:])
        if not _REVISION.fullmatch(revision) or ".." in revision.split("/"):
            raise ValueError("Hugging Face revision 无效")
    return f"{owner}/{name}", revision


@dataclass(slots=True)
class ModelDownload:
    id: str
    repo_id: str
    revision: str | None
    status: str = "queued"
    phase: str = "queued"
    model_ids: list[str] = field(default_factory=list)
    loaded_model_ids: list[str] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "status": self.status,
            "phase": self.phase,
            "model_ids": list(self.model_ids),
            "loaded_model_ids": list(self.loaded_model_ids),
            "load_errors": list(self.load_errors),
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class ModelDownloadManager:
    def __init__(
        self,
        model_root: Path,
        registry: ModelRegistry,
        *,
        loader: Callable[[str], None] | None = None,
    ) -> None:
        self.model_root = model_root.resolve(strict=False)
        self.registry = registry
        self.loader = loader
        self._records: dict[str, ModelDownload] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = threading.RLock()
        self._download_lock = asyncio.Lock()

    def get(self, download_id: str) -> ModelDownload | None:
        with self._lock:
            return self._records.get(download_id)

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def start(self, url: str, revision: str | None = None) -> ModelDownload:
        repo_id, url_revision = parse_huggingface_url(url)
        selected_revision = str(revision).strip() if revision else url_revision
        if selected_revision and (
            not _REVISION.fullmatch(selected_revision) or ".." in selected_revision.split("/")
        ):
            raise ValueError("Hugging Face revision 无效")
        record = ModelDownload(id=f"download_{uuid.uuid4().hex}", repo_id=repo_id, revision=selected_revision)
        with self._lock:
            self._prune()
            duplicate = next(
                (
                    item
                    for item in self._records.values()
                    if item.repo_id.casefold() == repo_id.casefold()
                    and item.revision == selected_revision
                    and item.status not in _TERMINAL
                ),
                None,
            )
            if duplicate is not None:
                return duplicate
            self._records[record.id] = record
        task = asyncio.create_task(self._run(record.id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    def _prune(self) -> None:
        terminal = sorted(
            (item for item in self._records.values() if item.status in _TERMINAL),
            key=lambda item: item.updated_at,
        )
        for item in terminal[:-40]:
            self._records.pop(item.id, None)

    def _update(self, download_id: str, **values: Any) -> None:
        with self._lock:
            record = self._records[download_id]
            for key, value in values.items():
                setattr(record, key, value)
            record.updated_at = utc_now()

    async def _run(self, download_id: str) -> None:
        record = self.get(download_id)
        if record is None:
            return
        async with self._download_lock:
            self._update(download_id, status="running", phase="downloading")
            target = self.model_root / record.repo_id.replace("/", "__")
            try:
                if target.is_symlink():
                    raise ModelRegistryError("模型下载目录不能是符号链接")
                await asyncio.to_thread(
                    snapshot_download,
                    repo_id=record.repo_id,
                    revision=record.revision,
                    local_dir=target,
                )
                if await asyncio.to_thread(
                    lambda: any(path.is_symlink() for path in target.rglob("*"))
                ):
                    raise ModelRegistryError("下载仓库包含不允许的符号链接")
                self._update(download_id, phase="registering")
                discovered = []
                try:
                    discovered.append(self.registry.register(target))
                except ModelRegistryError:
                    pass
                discovered.extend(self.registry.discover())
                model_ids = list(
                    dict.fromkeys(
                        item.model_id
                        for item in discovered
                        if item.path.resolve(strict=False).is_relative_to(target.resolve(strict=False))
                        and item.tags
                    )
                )
                if not model_ids:
                    raise ModelRegistryError("仓库已下载，但未检测到受支持的模型权重")
                loaded_model_ids: list[str] = []
                load_errors: list[str] = []
                if self.loader is not None:
                    self._update(download_id, phase="loading")
                    for model_id in model_ids:
                        try:
                            await asyncio.to_thread(self.loader, model_id)
                            loaded_model_ids.append(model_id)
                        except Exception as exc:
                            load_errors.append(f"{model_id}: {type(exc).__name__}")
                self._update(
                    download_id,
                    status="succeeded",
                    phase="completed",
                    model_ids=model_ids,
                    loaded_model_ids=loaded_model_ids,
                    load_errors=load_errors,
                    error=("模型已下载，但部分模型自动加载失败" if load_errors else None),
                )
            except asyncio.CancelledError:
                self._update(download_id, status="failed", phase="interrupted", error="下载因服务停止而中断")
                raise
            except ModelRegistryError as exc:
                self._update(download_id, status="failed", phase="registering", error=str(exc))
            except Exception as exc:
                self._update(
                    download_id,
                    status="failed",
                    phase="downloading",
                    error=f"下载失败，请检查仓库地址、网络或访问权限（{type(exc).__name__}）",
                )


__all__ = ["ModelDownload", "ModelDownloadManager", "parse_huggingface_url"]
