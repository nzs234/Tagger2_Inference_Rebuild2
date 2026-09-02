"""Tag manager service: dataset sessions, tag editing, batch ops, undo/redo.

The service is the only component that touches dataset sidecars.  Every write
is atomic, validated against the sidecar kind recorded at scan time and
journalled so batch operations can be undone.  Path access goes through the
shared ``PathAllowlist``; responses never contain absolute paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from ..security import PathAllowlist, PathNotAllowedError, atomic_write_bytes
from ..workflow.dataset_import import SUPPORTED_EXTENSIONS
from .contracts import (
    BatchOperationRequest,
    CreateDatasetRequest,
    ImageEditRequest,
    ImageFilter,
    NlTranslateRequest,
    TranslationLookupRequest,
)
from .sidecar_io import (
    NINE_FIELDS,
    SidecarContent,
    SidecarError,
    dedup_tags,
    load_sidecar,
    render_standard_json,
    render_tag_txt,
    render_tags_json,
)
from .storage import TagManagerStore
from .tag_db import TagDatabaseError
from .translations import TagTranslations

logger = logging.getLogger("tagger2.tag_manager")

SCAN_CHUNK = 500
MAX_BATCH_IMAGES = 2000
JOURNAL_DEPTH = 20

# Nine-field list fields that batch tag operations apply to.  Character,
# series, artist, quality, count and nl are never touched by batch edits.
BATCH_TAG_FIELDS = ("tags", "appearance", "environment")

# Nine-field entries whose values are tag-like and therefore translatable; the
# free-form nl paragraph is translated by the online model instead.
TRANSLATABLE_FIELDS = ("quality", "appearance", "tags", "environment")

NL_TRANSLATION_SYSTEM_PROMPT = {
    "zh": (
        "You translate image dataset captions from English into Simplified Chinese. "
        "Return only the translation as a single paragraph. Do not add notes, "
        "explanations, quotes or markdown. Keep proper nouns, character names and "
        "series titles recognizable, and preserve the original level of detail."
    ),
    "en": (
        "You translate image dataset captions into natural English. "
        "Return only the translation as a single paragraph. Do not add notes, "
        "explanations, quotes or markdown. Keep proper nouns, character names and "
        "series titles recognizable, and preserve the original level of detail."
    ),
}


class TagManagerError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int = 400, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class TagManagerService:
    """Facade over the tag manager store, sidecar IO and the tag database."""

    def __init__(
        self,
        *,
        store: TagManagerStore,
        allowlist: PathAllowlist,
        thumbnails: Any,
        tag_database: Any,
        translations: Any = None,
        provider_factory: Any = None,
        provider_ids: Any = None,
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
        self._session_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- path helpers ------------------------------------------------------

    def _resolve_dataset_dir(self, root_id: str, relative_path: str) -> Path:
        try:
            resolved = self.allowlist.resolve(
                root_id, relative_path, must_exist=True, expect="dir"
            )
        except PathNotAllowedError as exc:
            raise TagManagerError(
                "dataset path is not allowed",
                code="path_not_allowed",
                status_code=403,
            ) from exc
        return resolved

    def _resolve_image(self, session: Mapping[str, Any], image: Mapping[str, Any]) -> Path:
        try:
            return self.allowlist.resolve(
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

    def _resolve_sidecar(
        self, session: Mapping[str, Any], image: Mapping[str, Any], sidecar_path: str
    ) -> Path:
        try:
            return self.allowlist.resolve(
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

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _require_session(self, session_id: str) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise TagManagerError(
                "dataset session not found",
                code="dataset_not_found",
                status_code=404,
            )
        return session

    def _require_image(self, session_id: str, image_id: int) -> dict[str, Any]:
        image = self.store.get_image(session_id, image_id)
        if image is None:
            raise TagManagerError(
                "image not found",
                code="image_not_found",
                status_code=404,
            )
        return image

    # -- sessions ----------------------------------------------------------

    def create_session(self, request: CreateDatasetRequest) -> dict[str, Any]:
        dataset_dir = self._resolve_dataset_dir(request.root_id, request.relative_path)
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
        return self._require_session(session_id)

    def list_sessions(self) -> list[dict[str, Any]]:
        return self.store.list_sessions()

    def delete_session(self, session_id: str) -> None:
        if not self.store.delete_session(session_id):
            raise TagManagerError(
                "dataset session not found",
                code="dataset_not_found",
                status_code=404,
            )
        with self._locks_guard:
            self._session_locks.pop(session_id, None)

    def schedule_index(self, session_id: str) -> None:
        """Run :meth:`index_session` on a worker thread (non-blocking caller)."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.index_session(session_id)
            return
        loop.run_in_executor(None, self.index_session, session_id)

    def index_session(self, session_id: str) -> None:
        """Scan the dataset directory and rebuild the index (blocking)."""

        lock = self._session_lock(session_id)
        if not lock.acquire(blocking=False):
            return  # a scan is already running for this session
        try:
            session = self._require_session(session_id)
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
        dataset_dir = self._resolve_dataset_dir(
            str(session["root_id"]), str(session["relative_path"])
        )
        recursive = bool(session["recursive"])
        profile = str(session["profile"])
        categories = _CategoryResolver(self.tag_database, profile)

        keep_paths: set[str] = set()
        pending: list[dict[str, Any]] = []
        indexed = 0
        for image_path in _iter_images(dataset_dir, recursive):
            relative = image_path.relative_to(dataset_dir).as_posix()
            keep_paths.add(relative)
            row, tags = _index_row(image_path, relative, categories)
            row["_tags"] = tags
            pending.append(row)
            if len(pending) >= SCAN_CHUNK:
                indexed += self._flush_rows(session_id, pending)
                pending = []
        if pending:
            indexed += self._flush_rows(session_id, pending)
        self.store.prune_images_missing(session_id, keep_paths)
        self.store.update_session(session_id, status="ready", error=None, image_count=indexed)

    def _flush_rows(self, session_id: str, rows: list[dict[str, Any]]) -> int:
        for row in rows:
            tags = row.pop("_tags")
            image_ids = self.store.upsert_images(session_id, [row])
            self.store.set_image_tags(
                image_ids[0],
                tags,
                sidecar_kind=str(row["sidecar_kind"]),
                sidecar_mtime=row["sidecar_mtime"],
            )
        return len(rows)

    def refresh_session(self, session_id: str) -> dict[str, Any]:
        self._require_session(session_id)
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
        content, live_mtime = self._load_content_and_mtime(
            paths=self._sidecar_paths(session, image)
        )
        image["content"] = _content_payload(content)
        image["sidecar_mtime"] = live_mtime
        # The editor renders the sidecar's own tag strings, which can differ
        # from the indexed rows (nine-field documents carry several lists), so
        # ship one translation map covering everything the drawer will show.
        image["translations"] = self.translations.translate_many(
            profile, _content_tag_strings(content)
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
        """Editing writes sidecars in place, so the session root must be writable."""

        try:
            root = self.allowlist.get(str(session["root_id"]))
        except PathNotAllowedError as exc:
            raise TagManagerError(
                "数据集根目录不存在或未授权",
                code="path_not_allowed",
                status_code=403,
            ) from exc
        if not root.writable:
            raise TagManagerError(
                "数据集根目录不可写：请在设置中为该目录开启可写权限后再编辑标签",
                code="root_not_writable",
                status_code=403,
            )

    def save_image(self, session_id: str, image_id: int, edit: ImageEditRequest) -> dict[str, Any]:
        session = self._require_session(session_id)
        self._require_writable_root(session)
        image = self._require_image(session_id, image_id)
        kind = str(edit.content.kind)
        current_kind = str(image["sidecar_kind"])
        sidecar_rel = image["sidecar_path"] or _sidecar_rel_for_kind(
            str(image["relative_path"]), kind
        )
        if current_kind == "raw_e621_json" and kind != "raw_e621_json":
            raise TagManagerError(
                "raw e621 sidecars are read-only; convert explicitly",
                code="sidecar_read_only",
                status_code=409,
            )
        if current_kind not in {"none", kind}:
            raise TagManagerError(
                "sidecar kind does not match the edit payload",
                code="sidecar_kind_mismatch",
                status_code=409,
            )
        sidecar_path = self._resolve_sidecar(session, image, sidecar_rel)
        current_mtime = _stat_mtime(sidecar_path)
        if edit.expected_sidecar_mtime is not None and current_mtime != edit.expected_sidecar_mtime:
            raise TagManagerError(
                "sidecar changed since it was loaded",
                code="sidecar_conflict",
                status_code=409,
                retryable=True,
            )

        before_text = _read_sidecar_text(sidecar_path)
        rendered = _render_edit(edit.content)
        atomic_write_bytes(sidecar_path, rendered.encode("utf-8"))
        content = load_sidecar(
            sidecar_path.with_suffix(".txt") if kind == "tag_txt" else None,
            sidecar_path if kind != "tag_txt" else None,
        )
        categories = _CategoryResolver(self.tag_database, str(session["profile"]))
        new_mtime = _stat_mtime(sidecar_path)
        self.store.upsert_images(
            session_id,
            [{
                "relative_path": str(image["relative_path"]),
                "file_name": str(image["file_name"]),
                "image_format": str(image["image_format"]),
                "sidecar_kind": kind,
                "sidecar_path": sidecar_rel,
                "mtime": float(image["mtime"]),
                "sidecar_mtime": new_mtime,
                "width": image["width"],
                "height": image["height"],
                "tag_count": len(content.tags),
            }],
        )
        self.store.set_image_tags(
            image_id,
            categories.categorize(content.tags),
            sidecar_kind=kind,
            sidecar_mtime=new_mtime,
        )
        entry_id = self.store.append_journal(
            session_id,
            op="edit",
            spec={"image_ids": [image_id], "kind": kind},
            changes=[{
                "image_id": image_id,
                "sidecar": sidecar_rel,
                "existed": before_text is not None,
                "before": before_text or "",
                "after": rendered,
            }],
        )
        self.store.trim_journal(session_id, JOURNAL_DEPTH)
        return {"image_id": image_id, "journal_id": entry_id, "sidecar_kind": kind}

    def batch_operation(self, session_id: str, request: BatchOperationRequest) -> dict[str, Any]:
        session = self._require_session(session_id)
        self._require_writable_root(session)
        targets = self._resolve_targets(session_id, request)
        if not targets:
            return {"affected": 0, "changes": []}
        if len(targets) > MAX_BATCH_IMAGES:
            raise TagManagerError(
                f"batch operations are capped at {MAX_BATCH_IMAGES} images",
                code="batch_too_large",
                status_code=413,
            )
        categories = _CategoryResolver(self.tag_database, str(session["profile"]))
        changes: list[dict[str, Any]] = []
        for image in targets:
            change = self._apply_batch_to_image(session, image, request, categories)
            if change is not None:
                changes.append(change)
        entry_id = self.store.append_journal(
            session_id,
            op=f"batch_{request.op}",
            spec={"tags": request.tags, "replacement": request.replacement,
                  "use_regex": request.use_regex, "count": len(changes)},
            changes=changes,
        )
        self.store.trim_journal(session_id, JOURNAL_DEPTH)
        return {"affected": len(changes), "journal_id": entry_id}

    def _resolve_targets(
        self, session_id: str, request: BatchOperationRequest
    ) -> list[dict[str, Any]]:
        if request.image_ids is not None:
            targets = []
            for image_id in request.image_ids:
                image = self.store.get_image(session_id, image_id)
                if image is None:
                    raise TagManagerError(
                        f"image {image_id} not found",
                        code="image_not_found",
                        status_code=404,
                    )
                targets.append(image)
            return targets
        image_filter = request.filter or ImageFilter()
        items, _total = self.store.list_images(
            session_id,
            include_tags=list(image_filter.include_tags),
            exclude_tags=list(image_filter.exclude_tags),
            include_mode=image_filter.include_mode,
            kind=image_filter.kind,
            sidecar=image_filter.sidecar,
            sort="name",
            offset=0,
            limit=MAX_BATCH_IMAGES + 1,
        )
        return items

    def _apply_batch_to_image(
        self,
        session: Mapping[str, Any],
        image: Mapping[str, Any],
        request: BatchOperationRequest,
        categories: "_CategoryResolver",
    ) -> dict[str, Any] | None:
        kind = str(image["sidecar_kind"])
        if kind == "raw_e621_json":
            return None  # read-only surfaces are skipped, never half-edited
        sidecar_rel = image["sidecar_path"] or _sidecar_rel_for_kind(
            str(image["relative_path"]), "tag_txt"
        )
        effective_kind = kind if kind != "none" else "tag_txt"
        sidecar_path = self._resolve_sidecar(session, image, sidecar_rel)
        before_text = _read_sidecar_text(sidecar_path)
        content, _live_mtime = self._load_content_and_mtime(
            paths=self._sidecar_paths(session, image)
        )

        if effective_kind == "tag_txt":
            new_tags = _apply_tag_op(list(content.tags), request)
            if new_tags is None:
                return None
            after_text = render_tag_txt(new_tags)
        elif effective_kind == "tags_json":
            entries = [dict(entry) for entry in content.tag_entries]
            new_entries = _apply_entry_op(entries, request, categories)
            if new_entries is None:
                return None
            after_text = render_tags_json(new_entries, document=content.document)
        else:
            document = dict(content.document or {})
            changed = False
            for field in BATCH_TAG_FIELDS:
                values = list(document.get(field) or ())
                new_values = _apply_tag_op([str(value) for value in values], request)
                if new_values is not None:
                    document[field] = new_values
                    changed = True
            if not changed:
                return None
            after_text = render_standard_json(document)

        if after_text == (before_text or ""):
            return None
        atomic_write_bytes(sidecar_path, after_text.encode("utf-8"))
        refreshed = load_sidecar(
            sidecar_path.with_suffix(".txt") if effective_kind == "tag_txt" else None,
            sidecar_path if effective_kind != "tag_txt" else None,
        )
        self.store.set_image_tags(
            int(image["id"]),
            categories.categorize(refreshed.tags),
            sidecar_kind=effective_kind,
            sidecar_mtime=_stat_mtime(sidecar_path),
        )
        return {
            "image_id": int(image["id"]),
            "sidecar": sidecar_rel,
            "existed": before_text is not None,
            "before": before_text or "",
            "after": after_text,
        }

    # -- undo / redo -------------------------------------------------------

    def undo(self, session_id: str) -> dict[str, Any]:
        entry = self.store.latest_journal_entry(session_id, undone=False)
        if entry is None:
            raise TagManagerError(
                "nothing to undo",
                code="undo_empty",
                status_code=409,
            )
        self._replay_changes(session_id, entry["changes"], use="before")
        self.store.set_journal_undone(int(entry["id"]), True)
        return {"journal_id": int(entry["id"]), "reverted": len(entry["changes"])}

    def redo(self, session_id: str) -> dict[str, Any]:
        entry = self.store.latest_journal_entry(session_id, undone=True)
        if entry is None:
            raise TagManagerError(
                "nothing to redo",
                code="redo_empty",
                status_code=409,
            )
        self._replay_changes(session_id, entry["changes"], use="after")
        self.store.set_journal_undone(int(entry["id"]), False)
        return {"journal_id": int(entry["id"]), "reapplied": len(entry["changes"])}

    def _replay_changes(
        self, session_id: str, changes: list[Mapping[str, Any]], *, use: str
    ) -> None:
        session = self._require_session(session_id)
        self._require_writable_root(session)
        categories = _CategoryResolver(self.tag_database, str(session["profile"]))
        for change in changes:
            image = self.store.get_image(session_id, int(change["image_id"]))
            if image is None:
                continue  # the image row is gone; nothing to restore
            sidecar_rel = str(change["sidecar"])
            sidecar_path = self._resolve_sidecar(session, image, sidecar_rel)
            text = str(change[use])
            if not text and not change["existed"]:
                sidecar_path.unlink(missing_ok=True)
                kind = "none"
            else:
                atomic_write_bytes(sidecar_path, text.encode("utf-8"))
                kind = str(change.get("kind") or _kind_from_suffix(sidecar_path))
            content = load_sidecar(
                sidecar_path.with_suffix(".txt") if kind == "tag_txt" else None,
                sidecar_path if kind != "tag_txt" else None,
            )
            self.store.set_image_tags(
                int(image["id"]),
                categories.categorize(content.tags) if kind != "none" else [],
                sidecar_kind=kind,
                sidecar_mtime=_stat_mtime(sidecar_path) if kind != "none" else None,
            )

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
        for item in items:
            item["translation"] = self.translations.translate(profile, str(item["name"]))
        return {"profile": profile, "items": items}

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

    async def translate_nl(self, request: NlTranslateRequest) -> dict[str, Any]:
        """Translate one NL caption with a configured online provider."""

        provider_id = (request.provider_id or "").strip() or self._first_provider_id()
        if not provider_id or self._provider_factory is None:
            raise TagManagerError(
                "没有可用的在线模型：请先在「Provider 配置」中添加并启用一个在线模型",
                code="nl_translate_unavailable",
                status_code=409,
            )
        try:
            provider = self._provider_factory(provider_id)
        except Exception as exc:  # noqa: BLE001 - provider errors are sanitized below
            raise TagManagerError(
                f"在线模型不可用：{exc}",
                code="nl_translate_unavailable",
                status_code=409,
            ) from exc
        try:
            text = await provider.generate(
                image=None,
                prompt=request.text,
                model=(request.model or "").strip() or None,
                system_prompt=NL_TRANSLATION_SYSTEM_PROMPT[request.target],
            )
        except Exception as exc:  # noqa: BLE001 - one failure mode for the UI
            logger.warning("tag manager NL translation failed via %s: %s", provider_id, exc)
            raise TagManagerError(
                f"翻译失败：{exc}",
                code="nl_translate_failed",
                status_code=502,
                retryable=True,
            ) from exc
        translated = str(text or "").strip()
        if not translated:
            raise TagManagerError(
                "翻译失败：在线模型返回了空结果",
                code="nl_translate_failed",
                status_code=502,
                retryable=True,
            )
        return {
            "text": translated,
            "target": request.target,
            "provider_id": provider_id,
            "model": (request.model or "").strip() or str(getattr(provider, "model", "")),
        }

    def _first_provider_id(self) -> str:
        if self._provider_ids is None:
            return ""
        try:
            candidates = list(self._provider_ids())
        except Exception:  # noqa: BLE001 - a broken registry must not 500 here
            return ""
        return str(candidates[0]) if candidates else ""

    def _annotate_tags(
        self, profile: str, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Attach the Chinese name to each tag row, in place."""

        for row in rows:
            row["translation"] = self.translations.translate(profile, str(row["tag"]))
        return rows

    # -- helpers -----------------------------------------------------------

    def _sidecar_paths(self, session: Mapping[str, Any], image: Mapping[str, Any]) -> tuple[Path, Path]:
        """Return (txt, json) sidecar paths for one indexed image."""

        image_path = self._resolve_image(session, image)
        return image_path.with_suffix(".txt"), image_path.with_suffix(".json")

    def _load_content_and_mtime(
        self, *, paths: tuple[Path, Path]
    ) -> tuple[SidecarContent, float | None]:
        txt_path, json_path = paths
        try:
            content = load_sidecar(
                txt_path if txt_path.is_file() else None,
                json_path if json_path.is_file() else None,
            )
        except SidecarError as exc:
            raise TagManagerError(
                str(exc), code="sidecar_invalid", status_code=409
            ) from exc
        live = txt_path if content.kind == "tag_txt" else (
            json_path if content.kind != "none" else None
        )
        return content, _stat_mtime(live) if live is not None else None


class _CategoryResolver:
    """Resolves tag categories through the tag database with caching."""

    def __init__(self, tag_database: Any, profile: str) -> None:
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
        if self._available:
            info = self._tag_database.lookup(self._profile, tag)
            if info is not None:
                category = str(info["category"])
        elif self._tag_database is not None:
            try:
                info = self._tag_database.lookup(self._profile, tag)
            except Exception:  # noqa: BLE001 - enrichment is best effort
                info = None
            if info is not None:
                category = str(info["category"])
                self._available = True
        self._cache[key] = category
        return category

    def categorize(self, tags: tuple[str, ...] | list[str]) -> list[tuple[str, str]]:
        return [(tag, self.category_for(tag)) for tag in tags]


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


def _index_row(
    image_path: Path, relative: str, categories: _CategoryResolver
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
        sidecar_mtime = _stat_mtime(txt_path if content.kind == "tag_txt" else json_path)

    row = {
        "relative_path": relative,
        "file_name": image_path.name,
        "image_format": image_format,
        "sidecar_kind": content.kind,
        "sidecar_path": sidecar_rel,
        "mtime": _stat_mtime(image_path) or 0.0,
        "sidecar_mtime": sidecar_mtime,
        "width": width,
        "height": height,
        "tag_count": len(content.tags),
    }
    return row, categories.categorize(content.tags)


def _sidecar_rel_for_kind(relative_image_path: str, kind: str) -> str:
    suffix = ".txt" if kind == "tag_txt" else ".json"
    return relative_image_path[: -len(Path(relative_image_path).suffix)] + suffix


def _kind_from_suffix(sidecar_path: Path) -> str:
    return "tag_txt" if sidecar_path.suffix.casefold() == ".txt" else "standard_json"


def _stat_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _read_sidecar_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _content_tag_strings(content: SidecarContent) -> list[str]:
    """Every tag-like string the editor will render for one sidecar.

    Nine-field documents keep tags across several lists, so the translation map
    has to cover all of them rather than only the indexed ``tags`` field.
    """

    if content.kind == "standard_json":
        document = content.document or {}
        values: list[str] = []
        for field in TRANSLATABLE_FIELDS:
            entries = document.get(field) or ()
            if isinstance(entries, str):
                values.append(entries)
            elif isinstance(entries, (list, tuple)):
                values.extend(str(entry) for entry in entries)
        return values
    return list(content.tags)


def _content_payload(content: SidecarContent) -> dict[str, Any]:
    if content.kind == "tag_txt":
        return {"kind": "tag_txt", "tags": list(content.tags)}
    if content.kind == "tags_json":
        return {"kind": "tags_json", "tags": [dict(entry) for entry in content.tag_entries]}
    if content.kind == "standard_json":
        document = {key: (content.document or {}).get(key) for key in NINE_FIELDS}
        return {"kind": "standard_json", "fields": document}
    if content.kind == "raw_e621_json":
        return {"kind": "raw_e621_json", "tags": list(content.tags), "read_only": True}
    return {"kind": "none"}


def _render_edit(content: Any) -> str:
    if content.kind == "tag_txt":
        return render_tag_txt(dedup_tags(content.tags))
    if content.kind == "tags_json":
        return render_tags_json([entry.model_dump(exclude_none=True) for entry in content.tags])
    document = {field: getattr(content.fields, field) for field in NINE_FIELDS}
    return render_standard_json(document)


def _apply_tag_op(
    tags: list[str], request: BatchOperationRequest
) -> list[str] | None:
    """Apply one batch op to a flat tag list; None means no change."""

    if request.op == "add":
        merged = dedup_tags([*tags, *request.tags])
        return merged if merged != dedup_tags(tags) else None
    if request.op == "remove":
        if request.use_regex:
            patterns = [re.compile(tag) for tag in request.tags]
            kept = [
                tag for tag in tags
                if not any(pattern.search(tag) for pattern in patterns)
            ]
        else:
            removed = {tag.casefold() for tag in request.tags}
            kept = [tag for tag in tags if tag.casefold() not in removed]
        return kept if kept != tags else None
    # replace
    if request.use_regex:
        pattern = re.compile(request.tags[0]) if request.tags else None
        replacement = request.replacement or ""
        updated = [
            pattern.sub(replacement, tag) if pattern else tag for tag in tags
        ]
    else:
        replaced = {tag.casefold(): request.replacement or "" for tag in request.tags}
        updated = [replaced.get(tag.casefold(), tag) for tag in tags]
    cleaned = [tag for tag in updated if tag.strip()]
    merged = dedup_tags(cleaned)
    return merged if merged != dedup_tags(tags) else None


def _apply_entry_op(
    entries: list[dict[str, Any]],
    request: BatchOperationRequest,
    categories: _CategoryResolver,
) -> list[dict[str, Any]] | None:
    """Apply one batch op to tags_json entries, preserving entry metadata."""

    if request.op == "add":
        existing = {str(entry.get("text", "")).casefold() for entry in entries}
        fresh = [
            {"text": tag, "category": categories.category_for(tag)}
            for tag in request.tags
            if tag.casefold() not in existing
        ]
        return entries + fresh if fresh else None
    if request.op == "remove":
        if request.use_regex:
            patterns = [re.compile(tag) for tag in request.tags]
            kept = [
                entry for entry in entries
                if not any(pattern.search(str(entry.get("text", ""))) for pattern in patterns)
            ]
        else:
            removed = {tag.casefold() for tag in request.tags}
            kept = [
                entry for entry in entries
                if str(entry.get("text", "")).casefold() not in removed
            ]
        return kept if kept != entries else None
    # replace
    if request.use_regex:
        pattern = re.compile(request.tags[0]) if request.tags else None
        replacement = request.replacement or ""
        updated = []
        for entry in entries:
            text = str(entry.get("text", ""))
            new_text = pattern.sub(replacement, text) if pattern else text
            if new_text.strip():
                entry = dict(entry)
                entry["text"] = new_text
                updated.append(entry)
    else:
        replaced = {tag.casefold(): request.replacement or "" for tag in request.tags}
        updated = []
        for entry in entries:
            text = str(entry.get("text", ""))
            new_text = replaced.get(text.casefold(), text)
            if new_text.strip():
                entry = dict(entry)
                entry["text"] = new_text
                updated.append(entry)
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in updated:
        key = str(entry.get("text", "")).casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged if merged != entries else None


__all__ = ["TagManagerError", "TagManagerService"]
