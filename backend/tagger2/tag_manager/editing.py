"""In-place sidecar editing for the tag manager: saves, batch ops, undo/redo.

Every write is atomic, validated against the sidecar kind recorded at scan
time and journalled so batch operations can be undone.  Mutating operations
serialize through the shared :class:`~tagger2.tag_manager.indexing.SessionLocks`
instance owned by the facade.  The module also carries the sidecar payload
helpers (editor read payloads, nested-extras merge, render-on-save) shared with
the facade's browsing methods.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Container, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

from ..security import (
    PathAllowlist,
    PathNotAllowedError,
    atomic_write_bytes,
)
from ..tag_text import canonical_tag_key
from .contracts import (
    BatchOperationRequest,
    ImageEditRequest,
    ImageFilter,
)
from .errors import TagManagerError
from .indexing import (
    CategoryResolver,
    SessionLocks,
    require_image,
    require_session,
    resolve_image,
    resolve_sidecar,
    stat_mtime,
)
from .protocols import TagDatabaseClient
from .sidecar_io import (
    MAX_SIDECAR_BYTES,
    NINE_FIELDS,
    SidecarContent,
    SidecarError,
    _parse_tag_list,
    dedup_tags,
    load_sidecar,
    render_standard_json,
    render_tag_txt,
    render_tags_json,
)
from .storage import TagManagerStore

logger = logging.getLogger("tagger2.tag_manager")

MAX_BATCH_IMAGES = 2000
JOURNAL_DEPTH = 20
# The batch preview ships at most this many sample diffs; the counts above the
# samples always describe the full target set, so a large batch stays cheap.
PREVIEW_SAMPLE_LIMIT = 5

# ``_apply_batch_to_image`` return sentinel for a read-only target, kept
# distinct from ``None`` (a target the operation left unchanged) so the batch
# response can report the two skip reasons separately.
_SKIPPED_READ_ONLY: Any = object()


@dataclass(frozen=True)
class BatchChangePlan:
    """One target's fully-computed batch change, before anything is written.

    ``_compute_batch_change`` owns the read + validate + render span (including
    the write-size guard); this plan carries everything the write phase needs so
    the same computation can drive either the real batch or a read-only preview.
    ``live_mtime`` is the stamp observed while reading: the write phase
    re-verifies it immediately before writing, and the preview never does (it
    never writes).
    """

    image_id: int
    file_name: str
    sidecar_rel: str
    sidecar_path: Path
    kind: str
    existed: bool
    before_text: str | None
    after_text: str
    before_tags: list[str]
    after_tags: list[str]
    before_version: dict[str, int] | None
    live_mtime: float | None

# Nine-field list fields that batch tag operations apply to.  Character,
# series, artist, quality, count and nl are never touched by batch edits.
BATCH_TAG_FIELDS = ("tags", "appearance", "environment")

# Nine-field entries whose values are tag-like and therefore translatable; the
# free-form nl paragraph is translated by the online model instead.
TRANSLATABLE_FIELDS = ("quality", "appearance", "tags", "environment")


def _guard_write_size(text: str) -> None:
    """Reject a sidecar or journalled text over the 1 MiB read budget.

    ``sidecar_io`` only enforces the limit on read; without the write-side
    mirror an oversized save would land a file the next load refuses to parse.
    Checked before every write (save, batch and undo/redo replay), so a
    rejected request leaves both the sidecar and the journal untouched.
    """

    if len(text.encode("utf-8")) > MAX_SIDECAR_BYTES:
        raise TagManagerError(
            "sidecar exceeds the 1 MiB limit",
            code="sidecar_too_large",
            status_code=413,
        )


@lru_cache(maxsize=256)
def _compile_tag_pattern(pattern: str) -> re.Pattern[str]:
    """Compile one batch regex pattern once per process.

    Patterns are validated (length, shape, compilability) in the request
    contract, so only vetted patterns reach here; caching keeps a 2000-image
    batch from recompiling the same pattern for every sidecar.
    """

    return re.compile(pattern)


# -- shared sidecar read helpers ---------------------------------------


def require_writable_root(allowlist: PathAllowlist, session: Mapping[str, Any]) -> None:
    """Editing writes sidecars in place, so the session root must be writable."""

    try:
        root = allowlist.get(str(session["root_id"]))
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


def assert_sidecar_not_stale(
    expected_mtime: float | None,
    current_mtime: float | None,
    image: Mapping[str, Any],
) -> None:
    """Optimistic-concurrency guard for one sidecar write.

    The client contract carries the ``st_mtime`` float it received from
    ``get_image``.  When the client does not supply one, the indexed
    ``sidecar_mtime`` is the fallback expectation, so an externally
    modified or deleted sidecar is never overwritten silently.  Exact
    ``mtime_ns``/size stamps are kept per journal change instead, because
    the SQLite REAL column and the JSON client contract cannot round-trip
    nanosecond integers losslessly.
    """

    expected = expected_mtime if expected_mtime is not None else image["sidecar_mtime"]
    # A sidecar appearing after the client loaded an image is also a
    # concurrent change: do not let a stale no-sidecar view overwrite it.
    if expected is None:
        if current_mtime is None:
            return
        raise TagManagerError(
            "sidecar changed since it was loaded",
            code="sidecar_conflict",
            status_code=409,
            retryable=True,
        )
    if current_mtime != expected:
        raise TagManagerError(
            "sidecar changed since it was loaded",
            code="sidecar_conflict",
            status_code=409,
            retryable=True,
        )


def sidecar_paths(
    allowlist: PathAllowlist, session: Mapping[str, Any], image: Mapping[str, Any]
) -> tuple[Path, Path]:
    """Return (txt, json) sidecar paths for one indexed image."""

    image_path = resolve_image(allowlist, session, image)
    return image_path.with_suffix(".txt"), image_path.with_suffix(".json")


def load_content_and_mtime(
    *, paths: tuple[Path, Path]
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
    return content, stat_mtime(live) if live is not None else None


def content_tag_strings(content: SidecarContent) -> list[str]:
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


# -- sidecar payload helpers -------------------------------------------


def _sidecar_rel_for_kind(relative_image_path: str, kind: str) -> str:
    suffix = ".txt" if kind == "tag_txt" else ".json"
    return relative_image_path[: -len(Path(relative_image_path).suffix)] + suffix


def _kind_from_suffix(sidecar_path: Path) -> str:
    return "tag_txt" if sidecar_path.suffix.casefold() == ".txt" else "standard_json"


def _sidecar_rel_matches_kind(sidecar_rel: str, kind: str) -> bool:
    """Whether a recorded sidecar path's suffix fits the requested kind.

    ``.txt`` is the tag_txt slot; anything else (``.json``) is the JSON slot.
    A recorded path that disagrees (a stale extension after the sidecar was
    deleted and re-created with another format) must not drive the write.
    """

    suffix = Path(sidecar_rel).suffix.casefold()
    return suffix == ".txt" if kind == "tag_txt" else suffix != ".txt"


def _sidecar_version(path: Path) -> dict[str, int] | None:
    """mtime_ns + size snapshot persisted as a journal change's version stamp.

    Exact integer nanoseconds and the byte size survive JSON round-trips, so
    replay code can diagnose drift precisely; the float ``st_mtime`` stays on
    the SQLite REAL column and the client contract, where nanosecond integers
    would lose precision.
    """

    try:
        stat = path.stat()
    except OSError:
        return None
    return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}


def _read_sidecar_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _is_flat_value(value: Any) -> bool:
    if isinstance(value, (bool, int, float, str)) or value is None:
        return True
    return isinstance(value, list) and all(
        isinstance(item, (bool, int, float, str)) or item is None for item in value
    )


def content_payload(content: SidecarContent) -> dict[str, Any]:
    """The edit payload shipped to the editor.

    Flat extra keys round-trip through the editor (the strict request models
    accept exactly these shapes); nested extra values — container-level and
    per-tag-entry alike — are stripped here and re-merged from the on-disk
    document at save time, so the client contract stays strict while no
    sidecar metadata is ever lost.
    """
    if content.kind == "tag_txt":
        return {"kind": "tag_txt", "tags": list(content.tags)}
    if content.kind == "tags_json":
        document = content.document or {}
        payload = {
            key: value
            for key, value in document.items()
            if key != "tags" and _is_flat_value(value)
        }
        payload["kind"] = "tags_json"
        payload["tags"] = [
            {
                key: value
                for key, value in entry.items()
                if key in {"text", "category", "score"} or _is_flat_value(value)
            }
            for entry in content.tag_entries
        ]
        return payload
    if content.kind == "standard_json":
        document = content.document or {}
        payload = {
            key: value
            for key, value in document.items()
            if key not in NINE_FIELDS and _is_flat_value(value)
        }
        payload["kind"] = "standard_json"
        payload["fields"] = {key: document.get(key) for key in NINE_FIELDS}
        return payload
    if content.kind == "raw_e621_json":
        return {"kind": "raw_e621_json", "tags": list(content.tags), "read_only": True}
    return {"kind": "none"}


def _pydantic_extras(model: Any) -> dict[str, Any]:
    return dict(getattr(model, "__pydantic_extra__", None) or {})


def _nested_extras(document: Mapping[str, Any] | None, skip: Container[str]) -> dict[str, Any]:
    if not document:
        return {}
    return {
        key: value
        for key, value in document.items()
        if key not in skip and not _is_flat_value(value)
    }


def _original_content_for_merge(before_text: str | None, kind: str) -> SidecarContent | None:
    """Parse the pre-save sidecar text for the nested-extras merge."""

    if not before_text or kind == "tag_txt":
        return None
    try:
        document = json.loads(before_text)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    return SidecarContent(kind=cast(Literal["tags_json", "standard_json"], kind), document=document)


def _tag_entry_payload(entry: Any, original_entries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """One tags_json entry as written back to disk, extras included.

    Nested extra values of the original entry (matched by tag text) are
    re-merged server-side: they never entered the client contract, but the
    editor must not be able to drop them either.
    """

    payload: dict[str, Any] = _pydantic_extras(entry)
    original = original_entries.get(entry.text.casefold())
    if original is not None:
        for key, value in original.items():
            if key in {"text", "category", "score"} or key in payload or _is_flat_value(value):
                continue
            payload[key] = value
    payload["text"] = entry.text
    if entry.category is not None:
        payload["category"] = entry.category
    if entry.score is not None:
        payload["score"] = entry.score
    return payload


def _render_edit(content: Any, original: SidecarContent | None = None) -> str:
    if content.kind == "tag_txt":
        return render_tag_txt(dedup_tags(content.tags))
    if content.kind == "tags_json":
        original_document = original.document if original is not None else None
        original_entries = {
            str(entry.get("text", "")).casefold(): entry
            for entry in ((original_document or {}).get("tags") or [])
            if isinstance(entry, dict)
        }
        entries = [_tag_entry_payload(entry, original_entries) for entry in content.tags]
        document = {**_nested_extras(original_document, skip={"tags"}), **_pydantic_extras(content)}
        return render_tags_json(entries, document=document or None)
    document = {field: getattr(content.fields, field) for field in NINE_FIELDS}
    document.update(_pydantic_extras(content))
    document.update(_nested_extras(
        original.document if original is not None else None, skip=NINE_FIELDS
    ))
    return render_standard_json(document)


# -- batch tag operations ----------------------------------------------


def _apply_tag_op(
    tags: list[str], request: BatchOperationRequest
) -> list[str] | None:
    """Apply one batch op to a flat tag list; None means no change.

    Non-regex comparisons key on ``canonical_tag_key`` (lowercase underscore),
    so a sidecar spelling ``long hair`` and a request spelling ``long_hair``
    match; dedup keeps the first spelling it sees.  Regex mode keeps matching
    the raw text (a pattern is not a tag name).
    """

    if request.op == "add":
        merged = dedup_tags([*tags, *request.tags])
        return merged if merged != dedup_tags(tags) else None
    if request.op == "remove":
        if request.use_regex:
            patterns = [_compile_tag_pattern(tag) for tag in request.tags]
            kept = [
                tag for tag in tags
                if not any(pattern.search(tag) for pattern in patterns)
            ]
        else:
            removed = {canonical_tag_key(tag) for tag in request.tags}
            kept = [tag for tag in tags if canonical_tag_key(tag) not in removed]
        return kept if kept != tags else None
    # replace
    if request.use_regex:
        pattern = _compile_tag_pattern(request.tags[0]) if request.tags else None
        replacement = request.replacement or ""
        updated = [
            pattern.sub(replacement, tag) if pattern else tag for tag in tags
        ]
    else:
        replaced = {
            canonical_tag_key(tag): request.replacement or "" for tag in request.tags
        }
        updated = [replaced.get(canonical_tag_key(tag), tag) for tag in tags]
    cleaned = [tag for tag in updated if tag.strip()]
    merged = dedup_tags(cleaned)
    return merged if merged != dedup_tags(tags) else None


def _apply_entry_op(
    entries: list[dict[str, Any]],
    request: BatchOperationRequest,
    categories: CategoryResolver,
) -> list[dict[str, Any]] | None:
    """Apply one batch op to tags_json entries, preserving entry metadata.

    Non-regex keys use ``canonical_tag_key`` exactly like the flat-list path;
    regex mode still matches the raw entry text.
    """

    if request.op == "add":
        existing = {
            canonical_tag_key(str(entry.get("text", ""))) for entry in entries
        }
        fresh = [
            {"text": tag, "category": categories.category_for(tag)}
            for tag in request.tags
            if canonical_tag_key(tag) not in existing
        ]
        return entries + fresh if fresh else None
    if request.op == "remove":
        if request.use_regex:
            patterns = [_compile_tag_pattern(tag) for tag in request.tags]
            kept = [
                entry for entry in entries
                if not any(pattern.search(str(entry.get("text", ""))) for pattern in patterns)
            ]
        else:
            removed = {canonical_tag_key(tag) for tag in request.tags}
            kept = [
                entry for entry in entries
                if canonical_tag_key(str(entry.get("text", ""))) not in removed
            ]
        return kept if kept != entries else None
    # replace
    if request.use_regex:
        pattern = _compile_tag_pattern(request.tags[0]) if request.tags else None
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
        replaced = {
            canonical_tag_key(tag): request.replacement or "" for tag in request.tags
        }
        updated = []
        for entry in entries:
            text = str(entry.get("text", ""))
            new_text = replaced.get(canonical_tag_key(text), text)
            if new_text.strip():
                entry = dict(entry)
                entry["text"] = new_text
                updated.append(entry)
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in updated:
        key = canonical_tag_key(str(entry.get("text", "")))
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged if merged != entries else None


# -- editor ------------------------------------------------------------


class SessionEditor:
    """Single saves, batch operations and undo/redo replay for one session."""

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

    @staticmethod
    def _journal_slot_kind(change: Mapping[str, Any], entry_id: int, sidecar_rel: str) -> str:
        """Loader slot kind (tag_txt vs json) for one journalled change.

        Entries written by this service carry the per-change ``kind``.  Legacy
        entries recorded it only in the entry ``spec`` (single-image saves) or
        not at all (batch changes), so the suffix decides the loader slot; the
        fallback is logged loudly instead of silently misreading a tags_json
        file as standard_json.  The restored content still wins in phase 2.
        """

        kind = str(change.get("kind") or "")
        if kind:
            return kind
        fallback = _kind_from_suffix(Path(sidecar_rel))
        logger.warning(
            "undo/redo journal entry %s change for sidecar %s has no kind"
            " recorded; falling back to the file suffix (%s)",
            entry_id, sidecar_rel, fallback,
        )
        return fallback

    @staticmethod
    def _assert_replay_matches(
        sidecar_path: Path,
        change: Mapping[str, Any],
        *,
        other: str,
        slot_kind: str,
    ) -> None:
        """Fail before writing when the live sidecar no longer fits the journal.

        Undo expects the entry's ``after`` state on disk, redo its ``before``
        state; an empty journalled state means "no annotation" (missing or
        blank file).  The mtime_ns+size version stamps are diagnostics in the
        warning — text equality is the decision, because every replay rewrites
        the file and therefore cannot keep mtimes stable.
        """

        sidecar_rel = str(change.get("sidecar", sidecar_path.name))
        expected_text = str(change.get(other, ""))
        current_text = _read_sidecar_text(sidecar_path)
        if expected_text:
            matches = current_text == expected_text
        else:
            matches = current_text is None or not current_text.strip()
        if not matches:
            logger.warning(
                "undo/redo refused: sidecar %s changed since the entry was"
                " written (expected %s version %s, live version %s)",
                sidecar_rel,
                other,
                change.get(f"{other}_version"),
                _sidecar_version(sidecar_path),
            )
            raise TagManagerError(
                f"sidecar changed since the journal was written: {sidecar_rel}",
                code="sidecar_conflict",
                status_code=409,
                retryable=True,
            )
        if not current_text:
            return
        try:
            detected = load_sidecar(
                sidecar_path.with_suffix(".txt") if slot_kind == "tag_txt" else None,
                sidecar_path if slot_kind != "tag_txt" else None,
            )
        except SidecarError as exc:
            raise TagManagerError(
                str(exc), code="sidecar_invalid", status_code=409
            ) from exc
        journalled_kind = str(change.get("kind") or "")
        if journalled_kind and detected.kind not in {"none", journalled_kind}:
            raise TagManagerError(
                f"sidecar {sidecar_rel} now parses as {detected.kind} but the"
                f" journal recorded {journalled_kind}",
                code="sidecar_kind_mismatch",
                status_code=409,
                retryable=True,
            )

    @staticmethod
    def _assert_journal_text_valid(text: str, slot_kind: str, sidecar_rel: str) -> None:
        """Reject journalled text that could not be re-parsed after writing.

        Without this check a corrupt (typically legacy) entry would fail in
        phase 2 — after earlier changes of the same entry were already applied.
        The size guard runs here too, so an oversized journalled text is
        refused in phase 1 with no file written.
        """

        _guard_write_size(text)
        if not text or slot_kind == "tag_txt":
            return
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise TagManagerError(
                f"journalled text for {sidecar_rel} is not valid JSON",
                code="journal_invalid",
                status_code=409,
            ) from exc
        if not isinstance(parsed, dict):
            raise TagManagerError(
                f"journalled text for {sidecar_rel} is not a JSON object",
                code="journal_invalid",
                status_code=409,
            )

    def save_image(self, session_id: str, image_id: int, edit: ImageEditRequest) -> dict[str, Any]:
        session = require_session(self.store, session_id)
        require_writable_root(self.allowlist, session)
        with self._locks.exclusive(session_id):
            # Re-check under the lock: delete_session serializes through the
            # same lock, so the session row can vanish while this request
            # waited between the fetch above and the acquisition.  The stale
            # mapping must never drive the write span (it would journal into
            # a deleted session and write sidecars for it).
            session = require_session(self.store, session_id)
            return self._save_image_locked(session, image_id, edit)

    def _save_image_locked(
        self, session: Mapping[str, Any], image_id: int, edit: ImageEditRequest
    ) -> dict[str, Any]:
        session_id = str(session["id"])
        image = require_image(self.store, session_id, image_id)
        kind = str(edit.content.kind)
        current_kind = str(image["sidecar_kind"])
        sidecar_rel = image["sidecar_path"] or _sidecar_rel_for_kind(
            str(image["relative_path"]), kind
        )
        # A recorded path may carry a suffix from a previous format (its
        # extension was not updated when an undo deleted the sidecar).  Reuse
        # it only when it belongs to the requested slot, otherwise re-derive:
        # writing tags_json bytes into "c.txt" would leave an unreadable file.
        if not _sidecar_rel_matches_kind(str(sidecar_rel), kind):
            sidecar_rel = _sidecar_rel_for_kind(str(image["relative_path"]), kind)
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
        sidecar_path = resolve_sidecar(self.allowlist, session, image, sidecar_rel)
        current_mtime = stat_mtime(sidecar_path)
        assert_sidecar_not_stale(edit.expected_sidecar_mtime, current_mtime, image)

        before_text = _read_sidecar_text(sidecar_path)
        before_version = _sidecar_version(sidecar_path)
        # Nested extra values never enter the client contract; merge them back
        # from the pre-save bytes so saving cannot drop them.
        rendered = _render_edit(
            edit.content, _original_content_for_merge(before_text, kind)
        )
        # Refuse an oversized render before the write: a file over the read
        # budget could never be loaded again, and nothing may land on disk.
        _guard_write_size(rendered)
        # Re-verify the optimistic-concurrency stamp immediately before the
        # write: an external writer can land a change between the validation
        # above and this write, and the write must only ever land on the
        # bytes this save actually read.
        if stat_mtime(sidecar_path) != current_mtime:
            raise TagManagerError(
                "sidecar changed since it was loaded",
                code="sidecar_conflict",
                status_code=409,
                retryable=True,
            )
        atomic_write_bytes(sidecar_path, rendered.encode("utf-8"))
        # Journal before the index refresh: if anything below fails, the entry
        # already covers the written sidecar, so undo can restore a consistent
        # state (and the undo itself repairs the index rows).
        entry_id = self.store.append_journal(
            session_id,
            op="edit",
            spec={"image_ids": [image_id], "kind": kind},
            changes=[{
                "image_id": image_id,
                "sidecar": sidecar_rel,
                "existed": before_text is not None,
                "kind": kind,
                "before": before_text or "",
                "after": rendered,
                "before_version": before_version,
                "after_version": _sidecar_version(sidecar_path),
            }],
        )
        self.store.trim_journal(session_id, JOURNAL_DEPTH)
        # A fresh edit branches the history: previously undone entries can no
        # longer be replayed (their text-equality guard would reject them
        # against the new state), so the redo stack is dropped only now — after
        # the journal entry actually landed.  A failed save (sidecar conflict,
        # kind mismatch, ...) must leave the redo history untouched.
        self.store.discard_redo_stack(session_id)
        # The bytes are already written and journalled above; a post-write
        # parse failure (should not happen for a render this service produced)
        # is surfaced as a readable error that says the step is undoable
        # rather than leaking a raw SidecarError after the redo stack dropped.
        try:
            content = load_sidecar(
                sidecar_path.with_suffix(".txt") if kind == "tag_txt" else None,
                sidecar_path if kind != "tag_txt" else None,
            )
        except SidecarError as exc:
            raise TagManagerError(
                f"sidecar was written but cannot be re-read ({exc});"
                " 该步已记录到操作日志，可撤销恢复",
                code="sidecar_invalid",
                status_code=409,
            ) from exc
        categories = CategoryResolver(self.tag_database, str(session["profile"]))
        self.store.upsert_images(
            session_id,
            [{
                "relative_path": str(image["relative_path"]),
                "file_name": str(image["file_name"]),
                "image_format": str(image["image_format"]),
                "sidecar_kind": kind,
                "sidecar_path": sidecar_rel,
                "mtime": float(image["mtime"]),
                "sidecar_mtime": stat_mtime(sidecar_path),
                "width": image["width"],
                "height": image["height"],
                "tag_count": len(content.tags),
            }],
        )
        self.store.set_image_tags(
            image_id,
            categories.categorize(content.tags),
            sidecar_kind=kind,
            sidecar_mtime=stat_mtime(sidecar_path),
        )
        return {
            "image_id": image_id,
            "journal_id": entry_id,
            "sidecar_kind": kind,
            "sidecar_mtime": stat_mtime(sidecar_path),
        }

    def batch_operation(self, session_id: str, request: BatchOperationRequest) -> dict[str, Any]:
        session = require_session(self.store, session_id)
        require_writable_root(self.allowlist, session)
        with self._locks.exclusive(session_id):
            # Same lock-window re-check as save_image: a concurrent
            # delete_session between the fetch above and the acquisition must
            # surface as 404, not as writes into a deleted session.
            session = require_session(self.store, session_id)
            targets = self._resolve_targets(session_id, request)
            if not targets:
                return {
                    "affected": 0,
                    "journal_id": None,
                    "skipped_read_only": 0,
                    "no_change": 0,
                }
            if len(targets) > MAX_BATCH_IMAGES:
                raise TagManagerError(
                    f"batch operations are capped at {MAX_BATCH_IMAGES} images",
                    code="batch_too_large",
                    status_code=413,
                )
            categories = CategoryResolver(self.tag_database, str(session["profile"]))
            changes: list[dict[str, Any]] = []
            skipped_read_only = 0
            no_change = 0
            try:
                for image in targets:
                    change = self._apply_batch_to_image(session, image, request, categories)
                    if change is _SKIPPED_READ_ONLY:
                        skipped_read_only += 1
                    elif change is None:
                        no_change += 1
                    else:
                        changes.append(change)
            except Exception:
                # Keep the images already written recoverable: journal the
                # partial changes before propagating, so undo can restore them.
                if changes:
                    self._append_batch_journal(session_id, request, changes, partial=True)
                    # Same branching rule as save_image: the partial entry is a
                    # fresh write, so the previously undone history is dropped
                    # only now that the entry actually landed.
                    self.store.discard_redo_stack(session_id)
                raise
            if not changes:
                # Every target was read-only or a no-op: nothing branches the
                # history, so no (empty) journal entry and the redo stack stays
                # intact.  The skip tallies still describe what was skipped.
                return {
                    "affected": 0,
                    "journal_id": None,
                    "skipped_read_only": skipped_read_only,
                    "no_change": no_change,
                }
            entry_id = self._append_batch_journal(session_id, request, changes)
            # A new batch invalidates the undone history that can no longer be
            # replayed; drop it only after the entry actually landed.
            self.store.discard_redo_stack(session_id)
            return {
                "affected": len(changes),
                "journal_id": entry_id,
                "skipped_read_only": skipped_read_only,
                "no_change": no_change,
            }

    def _append_batch_journal(
        self,
        session_id: str,
        request: BatchOperationRequest,
        changes: list[dict[str, Any]],
        *,
        partial: bool = False,
    ) -> int:
        spec: dict[str, Any] = {
            "tags": request.tags,
            "replacement": request.replacement,
            "use_regex": request.use_regex,
            "count": len(changes),
        }
        if partial:
            spec["partial"] = True
        entry_id = self.store.append_journal(
            session_id,
            op=f"batch_{request.op}",
            spec=spec,
            changes=changes,
        )
        self.store.trim_journal(session_id, JOURNAL_DEPTH)
        return entry_id

    def _resolve_targets(
        self, session_id: str, request: BatchOperationRequest
    ) -> list[dict[str, Any]]:
        if request.image_ids is not None:
            # One chunked query instead of a lookup per id: a 2000-image batch
            # would otherwise open one connection (and pay one index probe)
            # per id before touching a single sidecar.
            found = self.store.get_images(session_id, request.image_ids)
            targets = []
            seen: set[int] = set()
            for image_id in request.image_ids:
                key = int(image_id)
                if key in seen:
                    continue  # belt-and-braces: the contract already deduped
                seen.add(key)
                image = found.get(key)
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
        categories: CategoryResolver,
    ) -> Any:
        plan = self._compute_batch_change(session, image, request, categories)
        if plan is _SKIPPED_READ_ONLY or plan is None:
            return plan
        sidecar_path = plan.sidecar_path
        # Re-verify against the stamp observed while reading: an external
        # writer can land a change between the indexed-stamp check and this
        # write, and the write must only land on the bytes the batch actually
        # parsed.  The check deliberately lives in the write phase (not in
        # ``_compute_batch_change``), so a preview never trips it.
        if stat_mtime(sidecar_path) != plan.live_mtime:
            raise TagManagerError(
                "sidecar changed while the batch was running",
                code="sidecar_conflict",
                status_code=409,
                retryable=True,
            )
        atomic_write_bytes(sidecar_path, plan.after_text.encode("utf-8"))
        change = {
            "image_id": plan.image_id,
            "sidecar": plan.sidecar_rel,
            "existed": plan.existed,
            "kind": plan.kind,
            "before": plan.before_text or "",
            "after": plan.after_text,
            "before_version": plan.before_version,
            "after_version": _sidecar_version(sidecar_path),
        }
        try:
            refreshed = load_sidecar(
                sidecar_path.with_suffix(".txt") if plan.kind == "tag_txt" else None,
                sidecar_path if plan.kind != "tag_txt" else None,
            )
            self.store.set_image_tags(
                plan.image_id,
                categories.categorize(refreshed.tags),
                sidecar_kind=plan.kind,
                sidecar_mtime=stat_mtime(sidecar_path),
            )
        except Exception:  # noqa: BLE001 - the write is journalled; undo/rescan repairs the index
            logger.warning(
                "tag manager batch index refresh failed for %s;"
                " the journal entry keeps the change recoverable",
                plan.sidecar_rel,
                exc_info=True,
            )
        return change

    def _compute_batch_change(
        self,
        session: Mapping[str, Any],
        image: Mapping[str, Any],
        request: BatchOperationRequest,
        categories: CategoryResolver,
        *,
        for_write: bool = True,
    ) -> Any:
        """Read, validate and render one target without touching the disk.

        Returns ``_SKIPPED_READ_ONLY`` for a read-only target, ``None`` when the
        operation leaves the sidecar unchanged, and a :class:`BatchChangePlan`
        otherwise.  Validation failures (stale ``mtime``, a disappeared
        sidecar, an oversized render, ...) raise exactly like the write path.
        ``for_write=False`` (the preview) resolves the sidecar path without the
        writable-root requirement, since nothing is written.
        """

        if str(image["sidecar_kind"]) == "raw_e621_json":
            return _SKIPPED_READ_ONLY  # read-only surfaces are skipped, never half-edited
        content, _live_mtime = load_content_and_mtime(
            paths=sidecar_paths(self.allowlist, session, image)
        )
        if content.kind == "raw_e621_json":
            return _SKIPPED_READ_ONLY  # the index row is stale; never edit a read-only file
        indexed_kind = str(image["sidecar_kind"])
        if content.kind == "none" and indexed_kind != "none":
            # The indexed sidecar vanished (or blanked) after the scan.  Never
            # resurrect it as a degraded fresh document, and never trust the
            # mtime comparison alone: an index row without a stamp would
            # otherwise pass and write over the deletion.
            raise TagManagerError(
                "sidecar changed since it was indexed",
                code="sidecar_conflict",
                status_code=409,
                retryable=True,
            )
        # The sidecar on disk is authoritative for the format: a stale index
        # row must not render one format over another (e.g. tag_txt rendering
        # over a tags_json document the scan has not seen yet).
        if content.kind != "none":
            effective_kind = content.kind
        elif indexed_kind in {"tag_txt", "tags_json", "standard_json"}:
            effective_kind = cast(Literal["tag_txt", "tags_json", "standard_json"], indexed_kind)
        else:
            effective_kind = "tag_txt"
        sidecar_rel = _sidecar_rel_for_kind(str(image["relative_path"]), effective_kind)
        sidecar_path = resolve_sidecar(
            self.allowlist, session, image, sidecar_rel, for_write=for_write
        )
        before_text = _read_sidecar_text(sidecar_path)
        before_version = _sidecar_version(sidecar_path)
        indexed_mtime = image.get("sidecar_mtime")
        live_mtime = stat_mtime(sidecar_path)
        if indexed_mtime != live_mtime:
            raise TagManagerError(
                "sidecar changed since it was indexed",
                code="sidecar_conflict",
                status_code=409,
                retryable=True,
            )

        before_tags = list(content.tags)
        after_tags: list[str]
        if effective_kind == "tag_txt":
            new_tags = _apply_tag_op(list(content.tags), request)
            if new_tags is None:
                return None
            after_text = render_tag_txt(new_tags)
            after_tags = list(new_tags)
        elif effective_kind == "tags_json":
            entries = [dict(entry) for entry in content.tag_entries]
            new_entries = _apply_entry_op(entries, request, categories)
            if new_entries is None:
                return None
            after_text = render_tags_json(new_entries, document=content.document)
            after_tags = [str(entry["text"]) for entry in new_entries]
        else:
            document = dict(content.document or {})
            changed = False
            # The preview diffs the nine-field ``tags`` slot; remember its new
            # value when the op touched it so the samples show the real new
            # tag list rather than the pre-op one.
            new_tags_field: list[str] | None = None
            for field in BATCH_TAG_FIELDS:
                raw_values = document.get(field)
                # A nine-field list slot can hold a plain string (legacy or
                # hand-authored documents).  ``list("solo, wolf")`` would
                # explode it into single characters, so parse it with the same
                # rule the sidecar loader uses and write back the upgraded
                # list.
                if isinstance(raw_values, str):
                    values = list(_parse_tag_list(raw_values))
                elif raw_values is None:
                    values = []
                else:
                    values = [str(value) for value in raw_values]
                new_values = _apply_tag_op(values, request)
                if new_values is not None:
                    document[field] = new_values
                    changed = True
                    if field == "tags":
                        new_tags_field = list(new_values)
            if not changed:
                return None
            after_text = render_standard_json(document)
            after_tags = new_tags_field if new_tags_field is not None else before_tags

        if after_text == (before_text or ""):
            return None
        # A render over the 1 MiB read budget could never be loaded again;
        # refuse it before the write so the file stays untouched.
        _guard_write_size(after_text)
        return BatchChangePlan(
            image_id=int(image["id"]),
            file_name=str(image["file_name"]),
            sidecar_rel=sidecar_rel,
            sidecar_path=sidecar_path,
            kind=effective_kind,
            existed=before_text is not None,
            before_text=before_text,
            after_text=after_text,
            before_tags=before_tags,
            after_tags=after_tags,
            before_version=before_version,
            live_mtime=live_mtime,
        )

    def preview_batch(self, session_id: str, request: BatchOperationRequest) -> dict[str, Any]:
        """Describe what :meth:`batch_operation` would change, writing nothing.

        Runs under the same exclusive lock as the write so the described state
        cannot race a concurrent edit.  No file is touched, no journal entry is
        appended, the index is not refreshed and the redo stack is left alone;
        the response reports the same tallies plus the effective target formats
        and a bounded sample of before/after tag diffs.
        """

        session = require_session(self.store, session_id)
        with self._locks.exclusive(session_id):
            # Lock-window re-check, same as batch_operation: a concurrent
            # delete must surface as 404 before any target is resolved.
            session = require_session(self.store, session_id)
            targets = self._resolve_targets(session_id, request)
            if len(targets) > MAX_BATCH_IMAGES:
                raise TagManagerError(
                    f"batch operations are capped at {MAX_BATCH_IMAGES} images",
                    code="batch_too_large",
                    status_code=413,
                )
            formats = {"tag_txt": 0, "tags_json": 0, "standard_json": 0, "none": 0}
            samples: list[dict[str, Any]] = []
            affected = 0
            no_change = 0
            skipped_read_only = 0
            will_create = 0
            if targets:
                categories = CategoryResolver(self.tag_database, str(session["profile"]))
                for image in targets:
                    plan = self._compute_batch_change(
                        session, image, request, categories, for_write=False
                    )
                    if plan is _SKIPPED_READ_ONLY:
                        skipped_read_only += 1
                        continue
                    if plan is None:
                        no_change += 1
                        continue
                    affected += 1
                    # ``effective_kind`` falls back to tag_txt for a target with
                    # no sidecar, so a freshly created sidecar is counted under
                    # tag_txt; ``will_create`` tallies those separately and the
                    # ``none`` bucket therefore stays empty.
                    formats[plan.kind] = formats.get(plan.kind, 0) + 1
                    if not plan.existed:
                        will_create += 1
                    if len(samples) < PREVIEW_SAMPLE_LIMIT:
                        samples.append({
                            "image_id": plan.image_id,
                            "file_name": plan.file_name,
                            "kind": plan.kind,
                            "before_tags": plan.before_tags,
                            "after_tags": plan.after_tags,
                        })
            return {
                "targets": len(targets),
                "affected": affected,
                "no_change": no_change,
                "skipped_read_only": skipped_read_only,
                "will_create": will_create,
                "formats": formats,
                "samples": samples,
            }

    # -- undo / redo -------------------------------------------------------

    def undo(self, session_id: str) -> dict[str, Any]:
        require_session(self.store, session_id)
        with self._locks.exclusive(session_id):
            # Lock-window re-check: a concurrent delete must surface as 404
            # before any journal lookup, not as a misleading undo_empty.
            require_session(self.store, session_id)
            entry = self.store.latest_journal_entry(session_id, undone=False)
            if entry is None:
                raise TagManagerError(
                    "nothing to undo",
                    code="undo_empty",
                    status_code=409,
                )
            self._replay_changes(
                session_id, entry["changes"], use="before", entry_id=int(entry["id"])
            )
            self.store.set_journal_undone(int(entry["id"]), True)
            return {"journal_id": int(entry["id"]), "reverted": len(entry["changes"])}

    def redo(self, session_id: str) -> dict[str, Any]:
        require_session(self.store, session_id)
        with self._locks.exclusive(session_id):
            # Lock-window re-check, same as undo.
            require_session(self.store, session_id)
            # Undo walks the live history newest-first; redo must mirror it by
            # replaying the *oldest* undone entry first, or a multi-step undo
            # would redo the steps out of order.
            entry = self.store.next_redo_entry(session_id)
            if entry is None:
                raise TagManagerError(
                    "nothing to redo",
                    code="redo_empty",
                    status_code=409,
                )
            self._replay_changes(
                session_id, entry["changes"], use="after", entry_id=int(entry["id"])
            )
            self.store.set_journal_undone(int(entry["id"]), False)
            return {"journal_id": int(entry["id"]), "reapplied": len(entry["changes"])}

    def _replay_changes(
        self,
        session_id: str,
        changes: list[Mapping[str, Any]],
        *,
        use: str,
        entry_id: int,
    ) -> None:
        session = require_session(self.store, session_id)
        require_writable_root(self.allowlist, session)
        categories = CategoryResolver(self.tag_database, str(session["profile"]))
        other = "after" if use == "before" else "before"
        # Phase 1 validates every change against the live sidecars, so a
        # conflict, format mismatch or corrupt journal aborts before any file
        # is touched: undo/redo never applies half an entry.
        planned: list[tuple[Mapping[str, Any], Path, str]] = []
        for change in changes:
            image = self.store.get_image(session_id, int(change["image_id"]))
            if image is None:
                continue  # the image row is gone; nothing to restore
            sidecar_rel = str(change["sidecar"])
            sidecar_path = resolve_sidecar(self.allowlist, session, image, sidecar_rel)
            slot_kind = self._journal_slot_kind(change, entry_id, sidecar_rel)
            self._assert_replay_matches(
                sidecar_path, change, other=other, slot_kind=slot_kind
            )
            self._assert_journal_text_valid(
                str(change[use]), slot_kind, sidecar_rel
            )
            planned.append((change, sidecar_path, slot_kind))
        # Phase 2 applies; every planned text was already proven to fit.
        for change, sidecar_path, slot_kind in planned:
            text = str(change[use])
            if not text and not change["existed"]:
                sidecar_path.unlink(missing_ok=True)
                self.store.set_image_tags(
                    int(change["image_id"]),
                    [],
                    sidecar_kind="none",
                    sidecar_mtime=None,
                    # Clear the recorded path: leaving the old extension would
                    # make a later save of another format reuse this stale
                    # suffix and write the wrong file.
                    sidecar_path=None,
                )
                continue
            atomic_write_bytes(sidecar_path, text.encode("utf-8"))
            content = load_sidecar(
                sidecar_path.with_suffix(".txt") if slot_kind == "tag_txt" else None,
                sidecar_path if slot_kind != "tag_txt" else None,
            )
            journalled_kind = str(change.get("kind") or "")
            if journalled_kind and journalled_kind != content.kind:
                # The restored bytes win over the journalled kind: the index
                # must describe what is actually on disk.
                logger.warning(
                    "undo/redo journal entry %s change for image %s recorded kind"
                    " %s but the restored sidecar parses as %s; trusting the"
                    " sidecar content",
                    entry_id, change["image_id"], journalled_kind, content.kind,
                )
            kind = content.kind
            self.store.set_image_tags(
                int(change["image_id"]),
                categories.categorize(content.tags) if kind != "none" else [],
                sidecar_kind=kind,
                sidecar_mtime=stat_mtime(sidecar_path) if kind != "none" else None,
            )


__all__ = [
    "JOURNAL_DEPTH",
    "MAX_BATCH_IMAGES",
    "PREVIEW_SAMPLE_LIMIT",
    "BatchChangePlan",
    "SessionEditor",
    "assert_sidecar_not_stale",
    "content_payload",
    "content_tag_strings",
    "load_content_and_mtime",
    "require_writable_root",
    "sidecar_paths",
]
