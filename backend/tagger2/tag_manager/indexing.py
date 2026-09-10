"""Dataset session CRUD and the incremental index scan for the tag manager.

Sessions are opened against an allowlisted dataset directory and scanned into
the SQLite index.  Rescans are incremental: a scanned file whose image mtime
and winning sidecar mtime still match the recorded index row skips the sidecar
parse and the image header probe entirely — the row stays in the database and
only ``keep_paths`` bookkeeping runs for it.

This module also hosts the primitives shared with ``editing``: the per-session
write locks, the allowlist path resolvers and the small "row must exist"
raisers, so the two collaborators stay import-cycle free (editing -> indexing
only).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from PIL import Image

from ..security import (
    PathAllowlist,
    PathNotAllowedError,
)
from ..workflow.dataset_import import SUPPORTED_EXTENSIONS
from .contracts import CreateDatasetRequest
from .errors import TagManagerError
from .protocols import TagDatabaseClient
from .sidecar_io import (
    SidecarContent,
    SidecarError,
    load_sidecar,
)
from .storage import TagManagerStore

logger = logging.getLogger("tagger2.tag_manager")

SCAN_CHUNK = 500

# One indexed row as the incremental rescan baseline:
# relative_path -> (image mtime, sidecar mtime, sidecar kind).
IndexedRowState = tuple[float, float | None, str]


class SessionLocks:
    """Per-session write locks shared by editing and indexing.

    save/batch/undo/redo and the index scan of one dataset session serialize
    through a single ``threading.Lock`` per session id so concurrent requests
    can never interleave read-modify-write spans on the same sidecars.  The
    instance lives on the facade; both collaborators receive it at
    construction.
    """

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock(self, session_id: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    @contextmanager
    def exclusive(self, session_id: str) -> Iterator[None]:
        """Serialize the mutating operations of one dataset session.

        ``threading.Lock`` is not reentrant: wrap exactly the public entry
        points, never nested helpers.
        """

        lock = self.lock(session_id)
        if not lock.acquire(blocking=False):
            raise TagManagerError(
                "another write or scan is already running for this dataset",
                code="session_busy",
                status_code=409,
                retryable=True,
            )
        try:
            yield
        finally:
            lock.release()

    def discard(self, session_id: str) -> None:
        """Forget one session's lock after the session was deleted."""

        with self._guard:
            self._locks.pop(session_id, None)


# -- path and store guards ---------------------------------------------


def resolve_dataset_dir(allowlist: PathAllowlist, root_id: str, relative_path: str) -> Path:
    try:
        resolved = allowlist.resolve(root_id, relative_path, must_exist=True, expect="dir")
    except PathNotAllowedError as exc:
        raise TagManagerError(
            "dataset path is not allowed",
            code="path_not_allowed",
            status_code=403,
        ) from exc
    return resolved


def resolve_image(
    allowlist: PathAllowlist, session: Mapping[str, Any], image: Mapping[str, Any]
) -> Path:
    try:
        return allowlist.resolve(
            str(session["root_id"]),
            str(image["relative_path"]),
            must_exist=True,
            expect="file",
        )
    except PathNotAllowedError as exc:
        raise TagManagerError(
            "image path is not allowed",
            code="path_not_allowed",
            status_code=403,
        ) from exc


def resolve_sidecar(
    allowlist: PathAllowlist,
    session: Mapping[str, Any],
    image: Mapping[str, Any],
    sidecar_path: str,
) -> Path:
    try:
        return allowlist.resolve(
            str(session["root_id"]),
            sidecar_path,
            must_exist=False,
            for_write=True,
            expect="file",
        )
    except PathNotAllowedError as exc:
        raise TagManagerError(
            "sidecar path is not allowed",
            code="path_not_allowed",
            status_code=403,
        ) from exc


def require_session(store: TagManagerStore, session_id: str) -> dict[str, Any]:
    session = store.get_session(session_id)
    if session is None:
        raise TagManagerError(
            "dataset session not found",
            code="dataset_not_found",
            status_code=404,
        )
    return session


def require_image(store: TagManagerStore, session_id: str, image_id: int) -> dict[str, Any]:
    image = store.get_image(session_id, image_id)
    if image is None:
        raise TagManagerError(
            "image not found",
            code="image_not_found",
            status_code=404,
        )
    return image


def stat_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# -- category resolution ------------------------------------------------


class CategoryResolver:
    """Resolves tag categories through the tag database with caching."""

    def __init__(self, tag_database: TagDatabaseClient | None, profile: str) -> None:
        self._tag_database = tag_database
        self._profile = profile
        self._cache: dict[str, str] = {}
        self._available = False
        if tag_database is not None:
            try:
                self._available = tag_database.is_loaded(profile)
            except Exception:  # noqa: BLE001 - enrichment is best effort
                self._available = False

    def category_for(self, tag: str) -> str:
        key = tag.casefold()
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        category = "general"
        tag_database = self._tag_database
        if tag_database is not None:
            if self._available:
                info = tag_database.lookup(self._profile, tag)
                if info is not None:
                    category = str(info["category"])
            else:
                try:
                    info = tag_database.lookup(self._profile, tag)
                except Exception:  # noqa: BLE001 - enrichment is best effort
                    info = None
                if info is not None:
                    category = str(info["category"])
                    self._available = True
        self._cache[key] = category
        return category

    def categorize(self, tags: tuple[str, ...] | list[str]) -> list[tuple[str, str]]:
        return [(tag, self.category_for(tag)) for tag in tags]


# -- index scan ---------------------------------------------------------


class SessionIndexer:
    """Session CRUD plus the (incremental) scan of one dataset directory."""

    def __init__(
        self,
        *,
        store: TagManagerStore,
        allowlist: PathAllowlist,
        tag_database: TagDatabaseClient | None,
        locks: SessionLocks,
    ) -> None:
        self.store = store
        self.allowlist = allowlist
        self.tag_database = tag_database
        self._locks = locks

    def create_session(self, request: CreateDatasetRequest) -> dict[str, Any]:
        dataset_dir = resolve_dataset_dir(self.allowlist, request.root_id, request.relative_path)
        del dataset_dir  # validated eagerly so bad paths fail before indexing
        session_id = uuid.uuid4().hex
        return self.store.create_session(
            {
                "id": session_id,
                "name": request.name,
                "root_id": request.root_id,
                "relative_path": request.relative_path,
                "profile": request.profile,
                "recursive": request.recursive,
            }
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return require_session(self.store, session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.store.list_sessions()

    def delete_session(self, session_id: str) -> None:
        # Serialize deletion with every write/scan using the same stable lock.
        # The lock is intentionally retained after deletion so an already
        # queued worker cannot be replaced by a fresh lock object.
        with self._locks.exclusive(session_id):
            if not self.store.delete_session(session_id):
                raise TagManagerError(
                    "dataset session not found",
                    code="dataset_not_found",
                    status_code=404,
                )

    def index_session(self, session_id: str) -> None:
        """Scan the dataset directory and rebuild the index (blocking)."""

        lock = self._locks.lock(session_id)
        # A bounded wait instead of a silent skip: a rescan that lost the race
        # against a short write still happens, while a stuck lock fails loudly
        # in the log rather than reporting a refresh that never ran.
        if not lock.acquire(timeout=10):
            logger.warning(
                "tag manager index busy for %s: another write or scan is"
                " still holding the session lock",
                session_id,
            )
            try:
                self.store.update_session(session_id, status="error", error="session_busy")
            except TagManagerError:
                pass
            return
        try:
            session = require_session(self.store, session_id)
            # Observable transition before scanning: clients polling after a
            # refresh see the session leave 'ready' while the rescan runs
            # (create_session already stored 'indexing'; rewriting is harmless).
            self.store.update_session(session_id, status="indexing")
            self._index_session_locked(session)
        except TagManagerError as exc:
            logger.warning("tag manager index failed for %s: %s", session_id, exc)
            self.store.update_session(session_id, status="error", error=exc.code)
        except Exception:  # noqa: BLE001 - scan must never crash the app
            logger.exception("tag manager index crashed for %s", session_id)
            self.store.update_session(session_id, status="error", error="index_failed")
        finally:
            lock.release()

    def _index_session_locked(self, session: Mapping[str, Any]) -> None:
        session_id = str(session["id"])
        dataset_dir = resolve_dataset_dir(
            self.allowlist, str(session["root_id"]), str(session["relative_path"])
        )
        recursive = bool(session["recursive"])
        profile = str(session["profile"])
        categories = CategoryResolver(self.tag_database, profile)

        # Incremental baseline: existing rows keyed by relative path, so files
        # whose image and sidecar mtimes still match skip the sidecar parse and
        # the image header probe entirely.
        baseline = self.store.scan_state(session_id)

        keep_paths: set[str] = set()
        pending: list[dict[str, Any]] = []
        for image_path in _iter_images(dataset_dir, recursive):
            relative = image_path.relative_to(dataset_dir).as_posix()
            keep_paths.add(relative)
            prior = baseline.get(relative)
            if prior is not None and _row_is_current(image_path, prior):
                continue  # the row in the database is already current
            row, tags = _index_row(image_path, relative, categories)
            row["_tags"] = tags
            pending.append(row)
            if len(pending) >= SCAN_CHUNK:
                self._flush_rows(session_id, pending)
                pending = []
        if pending:
            self._flush_rows(session_id, pending)
        self.store.prune_images_missing(session_id, keep_paths)
        self.store.update_session(
            session_id, status="ready", error=None, image_count=len(keep_paths)
        )

    def _flush_rows(self, session_id: str, rows: list[dict[str, Any]]) -> int:
        # One transaction per scan chunk instead of one per image keeps large
        # datasets from paying a connection and a commit per sidecar.
        return self.store.upsert_rows(session_id, rows)


class IndexScheduler:
    """Tracks index scans scheduled from the async path, keyed by session id.

    A deleted session can cancel a queued scan this way, and escapees of
    ``index_session``'s own error handling still get logged.  These are the
    asyncio wrappers returned by run_in_executor; cancelling one also cancels
    the underlying executor future.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[None]] = {}
        self._guard = threading.Lock()

    @property
    def futures(self) -> dict[str, asyncio.Future[None]]:
        """The live scan-future map (kept for the facade's compat surface)."""

        return self._futures

    def schedule(self, session_id: str, run: Callable[[str], None]) -> None:
        """Run one blocking scan on a worker thread (non-blocking caller)."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            run(session_id)
            return
        future = loop.run_in_executor(None, run, session_id)
        with self._guard:
            self._futures[session_id] = future
        future.add_done_callback(functools.partial(self.finish, session_id))

    def discard(self, session_id: str) -> None:
        """Drop the session's scan slot, cancelling a queued (not started) scan."""

        with self._guard:
            future = self._futures.pop(session_id, None)
        if future is not None:
            future.cancel()

    def finish(self, session_id: str, future: asyncio.Future[None]) -> None:
        """Forget a finished scan future and log any exception it escaped.

        ``index_session`` records its own failures on the session row, so this
        only catches errors that escape it entirely (a crashed executor, a
        cancelled scan) which would otherwise vanish with the discarded
        ``run_in_executor`` future.
        """

        with self._guard:
            # A newer scan may already be registered for this session; only
            # the future that owns the slot removes it.
            if self._futures.get(session_id) is future:
                self._futures.pop(session_id, None)
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.warning(
                "tag manager index future for %s failed: %s",
                session_id,
                exc,
                exc_info=exc,
            )


def _iter_images(root: Path, recursive: bool) -> list[Path]:
    images: list[Path] = []
    if recursive:
        for current, directories, files in os.walk(root):
            directories.sort()
            for name in sorted(files):
                candidate = Path(current) / name
                if candidate.suffix.casefold() in SUPPORTED_EXTENSIONS:
                    images.append(candidate)
    else:
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.suffix.casefold() in SUPPORTED_EXTENSIONS:
                images.append(entry)
    return images


def _row_is_current(image_path: Path, prior: IndexedRowState) -> bool:
    """Return whether the indexed row for one image is still up to date.

    A row is reused only when the image file and its winning sidecar carry the
    exact ``st_mtime`` floats recorded at the last scan (SQLite REAL columns
    round-trip doubles losslessly) and the sidecar kind is unchanged.  A sidecar
    appearing or disappearing therefore always rescans, and the untouched row
    keeps its image id and tag rows.
    """

    indexed_mtime, indexed_sidecar_mtime, indexed_kind = prior
    if stat_mtime(image_path) != indexed_mtime:
        return False
    txt_path = image_path.with_suffix(".txt")
    json_path = image_path.with_suffix(".json")
    if indexed_kind == "none":
        # Any sidecar showing up is a change (a blank txt still parses as none,
        # but re-parsing it is the honest and rare path).
        return not json_path.is_file() and not txt_path.is_file()
    if indexed_kind == "tag_txt":
        # JSON wins over TXT on a re-parse, so a new json sibling must rescan;
        # a deleted txt is a change too.
        if json_path.is_file() or not txt_path.is_file():
            return False
        return stat_mtime(txt_path) == indexed_sidecar_mtime
    # JSON-based kinds: only the json sibling feeds the parser, so a txt
    # sibling appearing beside it cannot change the parsed content.
    if not json_path.is_file():
        return False
    return stat_mtime(json_path) == indexed_sidecar_mtime


def _index_row(
    image_path: Path, relative: str, categories: CategoryResolver
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Build one index row plus its categorized tags (cheap header probe)."""

    width: int | None = None
    height: int | None = None
    image_format = image_path.suffix.casefold().lstrip(".")
    try:
        with Image.open(image_path) as opened:
            image_format = (opened.format or image_format).lower()
            width, height = opened.size
    except Exception:  # noqa: BLE001 - unreadable images stay listed, not decoded
        width = None
        height = None

    txt_path = image_path.with_suffix(".txt")
    json_path = image_path.with_suffix(".json")
    sidecar_rel: str | None = None
    sidecar_mtime: float | None = None
    try:
        content = load_sidecar(
            txt_path if txt_path.is_file() else None,
            json_path if json_path.is_file() else None,
        )
    except SidecarError:
        content = SidecarContent(kind="none")
        sidecar_rel = None
    if content.kind != "none":
        sidecar_rel = relative[: -len(image_path.suffix)] + (
            ".txt" if content.kind == "tag_txt" else ".json"
        )
        sidecar_mtime = stat_mtime(txt_path if content.kind == "tag_txt" else json_path)

    row = {
        "relative_path": relative,
        "file_name": image_path.name,
        "image_format": image_format,
        "sidecar_kind": content.kind,
        "sidecar_path": sidecar_rel,
        "mtime": stat_mtime(image_path) or 0.0,
        "sidecar_mtime": sidecar_mtime,
        "width": width,
        "height": height,
        "tag_count": len(content.tags),
    }
    return row, categories.categorize(content.tags)


__all__ = [
    "SCAN_CHUNK",
    "CategoryResolver",
    "IndexScheduler",
    "IndexedRowState",
    "SessionIndexer",
    "SessionLocks",
    "require_image",
    "require_session",
    "resolve_dataset_dir",
    "resolve_image",
    "resolve_sidecar",
    "stat_mtime",
]
