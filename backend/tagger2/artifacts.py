"""Atomic Anima artifact writing and source/config based skip checks."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Protocol, Sequence

from .anima import ANIMA_SCHEMA_VERSION, AnimaPayload, anima_dict, normalize_anima_payload
from .config import DEFAULT_IMAGE_EXTENSIONS
from .schemas import TagItem
from .storage import ArtifactRecord, config_digest


TXT_FIELD_ORDER = ("quality", "count", "character", "series", "artist", "appearance", "tags", "environment")
LOCAL_TAG_SCHEMA_VERSION = "local-tags-v2"
HYBRID_NL_TAGS_SCHEMA_VERSION = "hybrid-nl-tags-v1"
HYBRID_LOCAL_TAGS_SCHEMA_VERSION = "hybrid-local-tags-v1"
ArtifactValidator = Callable[..., bool]
# Sidecar extensions this application writes next to an image.  Included so a
# target that already points at a sidecar is not suffixed twice.
SIDECAR_EXTENSIONS = frozenset({".txt", ".json"})
KNOWN_ARTIFACT_EXTENSIONS = DEFAULT_IMAGE_EXTENSIONS | SIDECAR_EXTENSIONS
# Per-name limit shared by NTFS (UTF-16 units) and common Linux filesystems
# (bytes).  Applied to temporary names so long image filenames stay writable.
MAX_NAME_BYTES = 255
# Windows rejects paths above this length unless long paths are enabled.
MAX_WINDOWS_PATH = 259
# Leading dot plus ``mkstemp``'s random component and the ``.tmp`` suffix.
_TEMP_NAME_OVERHEAD = 16


class ArtifactStorage(Protocol):
    def find_artifact(
        self,
        item_id: str | None = None,
        *,
        kind: str = "anima_json",
        path: str | Path | None = None,
        source_hash: str | None = None,
        config_hash: str | None = None,
        schema_version: str | None = None,
    ) -> ArtifactRecord | None: ...

    def record_artifact(
        self,
        *,
        job_id: str,
        item_id: str,
        kind: str,
        path: str | Path,
        source_hash: str,
        config_hash: str,
        schema_version: str,
        content_hash: str,
        artifact_id: str | None = None,
    ) -> ArtifactRecord: ...


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    json_path: Path
    txt_path: Path | None
    source_hash: str
    config_hash: str
    json_hash: str
    txt_hash: str | None


def hash_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_config(value: Mapping[str, Any]) -> str:
    return config_digest(value)


def strip_artifact_suffix(name: str) -> str:
    """Drop only a real trailing image/sidecar extension from a file name.

    ``Path.stem`` and ``Path.with_suffix`` treat everything after the last dot
    as an extension.  Generation filenames carry dots inside the name, so the
    two-step ``stem`` then ``with_suffix`` idiom silently truncates them::

        43900-...,_(andyredtiger_1.2),yellow.png
          -> stem            43900-...,_(andyredtiger_1.2),yellow
          -> with_suffix()   43900-...,_(andyredtiger_1.txt   # wrong

    Only a suffix that is a known image or sidecar extension is removed here;
    every other dot in the name is preserved verbatim so the sidecar always
    pairs with its image.
    """

    suffix = PurePosixPath(name).suffix
    if suffix and suffix.casefold() in KNOWN_ARTIFACT_EXTENSIONS:
        return name[: -len(suffix)]
    return name


def replace_suffix(path: str | Path, suffix: str) -> Path:
    """Return the sibling of ``path`` whose extension is ``suffix``.

    Used for every image/sidecar pairing so a TXT always lands on the image
    name plus ``.txt``.
    """

    source = Path(path)
    return source.with_name(strip_artifact_suffix(source.name) + suffix)


def numbered_name(name: str, index: int) -> str:
    """Return ``name (index).ext`` without truncating dots inside the name."""

    base = strip_artifact_suffix(name)
    return f"{base} ({index}){name[len(base):]}"


def numbered_path(path: Path, index: int) -> Path:
    """Return the conflict-renamed sibling of ``path``."""

    return path.with_name(numbered_name(path.name, index))


def _bounded_temp_prefix(name: str, parent: Path) -> str:
    """Keep ``mkstemp`` names inside the filesystem limits.

    The temporary name is ``.<name>.<random>.tmp``, so a long image filename
    pushes the sidecar temporary past the 255-unit per-name limit (and past
    Windows' 260-character path limit) even though the image itself was
    writable.  Only the temporary name is shortened; the destination name is
    never altered.
    """

    budget = MAX_NAME_BYTES - _TEMP_NAME_OVERHEAD
    if os.name == "nt":
        available = MAX_WINDOWS_PATH - len(str(parent)) - 1 - _TEMP_NAME_OVERHEAD
        budget = min(budget, available)
    budget = max(budget, 0)
    encoded = name.encode("utf-8", "surrogatepass")
    if len(encoded) > budget:
        name = encoded[:budget].decode("utf-8", "ignore")
    return f".{name}." if name else "."


def _create_temporary(destination: Path) -> tuple[int, str]:
    """Create the temporary file used for an atomic replace.

    The descriptive prefix keeps orphaned temporaries diagnosable.  A path that
    is still too long for the platform falls back to a minimal prefix so a
    long-named sidecar is written instead of failing next to its image.
    """

    try:
        return tempfile.mkstemp(
            prefix=_bounded_temp_prefix(destination.name, destination.parent),
            suffix=".tmp",
            dir=destination.parent,
        )
    except OSError:
        return tempfile.mkstemp(prefix=".", suffix=".tmp", dir=destination.parent)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Write bytes with flush/fsync and an atomic same-volume replacement."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.is_symlink():
        raise ValueError("refusing to replace a symbolic-link artifact")
    descriptor, temporary = _create_temporary(destination)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
        # Persist the directory entry where the platform exposes directory
        # handles.  Windows rejects this operation, so it is best-effort.
        try:
            directory_fd = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return destination


def atomic_write_text(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str | Path, value: Mapping[str, Any]) -> Path:
    data = json.dumps(dict(value), ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, data)


def render_anima_txt(payload: AnimaPayload | Mapping[str, Any]) -> str:
    normalized = _coerce_payload(payload)
    data = anima_dict(normalized)
    values: list[str] = []
    for field in TXT_FIELD_ORDER:
        value = data[field]
        if isinstance(value, list):
            values.extend(item.strip() for item in value if item.strip())
        elif str(value).strip():
            values.append(str(value).strip())
    # A final newline produces stable text files across editors and platforms.
    return ", ".join(values) + ("\n" if values else "")


def render_online_txt(caption: str, tags: Sequence[str] = (), *, include_tags: bool = False) -> str:
    """Render online TXT output with NL first and optional TAG content."""

    values = [str(caption).strip()] if str(caption).strip() else []
    if include_tags:
        tag_text = ", ".join(str(value).strip() for value in tags if str(value).strip())
        if tag_text:
            values.append(tag_text)
    return "\n\n".join(values) + ("\n" if values else "")


def render_hybrid_nl_tags(caption: str, tags: Sequence[str]) -> str:
    """Render a combined batch caption with its merged local TAG line.

    The delimiter is intentionally a complete line so downstream caption
    tooling can split natural language from booru tags without heuristics.
    """

    unique_tags: list[str] = []
    seen: set[str] = set()
    for value in tags:
        text = str(value).strip()
        if not text:
            continue
        key = " ".join(text.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique_tags.append(text)
    return f"{str(caption).strip()}\n|||\n{', '.join(unique_tags)}\n"


def validate_anima_file(path: str | Path, *, expected_hash: str | None = None) -> bool:
    artifact = Path(path)
    if not artifact.is_file() or artifact.is_symlink():
        return False
    try:
        if expected_hash and hash_file(artifact) != expected_hash:
            return False
        raw = json.loads(artifact.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, Mapping):
            return False
        normalize_anima_payload(raw, trigger_artist=str(raw.get("artist", "")))
        return True
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False


def validate_artifact_file(path: str | Path, *, expected_hash: str | None = None) -> bool:
    artifact = Path(path)
    if not artifact.is_file() or artifact.is_symlink():
        return False
    try:
        return not expected_hash or hash_file(artifact) == expected_hash
    except OSError:
        return False


def validate_local_tags_file(path: str | Path, *, expected_hash: str | None = None) -> bool:
    if not validate_artifact_file(path, expected_hash=expected_hash):
        return False
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(raw, Mapping) or set(raw) != {"tags"}:
            return False
        tags = raw.get("tags")
        if not isinstance(tags, list):
            return False
        for item in tags:
            TagItem.model_validate(item)
        return True
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False


def artifact_is_current(
    source_path: str | Path,
    artifact_path: str | Path,
    metadata: ArtifactRecord | Mapping[str, Any] | None,
    *,
    config_hash: str,
    schema_version: str = ANIMA_SCHEMA_VERSION,
    validator: ArtifactValidator = validate_anima_file,
) -> bool:
    """Return true only when source, config, schema and bytes all still match."""

    if metadata is None:
        return False
    get = (lambda key: getattr(metadata, key)) if isinstance(metadata, ArtifactRecord) else metadata.get
    try:
        source_hash = hash_file(source_path)
        if get("source_hash") != source_hash:
            return False
        if get("config_hash") != config_hash or get("schema_version") != schema_version:
            return False
        stored_path = get("path")
        if stored_path is None or Path(stored_path).resolve() != Path(artifact_path).resolve():
            return False
        return validator(artifact_path, expected_hash=str(get("content_hash")))
    except (OSError, TypeError, ValueError):
        return False


class ArtifactManager:
    def __init__(self, storage: ArtifactStorage, *, schema_version: str = ANIMA_SCHEMA_VERSION) -> None:
        self.storage = storage
        self.schema_version = schema_version

    def should_skip(
        self,
        *,
        item_id: str,
        source_path: str | Path,
        json_path: str | Path,
        config_hash: str,
    ) -> bool:
        return self.should_skip_file(
            item_id=item_id,
            source_path=source_path,
            artifact_path=json_path,
            kind="anima_json",
            config_hash=config_hash,
            schema_version=self.schema_version,
            validator=validate_anima_file,
        )

    def should_skip_file(
        self,
        *,
        item_id: str,
        source_path: str | Path,
        artifact_path: str | Path,
        kind: str,
        config_hash: str,
        schema_version: str,
        validator: ArtifactValidator = validate_artifact_file,
    ) -> bool:
        try:
            source_hash = hash_file(source_path)
        except OSError:
            return False
        metadata = self.storage.find_artifact(
            item_id,
            kind=kind,
            source_hash=source_hash,
            config_hash=config_hash,
            schema_version=schema_version,
        )
        if metadata is None:
            # A new job receives new item IDs.  Reuse a valid artifact from a
            # prior job when its path and all reproducibility hashes match.
            metadata = self.storage.find_artifact(
                None,
                kind=kind,
                path=artifact_path,
                source_hash=source_hash,
                config_hash=config_hash,
                schema_version=schema_version,
            )
        return artifact_is_current(
            source_path,
            artifact_path,
            metadata,
            config_hash=config_hash,
            schema_version=schema_version,
            validator=validator,
        )

    def write_bytes(
        self,
        *,
        job_id: str,
        item_id: str,
        source_path: str | Path,
        artifact_path: str | Path,
        kind: str,
        data: bytes,
        config_hash: str,
        schema_version: str,
    ) -> Path:
        source = Path(source_path)
        destination = atomic_write_bytes(artifact_path, data)
        self.storage.record_artifact(
            job_id=job_id,
            item_id=item_id,
            kind=kind,
            path=destination,
            source_hash=hash_file(source),
            config_hash=config_hash,
            schema_version=schema_version,
            content_hash=hash_bytes(data),
        )
        return destination

    def write_anima(
        self,
        *,
        job_id: str,
        item_id: str,
        source_path: str | Path,
        payload: AnimaPayload | Mapping[str, Any],
        config_hash: str,
        output_dir: str | Path | None = None,
        relative_path: str | Path | None = None,
        write_txt: bool = False,
    ) -> ArtifactWriteResult:
        source = Path(source_path)
        normalized = _coerce_payload(payload)
        if output_dir is None:
            base = source
        else:
            relative = Path(relative_path) if relative_path is not None else Path(source.name)
            # Caller supplies an allowlisted root.  Still reject absolute and
            # upward paths at this final filesystem boundary.
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("artifact relative path escapes output root")
            base = Path(output_dir) / relative
        json_path = replace_suffix(base, ".json")
        txt_path = replace_suffix(base, ".txt") if write_txt else None
        source_hash = hash_file(source)
        json_bytes = (json.dumps(anima_dict(normalized), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write_bytes(json_path, json_bytes)
        json_hash = hash_bytes(json_bytes)
        self.storage.record_artifact(
            job_id=job_id,
            item_id=item_id,
            kind="anima_json",
            path=json_path,
            source_hash=source_hash,
            config_hash=config_hash,
            schema_version=self.schema_version,
            content_hash=json_hash,
        )

        txt_hash: str | None = None
        if txt_path is not None:
            txt_bytes = render_anima_txt(normalized).encode("utf-8")
            atomic_write_bytes(txt_path, txt_bytes)
            txt_hash = hash_bytes(txt_bytes)
            self.storage.record_artifact(
                job_id=job_id,
                item_id=item_id,
                kind="anima_txt",
                path=txt_path,
                source_hash=source_hash,
                config_hash=config_hash,
                schema_version=self.schema_version,
                content_hash=txt_hash,
            )
        return ArtifactWriteResult(
            json_path=json_path,
            txt_path=txt_path,
            source_hash=source_hash,
            config_hash=config_hash,
            json_hash=json_hash,
            txt_hash=txt_hash,
        )

    def write_txt(
        self,
        *,
        job_id: str,
        item_id: str,
        source_path: str | Path,
        payload: AnimaPayload | Mapping[str, Any],
        config_hash: str,
        txt_path: str | Path,
    ) -> Path:
        """Atomically write and register a standalone Anima TXT artifact."""

        source = Path(source_path)
        destination = Path(txt_path)
        txt_bytes = render_anima_txt(_coerce_payload(payload)).encode("utf-8")
        atomic_write_bytes(destination, txt_bytes)
        self.storage.record_artifact(
            job_id=job_id,
            item_id=item_id,
            kind="anima_txt",
            path=destination,
            source_hash=hash_file(source),
            config_hash=config_hash,
            schema_version=self.schema_version,
            content_hash=hash_bytes(txt_bytes),
        )
        return destination


def write_anima_artifacts(
    source_path: str | Path,
    payload: AnimaPayload | Mapping[str, Any],
    *,
    output_dir: str | Path | None = None,
    write_txt: bool = False,
) -> tuple[Path, Path | None]:
    """Standalone atomic writer for tools that do not use the job database."""

    source = Path(source_path)
    base = source if output_dir is None else Path(output_dir) / source.name
    normalized = _coerce_payload(payload)
    json_path = replace_suffix(base, ".json")
    atomic_write_json(json_path, anima_dict(normalized))
    txt_path: Path | None = None
    if write_txt:
        txt_path = replace_suffix(base, ".txt")
        atomic_write_text(txt_path, render_anima_txt(normalized))
    return json_path, txt_path


def _coerce_payload(payload: AnimaPayload | Mapping[str, Any] | Any) -> AnimaPayload:
    """Accept this module's model, the shared Pydantic DTO, or a mapping."""

    if isinstance(payload, AnimaPayload):
        return payload
    if hasattr(payload, "model_dump"):
        data = dict(payload.model_dump())
    elif isinstance(payload, Mapping):
        data = dict(payload)
    else:
        raise TypeError("payload must be an Anima object or mapping")
    return normalize_anima_payload(data, trigger_artist=str(data.get("artist", "")))


__all__ = [
    "ArtifactManager",
    "ArtifactWriteResult",
    "HYBRID_LOCAL_TAGS_SCHEMA_VERSION",
    "HYBRID_NL_TAGS_SCHEMA_VERSION",
    "KNOWN_ARTIFACT_EXTENSIONS",
    "LOCAL_TAG_SCHEMA_VERSION",
    "MAX_NAME_BYTES",
    "MAX_WINDOWS_PATH",
    "SIDECAR_EXTENSIONS",
    "TXT_FIELD_ORDER",
    "artifact_is_current",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "hash_bytes",
    "hash_config",
    "hash_file",
    "numbered_name",
    "numbered_path",
    "render_anima_txt",
    "render_hybrid_nl_tags",
    "render_online_txt",
    "replace_suffix",
    "strip_artifact_suffix",
    "validate_anima_file",
    "validate_artifact_file",
    "validate_local_tags_file",
    "write_anima_artifacts",
]
