"""Tag manager service facade: dataset sessions, tag editing, batch ops, undo/redo.

The service is the only component that touches dataset sidecars.  Every write
is atomic, validated against the sidecar kind recorded at scan time and
journalled so batch operations can be undone.  Path access goes through the
shared ``PathAllowlist``; responses never contain absolute paths.

The implementation lives in focused collaborator modules — ``indexing``
(session CRUD, per-session locks and the incremental index scan), ``editing``
(saves, batch operations, undo/redo replay and the sidecar payload helpers)
and ``online_translation`` (NL and tag translation through online providers).
This class wires them together, owns the scheduled index futures and keeps the
historical surface for ``api.py``, ``main.py`` and the tests: every pre-split
method and attribute name stays available as a one-line delegation.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..security import PathAllowlist
from . import online_translation
from .contracts import (
    BatchOperationRequest,
    CreateDatasetRequest,
    ImageEditRequest,
    ImageFilter,
    NlTranslateRequest,
    TagTranslateRequest,
    TranslationLookupRequest,
)
from .editing import (
    SessionEditor,
    assert_sidecar_not_stale,
    content_payload,
    content_tag_strings,
    load_content_and_mtime,
    require_writable_root,
    sidecar_paths,
)
from .errors import TagManagerError
from .indexing import (
    CategoryResolver,
    IndexScheduler,
    SessionIndexer,
    SessionLocks,
    require_image,
    require_session,
    resolve_dataset_dir,
    resolve_image,
    resolve_sidecar,
)
from .protocols import (
    ProviderFactory,
    ProviderIds,
    TagDatabaseClient,
    ThumbnailProvider,
    TranslationProvider,
)
from .sidecar_io import SidecarContent
from .storage import TagManagerStore
from .tag_db import TagDatabaseError
from .translations import TagTranslations


class TagManagerService:
    """Facade over the tag manager store, sidecar IO and the tag database."""

    def __init__(
        self,
        *,
        store: TagManagerStore,
        allowlist: PathAllowlist,
        thumbnails: ThumbnailProvider,
        tag_database: TagDatabaseClient,
        translations: TagTranslations | None = None,
        provider_factory: ProviderFactory | None = None,
        provider_ids: ProviderIds | None = None,
    ) -> None:
        self.store = store
        self.allowlist = allowlist
        self.thumbnails = thumbnails
        self.tag_database = tag_database
        # The dictionaries ship with the app, so the default instance is the
        # committed one; tests point at their own directory.
        self.translations = translations if translations is not None else TagTranslations()
        # NL translation borrows the app's configured online providers; both
        # hooks stay optional so the service is usable without them.
        self._provider_factory = provider_factory
        self._provider_ids = provider_ids
        # One lock set shared by the editing and indexing collaborators; the
        # scheduler owns the asyncio futures of scans queued from the async path.
        self._locks = SessionLocks()
        self._scheduler = IndexScheduler()
        self._indexer = SessionIndexer(
            store=store,
            allowlist=allowlist,
            tag_database=tag_database,
            locks=self._locks,
        )
        self._editor = SessionEditor(
            store=store,
            allowlist=allowlist,
            tag_database=tag_database,
            locks=self._locks,
        )

    # -- compatibility surface for the split internals ----------------------

    @property
    def _index_futures(self) -> dict[str, asyncio.Future[None]]:
        """Scan futures keyed by session id (the scheduler's live map)."""

        return self._scheduler.futures

    # -- path helpers ------------------------------------------------------

    def _resolve_dataset_dir(self, root_id: str, relative_path: str) -> Path:
        return resolve_dataset_dir(self.allowlist, root_id, relative_path)

    def _resolve_image(self, session: Mapping[str, Any], image: Mapping[str, Any]) -> Path:
        return resolve_image(self.allowlist, session, image)

    def _resolve_sidecar(
        self, session: Mapping[str, Any], image: Mapping[str, Any], sidecar_path: str
    ) -> Path:
        return resolve_sidecar(self.allowlist, session, image, sidecar_path)

    def _session_lock(self, session_id: str) -> threading.Lock:
        return self._locks.lock(session_id)

    @contextmanager
    def _exclusive_session(self, session_id: str) -> Iterator[None]:
        with self._locks.exclusive(session_id):
            yield

    def _require_session(self, session_id: str) -> dict[str, Any]:
        return require_session(self.store, session_id)

    def _require_image(self, session_id: str, image_id: int) -> dict[str, Any]:
        return require_image(self.store, session_id, image_id)

    # -- sessions ----------------------------------------------------------

    def create_session(self, request: CreateDatasetRequest) -> dict[str, Any]:
        return self._indexer.create_session(request)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._indexer.get_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self._indexer.list_sessions()

    def delete_session(self, session_id: str) -> None:
        self._indexer.delete_session(session_id)
        # Cancel queued work after the lock-protected delete. A worker that has
        # already started cannot pass the same lock until deletion completes.
        self._scheduler.discard(session_id)

    def schedule_index(self, session_id: str) -> None:
        """Run :meth:`index_session` on a worker thread (non-blocking caller)."""

        # Resolved at call time so a substituted index_session (tests) is used.
        self._scheduler.schedule(session_id, self.index_session)

    def _finish_index_future(self, session_id: str, future: asyncio.Future[None]) -> None:
        self._scheduler.finish(session_id, future)

    def index_session(self, session_id: str) -> None:
        """Scan the dataset directory and rebuild the index (blocking)."""

        self._indexer.index_session(session_id)

    def _index_session_locked(self, session: Mapping[str, Any]) -> None:
        self._indexer._index_session_locked(session)

    def _flush_rows(self, session_id: str, rows: list[dict[str, Any]]) -> int:
        return self._indexer._flush_rows(session_id, rows)

    def refresh_session(self, session_id: str) -> dict[str, Any]:
        self._require_session(session_id)
        # Fail honestly when a write or scan is in flight: silently skipping
        # the rescan while reporting one would leave the client waiting for an
        # indexing transition that never happens.
        lock = self._session_lock(session_id)
        if not lock.acquire(blocking=False):
            raise TagManagerError(
                "another write or scan is already running for this dataset",
                code="session_busy",
                status_code=409,
                retryable=True,
            )
        # The probe must release before scheduling: the lock is not reentrant
        # and a synchronous schedule_index runs index_session inline, which
        # would find its own probe still holding the lock and skip the scan.
        # (Scheduling only queues the worker; a later async worker is covered
        # by index_session's bounded wait.)
        lock.release()
        self.schedule_index(session_id)
        return self.store.get_session(session_id) or {}

    # -- browsing ----------------------------------------------------------

    def list_images(
        self,
        session_id: str,
        *,
        image_filter: ImageFilter | None = None,
        sort: str = "name",
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        session = self._require_session(session_id)
        image_filter = image_filter or ImageFilter()
        items, total = self.store.list_images(
            session_id,
            include_tags=list(image_filter.include_tags),
            exclude_tags=list(image_filter.exclude_tags),
            include_mode=image_filter.include_mode,
            kind=image_filter.kind,
            sidecar=image_filter.sidecar,
            sort=sort,
            offset=offset,
            limit=min(max(limit, 1), 1000),
        )
        tags_by_image = self.store.image_tags([int(item["id"]) for item in items])
        profile = str(session["profile"])
        for item in items:
            item["tags"] = self._annotate_tags(profile, tags_by_image.get(int(item["id"]), []))
        return {"items": items, "total": total}

    def get_image(self, session_id: str, image_id: int) -> dict[str, Any]:
        session = self._require_session(session_id)
        image = self._require_image(session_id, image_id)
        profile = str(session["profile"])
        tags = self.store.image_tags([int(image["id"])]).get(int(image["id"]), [])
        image["tags"] = self._annotate_tags(profile, tags)
        content, live_mtime = load_content_and_mtime(
            paths=sidecar_paths(self.allowlist, session, image)
        )
        image["content"] = content_payload(content)
        image["sidecar_mtime"] = live_mtime
        # The editor renders the sidecar's own tag strings, which can differ
        # from the indexed rows (nine-field documents carry several lists), so
        # ship one translation map covering everything the drawer will show.
        image["translations"] = self.translations.translate_many(
            profile, content_tag_strings(content)
        )
        return image

    def thumbnail(self, session_id: str, image_id: int, *, size: int) -> Path:
        session = self._require_session(session_id)
        image = self._require_image(session_id, image_id)
        source = self._resolve_image(session, image)
        try:
            return self.thumbnails.ensure_thumbnail(source, size=size, mtime=float(image["mtime"]))
        except Exception as exc:
            raise TagManagerError(
                "thumbnail generation failed",
                code="thumbnail_failed",
                status_code=500,
                retryable=True,
            ) from exc

    # -- editing -----------------------------------------------------------

    def _require_writable_root(self, session: Mapping[str, Any]) -> None:
        require_writable_root(self.allowlist, session)

    def save_image(self, session_id: str, image_id: int, edit: ImageEditRequest) -> dict[str, Any]:
        return self._editor.save_image(session_id, image_id, edit)

    def _save_image_locked(
        self, session: Mapping[str, Any], image_id: int, edit: ImageEditRequest
    ) -> dict[str, Any]:
        return self._editor._save_image_locked(session, image_id, edit)

    @staticmethod
    def _assert_sidecar_not_stale(
        expected_mtime: float | None,
        current_mtime: float | None,
        image: Mapping[str, Any],
    ) -> None:
        assert_sidecar_not_stale(expected_mtime, current_mtime, image)

    def batch_operation(self, session_id: str, request: BatchOperationRequest) -> dict[str, Any]:
        return self._editor.batch_operation(session_id, request)

    def _append_batch_journal(
        self,
        session_id: str,
        request: BatchOperationRequest,
        changes: list[dict[str, Any]],
        *,
        partial: bool = False,
    ) -> int:
        return self._editor._append_batch_journal(session_id, request, changes, partial=partial)

    def _resolve_targets(
        self, session_id: str, request: BatchOperationRequest
    ) -> list[dict[str, Any]]:
        return self._editor._resolve_targets(session_id, request)

    def _apply_batch_to_image(
        self,
        session: Mapping[str, Any],
        image: Mapping[str, Any],
        request: BatchOperationRequest,
        categories: CategoryResolver,
    ) -> dict[str, Any] | None:
        return self._editor._apply_batch_to_image(session, image, request, categories)

    # -- undo / redo -------------------------------------------------------

    def undo(self, session_id: str) -> dict[str, Any]:
        return self._editor.undo(session_id)

    def redo(self, session_id: str) -> dict[str, Any]:
        return self._editor.redo(session_id)

    def _replay_changes(
        self,
        session_id: str,
        changes: list[Mapping[str, Any]],
        *,
        use: str,
        entry_id: int,
    ) -> None:
        self._editor._replay_changes(session_id, changes, use=use, entry_id=entry_id)

    @staticmethod
    def _journal_slot_kind(change: Mapping[str, Any], entry_id: int, sidecar_rel: str) -> str:
        return SessionEditor._journal_slot_kind(change, entry_id, sidecar_rel)

    @staticmethod
    def _assert_replay_matches(
        sidecar_path: Path,
        change: Mapping[str, Any],
        *,
        other: str,
        slot_kind: str,
    ) -> None:
        SessionEditor._assert_replay_matches(sidecar_path, change, other=other, slot_kind=slot_kind)

    @staticmethod
    def _assert_journal_text_valid(text: str, slot_kind: str, sidecar_rel: str) -> None:
        SessionEditor._assert_journal_text_valid(text, slot_kind, sidecar_rel)

    # -- stats / autocomplete ----------------------------------------------

    def tag_stats(self, session_id: str, *, limit: int = 200, min_count: int = 1) -> list[dict[str, Any]]:
        session = self._require_session(session_id)
        rows = self.store.tag_stats(
            session_id, limit=min(max(limit, 1), 1000), min_count=max(min_count, 1)
        )
        return self._annotate_tags(str(session["profile"]), rows)

    def autocomplete(self, profile: str, query: str, *, limit: int = 20, resource_id: str | None = None) -> dict[str, Any]:
        try:
            self.tag_database.ensure_loaded(profile, resource_id=resource_id)
        except TagDatabaseError as exc:
            # A missing snapshot is a setup state, not a server fault: danbooru
            # autocomplete needs scripts/import_classification_snapshot.py.
            raise TagManagerError(
                f"标签库未就绪：{exc}",
                code="tag_db_unavailable",
                status_code=409,
            ) from exc
        items = self.tag_database.autocomplete(profile, query, limit=min(max(limit, 1), 50))
        # Fresh dicts per item: the tag database exposes read-only mappings,
        # so the translation is attached without rewriting its rows.
        return {
            "profile": profile,
            "items": [
                {**item, "translation": self.translations.translate(profile, str(item["name"]))}
                for item in items
            ],
        }

    def tag_db_info(self) -> dict[str, Any]:
        return {
            "available": self.tag_database.available_profiles(),
            "loaded": {
                profile: self.tag_database.is_loaded(profile)
                for profile in ("e621", "danbooru")
            },
            "translations": self.translations.info(),
        }

    def lookup_translations(self, request: TranslationLookupRequest) -> dict[str, Any]:
        """Resolve Chinese names for an explicit tag batch."""

        return {
            "profile": request.profile,
            "translations": self.translations.translate_many(request.profile, request.tags),
        }

    # -- online translation -------------------------------------------------

    def _resolve_provider(
        self, explicit_provider_id: str | None, unavailable_code: str
    ) -> tuple[str, TranslationProvider]:
        return online_translation.resolve_provider(
            self._provider_factory, self._provider_ids, explicit_provider_id, unavailable_code
        )

    def _first_provider_id(self) -> str:
        return online_translation.first_provider_id(self._provider_ids)

    async def translate_nl(self, request: NlTranslateRequest) -> dict[str, Any]:
        """Translate one NL caption with a configured online provider."""

        return await online_translation.translate_nl(
            self._provider_factory, self._provider_ids, request
        )

    async def translate_tags(self, request: TagTranslateRequest) -> dict[str, Any]:
        """Translate tags the offline dictionary misses with the online model."""

        return await online_translation.translate_tags(
            self.translations, self._provider_factory, self._provider_ids, request
        )

    def _annotate_tags(
        self, profile: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Attach the Chinese name to each tag row, in place."""

        for row in rows:
            row["translation"] = self.translations.translate(profile, str(row["tag"]))
        return rows

    # -- sidecar read helpers ----------------------------------------------

    def _sidecar_paths(self, session: Mapping[str, Any], image: Mapping[str, Any]) -> tuple[Path, Path]:
        return sidecar_paths(self.allowlist, session, image)

    def _load_content_and_mtime(
        self, *, paths: tuple[Path, Path]
    ) -> tuple[SidecarContent, float | None]:
        return load_content_and_mtime(paths=paths)


__all__ = ["TagManagerError", "TagManagerService"]
