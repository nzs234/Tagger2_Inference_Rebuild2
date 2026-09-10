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
import posixpath
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

# Seconds an index scan waits for the session write lock before giving up and
# recording error/session_busy on the session row.  A module constant so the
# observability contract (a rescan that lost the lock race is visible to
# clients polling the session) stays testable without a 10 s wait.
INDEX_LOCK_TIMEOUT_SECONDS = 10.0

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


def _session_relative(session: Mapping[str, Any], relative: str) -> str:
    """Join the session's dataset prefix onto a dataset-relative path.

    Image rows and journalled sidecar paths are stored relative to the
    session's dataset directory, while the allowlist resolves against the
    registered root.  A session opened at root + ``sub`` must therefore
    resolve ``img.jpg`` as ``sub/img.jpg``.  The join happens on the read
    side so existing database rows and undo-journal entries (all stored
    prefix-less) stay valid without a data migration.
    """

    prefix = str(session.get("relative_path") or "").strip().replace("\\", "/").strip("/")
    if not prefix or prefix == ".":
        return relative
    return posixpath.join(prefix, relative)


def resolve_image(
    allowlist: PathAllowlist, session: Mapping[str, Any], image: Mapping[str, Any]
) -> Path:
    try:
        return allowlist.resolve(
            str(session["root_id"]),
            _session_relative(session, str(image["relative_path"])),
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
    *,
    for_write: bool = True,
) -> Path:
    # ``for_write`` defaults to True because every historical caller was a
    # write; the batch preview resolves the same path to read it and passes
    # False so a read-only dataset root can still be previewed.
    # ``sidecar_path`` is relative to the session's dataset directory (the
    # same shape the undo journal stores), so the session prefix is applied
    # exactly like ``resolve_image`` does.
    try:
        return allowlist.resolve(
            str(session["root_id"]),
            _session_relative(session, sidecar_path),
            must_exist=False,
            for_write=for_write,
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
        scheduler: IndexScheduler | None = None,
    ) -> None:
        self.store = store
        self.allowlist = allowlist
        self.tag_database = tag_database
        self._locks = locks
        # The scheduler registers one cancel event per scheduled scan; without
        # it (direct construction in tests) scans simply run to completion.
        self._scheduler = scheduler

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
        session = require_session(self.store, session_id)
        # Surface the undo/redo availability on the detail response so the
        # toolbar can render enabled/disabled buttons without polling the
        # journal; the flags flip with every edit/undo/redo.
        session["can_undo"] = self.store.has_journal_entry(session_id, undone=False)
        session["can_redo"] = self.store.has_journal_entry(session_id, undone=True)
        return session

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

    def cancel_scan(self, session_id: str) -> bool:
        """Signal a running (or queued) index scan for this session to stop.

        Returns whether a scan slot was found.  Cancelling is a normal user
        action, not an error: the scan stops at the next image boundary,
        keeps the rows it already indexed and ends in status ``ready``.  With
        no scan in flight the call is a harmless no-op (``False``).
        """

        # Unknown sessions must surface as 404, not as a silent success.
        require_session(self.store, session_id)
        if self._scheduler is None:
            return False
        return self._scheduler.cancel(session_id)

    def index_session(self, session_id: str) -> None:
        """Scan the dataset directory and rebuild the index (blocking)."""

        lock = self._locks.lock(session_id)
        # A bounded wait instead of a silent skip: a rescan that lost the race
        # against a short write still happens, while a stuck lock fails loudly
        # in the log rather than reporting a refresh that never ran.
        if not lock.acquire(timeout=INDEX_LOCK_TIMEOUT_SECONDS):
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
            # The progress counter restarts at zero with every scan.
            self.store.update_session(session_id, status="indexing", scanned_count=0)
            # Registered synchronously by the scheduler when the scan was
            # queued; a scan that started outside the scheduler runs to
            # completion (cancel_event is None).
            cancel_event = (
                self._scheduler.cancel_event(session_id)
                if self._scheduler is not None
                else None
            )
            self._index_session_locked(session, cancel_event=cancel_event)
        except TagManagerError as exc:
            logger.warning("tag manager index failed for %s: %s", session_id, exc)
            self.store.update_session(
                session_id, status="error", error=exc.code, scanned_count=0
            )
        except Exception:  # noqa: BLE001 - scan must never crash the app
            logger.exception("tag manager index crashed for %s", session_id)
            self.store.update_session(
                session_id, status="error", error="index_failed", scanned_count=0
            )
        finally:
            lock.release()

    def _index_session_locked(
        self,
        session: Mapping[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> None:
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
        processed = 0
        cancelled = False
        for image_path in _iter_images(dataset_dir, recursive):
            # Cancel is cooperative: the scan stops at the next image
            # boundary, keeps every row it already indexed and skips the
            # pruning pass that only a complete scan may run.
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            processed += 1
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
            # Progress is written at chunk boundaries (every processed image,
            # unchanged ones included) so a long scan is observable from the
            # session row without paying a database write per file.
            if processed % SCAN_CHUNK == 0:
                self.store.update_session(session_id, scanned_count=processed)
        if pending:
            self._flush_rows(session_id, pending)
        if cancelled:
            # A cancelled scan is not an error and must not prune: rows for
            # files the interrupted pass never reached stay in the index and
            # the next complete rescan reconciles them.
            self.store.update_session(
                session_id,
                status="ready",
                error=None,
                image_count=len(keep_paths),
                scanned_count=0,
            )
            return
        self.store.prune_images_missing(session_id, keep_paths)
        self.store.update_session(
            session_id,
            status="ready",
            error=None,
            image_count=len(keep_paths),
            scanned_count=0,
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

    Each scheduled scan also owns a ``threading.Event`` registered here at
    schedule time (synchronously with the future): a running scan observes it
    on its worker thread and stops at the next image boundary, which is the
    only safe way to stop a scan that is already past the queue.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._guard = threading.Lock()

    @property
    def futures(self) -> dict[str, asyncio.Future[None]]:
        """The live scan-future map (kept for the facade's compat surface)."""

        return self._futures

    def cancel_event(self, session_id: str) -> threading.Event | None:
        """The cancel event of the session's scheduled scan, if any."""

        with self._guard:
            return self._cancel_events.get(session_id)

    def cancel(self, session_id: str) -> bool:
        """Signal the session's running (or queued) scan to stop.

        Returns whether a scan slot was found; with no scan scheduled this is
        an idempotent no-op.  The event is only set here — observing it and
        settling the session row is the scan's own job on its worker thread.
        """

        with self._guard:
            event = self._cancel_events.get(session_id)
        if event is None:
            return False
        event.set()
        return True

    def _release_event(self, session_id: str, event: threading.Event) -> None:
        # Identity guard: a newer scan for the same session may already own
        # the slot, and only the scan that registered it may release it.
        with self._guard:
            if self._cancel_events.get(session_id) is event:
                self._cancel_events.pop(session_id, None)

    def schedule(self, session_id: str, run: Callable[[str], None]) -> None:
        """Run one blocking scan on a worker thread (non-blocking caller)."""

        event = threading.Event()
        with self._guard:
            self._cancel_events[session_id] = event
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop: the historical synchronous path runs the scan
            # inline and releases its own event slot when it returns.
            try:
                run(session_id)
            finally:
                self._release_event(session_id, event)
            return
        try:
            future = loop.run_in_executor(None, run, session_id)
        except BaseException:
            self._release_event(session_id, event)
            raise
        with self._guard:
            self._futures[session_id] = future
        future.add_done_callback(functools.partial(self.finish, session_id))

    def discard(self, session_id: str) -> None:
        """Drop the session's scan slot, cancelling a queued (not started) scan.

        Safe to call from any thread: delete_session runs on a worker thread
        once the API offloads it, and ``asyncio.Future.cancel`` is not
        thread-safe, so the cancel is marshalled onto the loop that owns the
        future.  The cancel event is dropped together with the future: a scan
        discarded while queued can never observe it.
        """

        with self._guard:
            future = self._futures.pop(session_id, None)
            self._cancel_events.pop(session_id, None)
        if future is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is future.get_loop():
            future.cancel()
            return
        try:
            future.get_loop().call_soon_threadsafe(future.cancel)
        except RuntimeError:
            # The owning loop is already closed: nothing can run the scan.
            pass

    def finish(self, session_id: str, future: asyncio.Future[None]) -> None:
        """Forget a finished scan future and log any exception it escaped.

        ``index_session`` records its own failures on the session row, so this
        only catches errors that escape it entirely (a crashed executor, a
        cancelled scan) which would otherwise vanish with the discarded
        ``run_in_executor`` future.
        """

        with self._guard:
            # A newer scan may already be registered for this session; only
            # the future that owns the slot removes it (and its event).
            if self._futures.get(session_id) is future:
                self._futures.pop(session_id, None)
                self._cancel_events.pop(session_id, None)
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
    "INDEX_LOCK_TIMEOUT_SECONDS",
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
