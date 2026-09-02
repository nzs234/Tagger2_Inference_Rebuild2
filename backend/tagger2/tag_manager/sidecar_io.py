"""Sidecar (annotation) reading and writing for the tag manager.

Three editable formats are supported: booru flat TXT (comma-separated tags),
the local tags JSON container (``{"tags": [...]}``) and the nine-field
standard JSON shared with the dataset workflow.  A raw e621 grouped JSON is
recognised but deliberately read-only.  Detection precedence matches
``workflow.dataset_import``: JSON wins over TXT, raw e621 wins over anything
else.  Serialization conventions mirror ``artifacts.atomic_write_json``
(``ensure_ascii=False, indent=2`` + trailing newline) and
``tag_output.format_local_tags`` (``, ``.join + trailing newline).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from ..workflow.raw_e621 import RawE621JsonError, parse_raw_e621_annotation

SidecarKind = Literal[
    "none",
    "tag_txt",
    "tags_json",
    "standard_json",
    "raw_e621_json",
]

# Same frozen order as the workflow nine-field contract.
NINE_FIELDS = (
    "quality",
    "count",
    "character",
    "series",
    "artist",
    "appearance",
    "tags",
    "environment",
    "nl",
)

# A sidecar may legitimately carry two thousand tag objects; the limit exists
# to bound memory and editor payloads, not to mirror the workflow's tighter
# TXT budget.
MAX_SIDECAR_BYTES = 1024 * 1024


class SidecarError(ValueError):
    """Raised when a sidecar cannot be parsed or rendered."""


@dataclass(frozen=True)
class SidecarContent:
    """Parsed sidecar payload.

    ``tags`` is always the flat, order-preserved tag view used for indexing
    and filtering.  Only one of ``raw_text`` / ``tag_entries`` / ``document``
    carries the format-native payload.
    """

    kind: SidecarKind
    tags: tuple[str, ...] = ()
    tag_entries: tuple[dict[str, Any], ...] = ()
    document: Mapping[str, Any] | None = None
    raw_text: str | None = None


def _read_bytes(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SidecarError(f"sidecar cannot be read: {exc}") from exc
    if b"\x00" in raw:
        raise SidecarError("sidecar contains NUL")
    if len(raw) > MAX_SIDECAR_BYTES:
        raise SidecarError("sidecar exceeds the 1 MiB limit")
    return raw


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SidecarError("sidecar is not UTF-8") from exc


def _parse_tag_list(text: str) -> tuple[str, ...]:
    """Split comma-separated tags; dedup casefolded, keep first occurrence."""

    tags: list[str] = []
    seen: set[str] = set()
    for part in text.replace("\n", ",").split(","):
        tag = part.strip()
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tuple(tags)


def _coerce_string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return _parse_tag_list(value)
    if isinstance(value, (list, tuple)):
        tags: list[str] = []
        seen: set[str] = set()
        for item in value:
            tag = str(item).strip()
            if not tag:
                continue
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                tags.append(tag)
        return tuple(tags)
    raise SidecarError("tag field must be a list or comma-separated string")


def _is_nine_field_document(document: Mapping[str, Any]) -> bool:
    return any(key in document for key in NINE_FIELDS if key != "tags")


def _load_json_document(raw: bytes, path: Path) -> Mapping[str, Any]:
    try:
        return json.loads(_decode(raw))
    except json.JSONDecodeError as exc:
        raise SidecarError(f"{path.name} is not valid JSON: {exc}") from exc


def load_sidecar(txt_path: Path | None, json_path: Path | None) -> SidecarContent:
    """Detect and parse the sidecar for one image.

    Either path may be ``None`` when the file does not exist; a blank TXT
    counts as no annotation.
    """

    if json_path is not None and json_path.is_file():
        raw = _read_bytes(json_path)
        if raw.strip():
            try:
                annotation = parse_raw_e621_annotation(raw)
            except RawE621JsonError as exc:
                # Fail closed like the workflow importer: a document with the
                # raw e621 grouped shape that is invalid is an error, never a
                # silent fallback to another format.
                raise SidecarError(f"raw e621 JSON is invalid: {exc}") from exc
            if annotation is not None:
                return SidecarContent(
                    kind="raw_e621_json",
                    tags=tuple(annotation.classify_tags),
                )
            document = _load_json_document(raw, json_path)
            if not isinstance(document, dict):
                raise SidecarError(f"{json_path.name} root is not an object")
            if _is_nine_field_document(document):
                return SidecarContent(
                    kind="standard_json",
                    tags=_coerce_string_list(document.get("tags", ())),
                    document=document,
                )
            if "tags" in document or "tag" in document:
                # Local tags container: object entries carry optional
                # category/score metadata; plain strings are legacy exports.
                source = document.get("tags", document.get("tag"))
                if isinstance(source, str):
                    entries = [
                        {"text": tag} for tag in _parse_tag_list(source)
                    ]
                elif isinstance(source, list):
                    entries = []
                    for item in source:
                        if isinstance(item, str):
                            text = item.strip()
                            if not text:
                                continue
                            entries.append({"text": text})
                        elif isinstance(item, dict):
                            text = str(item.get("text", "")).strip()
                            if not text:
                                raise SidecarError(
                                    f"{json_path.name} has a tag entry without text"
                                )
                            entry = dict(item)
                            entry["text"] = text
                            entries.append(entry)
                        else:
                            raise SidecarError(
                                f"{json_path.name} tag entries must be strings or objects"
                            )
                else:
                    raise SidecarError(f"{json_path.name} tags field has an unsupported type")
                return SidecarContent(
                    kind="tags_json",
                    tags=tuple(str(entry["text"]) for entry in entries),
                    tag_entries=tuple(entries),
                )
            raise SidecarError(
                f"{json_path.name} is neither a nine-field document nor a tags container"
            )

    if txt_path is not None and txt_path.is_file():
        text = _decode(_read_bytes(txt_path))
        if text.strip():
            return SidecarContent(
                kind="tag_txt",
                tags=_parse_tag_list(text),
                raw_text=text,
            )

    return SidecarContent(kind="none")


def render_tag_txt(tags: list[str]) -> str:
    """Serialize booru flat TXT: ``, ``-joined tags with a trailing newline."""

    cleaned = [tag.strip() for tag in tags if tag.strip()]
    return ", ".join(cleaned) + ("\n" if cleaned else "")


def render_tags_json(
    entries: Sequence[Mapping[str, Any]],
    *,
    document: Mapping[str, Any] | None = None,
) -> str:
    """Serialize a local tags JSON container, preserving unknown extra keys."""

    payload: dict[str, Any] = {}
    if document is not None:
        payload.update(
            {key: value for key, value in document.items() if key != "tags"}
        )
    payload["tags"] = list(entries)
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_standard_json(document: Mapping[str, Any]) -> str:
    """Serialize a nine-field document, freezing the canonical field order."""

    payload: dict[str, Any] = {key: document.get(key) for key in NINE_FIELDS}
    for key, value in document.items():
        if key not in payload:
            payload[key] = value
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def dedup_tags(tags: list[str]) -> list[str]:
    """Casefold-dedup while preserving order and original spelling."""

    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        cleaned = tag.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


__all__ = [
    "MAX_SIDECAR_BYTES",
    "NINE_FIELDS",
    "SidecarContent",
    "SidecarError",
    "SidecarKind",
    "dedup_tags",
    "load_sidecar",
    "render_standard_json",
    "render_tag_txt",
    "render_tags_json",
]
