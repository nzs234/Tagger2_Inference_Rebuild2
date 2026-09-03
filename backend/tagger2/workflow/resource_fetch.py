"""On-demand fetching of content-addressed workflow resources.

The portable package ships resource manifests and small data tables but not
the model-class blobs (classification snapshots, tokenizer packs): those
download on first use from the pinned release-asset host and are verified
against the manifest fingerprint before any consumer touches them. Downloads
are size-checked, streamed to a ``.part`` file and moved into place
atomically, so a failed or interrupted fetch never leaves a half-written
resource behind.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .resources import WorkflowResourceCatalog

RESOURCE_ASSET_BASE_ENV = "TAGGER2_RESOURCE_ASSET_BASE"
DEFAULT_RESOURCE_ASSET_BASE = (
    "https://github.com/nzs234/Tagger2_Inference_Rebuild2/releases/download/resources-v1"
)


class ResourceFetchError(RuntimeError):
    """Raised when a resource cannot be fetched or fails verification."""

    def __init__(self, message: str, *, resource_id: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.resource_id = resource_id
        self.retryable = retryable


@dataclass
class ResourceFetchState:
    """Progress of one resource's fetch, shared across caller threads."""

    state: str = "idle"  # downloading | ready | error
    received: int = 0
    total: int = 0
    path: Path | None = None
    error: str = ""
    done: threading.Event = field(default_factory=threading.Event)

    @property
    def percent(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, round(self.received / self.total * 100))

    def progress_text(self) -> str:
        if self.state == "downloading":
            return f"后台下载中 {self.percent}%（{self.received / 1e6:.1f}/{self.total / 1e6:.1f} MB）"
        if self.state == "ready":
            return "完成"
        return self.error or "失败"


class ResourceFetchManager:
    """Downloads catalog resources on first use; one background thread each."""

    def __init__(
        self,
        catalog: WorkflowResourceCatalog,
        *,
        base_url: str | None = None,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        self._catalog = catalog
        configured = base_url or os.environ.get(RESOURCE_ASSET_BASE_ENV) or DEFAULT_RESOURCE_ASSET_BASE
        self._base_url = configured.rstrip("/")
        self._client_factory = client_factory or self._default_client_factory
        self._states: dict[str, ResourceFetchState] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    @staticmethod
    def _default_client_factory() -> httpx.Client:
        # Release assets redirect to the CDN, and slow links need a generous
        # read timeout between 1 MiB chunks.
        return httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=120.0),
        )

    def asset_url(self, resource_id: str) -> str:
        manifest = self._require_manifest(resource_id)
        return self._asset_url_for(manifest)

    def _asset_url_for(self, manifest: Any) -> str:
        filename = f"{manifest.resource_id}.{manifest.resource_fingerprint[:16]}"
        return f"{self._base_url}/{filename}"

    def _require_manifest(self, resource_id: str) -> Any:
        manifest = self._catalog.get_manifest(resource_id)
        if manifest is None:
            raise ResourceFetchError(
                f"资源清单缺失，无法下载：{resource_id}",
                resource_id=resource_id,
                retryable=False,
            )
        return manifest

    def state_for(self, resource_id: str) -> ResourceFetchState | None:
        """Current fetch state, or None when this resource was never started."""

        with self._registry_lock:
            return self._states.get(resource_id)

    def get_or_start(self, resource_id: str) -> ResourceFetchState:
        """Return a ready state with the path, or start a background download."""

        path = self._catalog.get_resource_path(resource_id)
        if path is not None:
            return ResourceFetchState(state="ready", path=path)
        with self._registry_lock:
            state = self._states.get(resource_id)
            if state is None or (state.state == "error" and state.done.is_set()):
                state = ResourceFetchState(state="downloading")
                self._states[resource_id] = state
                thread = threading.Thread(
                    target=self._download,
                    args=(resource_id, state),
                    daemon=True,
                    name=f"resource-fetch-{resource_id}",
                )
                thread.start()
            return state

    def ensure(self, resource_id: str, *, timeout: float = 900.0) -> Path:
        """Blocking variant for job execution paths: wait until usable."""

        state = self.get_or_start(resource_id)
        if not state.done.wait(timeout):
            raise ResourceFetchError(
                f"资源下载超时：{resource_id}", resource_id=resource_id
            )
        if state.state != "ready" or state.path is None:
            raise ResourceFetchError(
                state.error or f"资源下载失败：{resource_id}", resource_id=resource_id
            )
        return state.path

    def _download(self, resource_id: str, state: ResourceFetchState) -> None:
        lock = self._lock_for(resource_id)
        with lock:
            part_path: Path | None = None
            try:
                existing = self._catalog.get_resource_path(resource_id)
                if existing is not None:
                    state.path = existing
                    state.state = "ready"
                    return
                manifest = self._require_manifest(resource_id)
                target_dir = self._catalog.resource_dir / manifest.category
                target_dir.mkdir(parents=True, exist_ok=True)
                final_path = target_dir / f"{resource_id}.{manifest.resource_fingerprint[:16]}"
                part_path = Path(str(final_path) + ".part")
                state.total = int(manifest.size_bytes)
                with self._client_factory() as client:
                    with client.stream("GET", self._asset_url_for(manifest)) as response:
                        response.raise_for_status()
                        with part_path.open("wb") as handle:
                            for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                                handle.write(chunk)
                                state.received = handle.tell()
                if state.received != int(manifest.size_bytes):
                    raise ResourceFetchError(
                        f"资源大小不符：期望 {manifest.size_bytes} 字节，实际 {state.received}",
                        resource_id=resource_id,
                    )
                if self._catalog.fingerprint_file(part_path) != manifest.resource_fingerprint:
                    raise ResourceFetchError(
                        f"资源指纹校验失败：{resource_id}", resource_id=resource_id
                    )
                part_path.replace(final_path)
                state.path = final_path
                state.state = "ready"
            except Exception as exc:  # noqa: BLE001 - the state carries the detail
                state.state = "error"
                state.error = str(exc) or exc.__class__.__name__
                if part_path is not None:
                    try:
                        part_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            finally:
                state.done.set()

    def _lock_for(self, resource_id: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks.setdefault(resource_id, threading.Lock())


_MANAGERS: dict[str, ResourceFetchManager] = {}
_MANAGERS_LOCK = threading.Lock()


def manager_for(catalog: WorkflowResourceCatalog) -> ResourceFetchManager:
    """Shared manager per resource directory, mirroring the tag-db cache."""

    key = os.path.normcase(str(catalog.resource_dir))
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = ResourceFetchManager(catalog)
            _MANAGERS[key] = manager
        return manager
