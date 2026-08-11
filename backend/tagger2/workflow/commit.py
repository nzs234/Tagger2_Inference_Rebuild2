"""Annotation backup, staged export and atomic commit.

Ordering guarantees, mirroring the source project:

1. Original ``.txt`` / ``.json`` annotations are archived into a verified ZIP
   before any dataset write happens.
2. Results are written into a per-job staging tree and validated there.
3. Staged files are promoted with :func:`os.replace`, one file at a time, with a
   journal so an interrupted commit can be replayed or rolled back.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .contracts import utc_now


class CommitError(RuntimeError):
    """Raised when a backup, staging or commit operation cannot proceed safely."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    """Reject any relative path that could escape its root."""

    if not value or "\x00" in value:
        raise CommitError("annotation path is invalid")
    normal = value.replace("\\", "/")
    parts = [part for part in normal.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise CommitError("annotation path escapes the dataset root")
    if Path(normal).is_absolute() or (len(normal) > 1 and normal[1] == ":"):
        raise CommitError("annotation path must be relative")
    return "/".join(parts)


def write_annotation_backup(
    dataset_root: Path,
    backup_zip: Path,
    annotation_keys: Iterable[str],
) -> Path:
    """Archive existing annotations into a verified ZIP64 backup.

    Writes to a ``.partial`` file and verifies every recorded entry against the
    embedded manifest before renaming into place, so a truncated backup can
    never be mistaken for a usable one.
    """

    dataset_root = Path(dataset_root)
    target = Path(backup_zip)
    partial = target.with_suffix(target.suffix + ".partial")

    if target.exists() or partial.exists():
        raise CommitError("backup destination already exists")
    target.parent.mkdir(parents=True, exist_ok=True)

    manifest_lines: list[bytes] = []
    try:
        with zipfile.ZipFile(
            partial, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for key in annotation_keys:
                relative_key = _safe_relative(key)
                for suffix in (".txt", ".json"):
                    relative = relative_key + suffix
                    source = dataset_root / Path(relative.replace("/", os.sep))
                    entry: dict[str, object] = {
                        "path": relative,
                        "exists": source.is_file(),
                    }
                    if source.is_file():
                        stat = source.stat()
                        entry.update(
                            {
                                "size": stat.st_size,
                                "mtimeNs": stat.st_mtime_ns,
                                "sha256": sha256_file(source),
                            }
                        )
                        archive.write(source, relative)
                    manifest_lines.append(
                        (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                            "utf-8"
                        )
                    )
            archive.writestr("manifest.jsonl", b"".join(manifest_lines))

        with zipfile.ZipFile(partial) as archive:
            if archive.testzip() is not None:
                raise CommitError("backup verification failed")
            with archive.open("manifest.jsonl") as manifest:
                for raw in manifest:
                    entry = json.loads(raw.decode("utf-8"))
                    if not entry["exists"]:
                        continue
                    info = archive.getinfo(entry["path"])
                    if info.file_size != entry["size"]:
                        raise CommitError("backup verification failed")
                    if sha256_bytes(archive.read(info)) != entry["sha256"]:
                        raise CommitError("backup verification failed")

        os.replace(partial, target)
        return target
    except Exception:
        if partial.exists():
            partial.unlink()
        raise


def restore_annotation_backup(backup_zip: Path, dataset_root: Path) -> int:
    """Restore archived annotations, verifying each entry before writing."""

    backup = Path(backup_zip)
    dataset_root = Path(dataset_root)
    if not backup.is_file():
        raise CommitError("backup archive is unavailable")
    if not dataset_root.is_dir():
        raise CommitError("dataset root is unavailable")

    restored = 0
    with zipfile.ZipFile(backup) as archive:
        if archive.testzip() is not None or "manifest.jsonl" not in archive.namelist():
            raise CommitError("backup verification failed")
        with archive.open("manifest.jsonl") as manifest:
            for raw in manifest:
                entry = json.loads(raw.decode("utf-8"))
                relative = _safe_relative(str(entry["path"]))
                target = dataset_root / Path(relative.replace("/", os.sep))
                if not entry["exists"]:
                    # The annotation did not exist before the run; remove ours.
                    if target.is_file():
                        target.unlink()
                    restored += 1
                    continue
                info = archive.getinfo(relative)
                data = archive.read(info)
                if sha256_bytes(data) != entry["sha256"]:
                    raise CommitError("backup archive digest mismatch")
                target.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(target, data)
                os.utime(target, ns=(entry["mtimeNs"], entry["mtimeNs"]))
                restored += 1
    return restored


def _atomic_write(target: Path, data: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


@dataclass(frozen=True)
class StagedFile:
    """One validated file waiting to be promoted into the dataset."""

    relative_path: str
    sha256: str
    size: int


class ExportStaging:
    """Per-job staging tree for validated output files."""

    def __init__(self, staging_root: Path):
        self.staging_root = Path(staging_root)
        self.staging_root.mkdir(parents=True, exist_ok=True)

    def stage(self, relative_path: str, data: bytes) -> StagedFile:
        relative = _safe_relative(relative_path)
        target = self.staging_root / Path(relative.replace("/", os.sep))
        _atomic_write(target, data)
        return StagedFile(
            relative_path=relative, sha256=sha256_bytes(data), size=len(data)
        )

    def staged_path(self, relative_path: str) -> Path:
        return self.staging_root / Path(_safe_relative(relative_path).replace("/", os.sep))


class CommitJournal:
    """Append-only record of a commit so it can be replayed or rolled back."""

    def __init__(self, journal_path: Path):
        self.journal_path = Path(journal_path)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict[str, object]) -> None:
        record = dict(event)
        record.setdefault("at", utc_now())
        with self.journal_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def entries(self) -> list[dict[str, object]]:
        if not self.journal_path.is_file():
            return []
        records: list[dict[str, object]] = []
        with self.journal_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records


def commit_staged_files(
    dataset_root: Path,
    staging: ExportStaging,
    staged: Sequence[StagedFile],
    journal: CommitJournal,
) -> int:
    """Promote validated staged files into the dataset one atomic rename at a time.

    Each file is verified against its recorded digest immediately before being
    promoted, so a staging tree corrupted after validation is refused rather
    than committed.
    """

    dataset_root = Path(dataset_root)
    if not dataset_root.is_dir():
        raise CommitError("dataset root is unavailable")

    journal.append({"event": "commit_started", "files": len(staged)})
    committed = 0
    try:
        for item in staged:
            source = staging.staged_path(item.relative_path)
            if not source.is_file():
                raise CommitError(f"staged file is missing: {item.relative_path}")
            data = source.read_bytes()
            if sha256_bytes(data) != item.sha256 or len(data) != item.size:
                raise CommitError(f"staged file changed after validation: {item.relative_path}")

            target = dataset_root / Path(item.relative_path.replace("/", os.sep))
            _atomic_write(target, data)
            committed += 1
            journal.append(
                {
                    "event": "file_committed",
                    "path": item.relative_path,
                    "sha256": item.sha256,
                }
            )
    except Exception as exc:
        journal.append({"event": "commit_failed", "committed": committed, "error": str(exc)})
        raise

    journal.append({"event": "commit_completed", "committed": committed})
    return committed


__all__ = [
    "CommitError",
    "CommitJournal",
    "ExportStaging",
    "StagedFile",
    "commit_staged_files",
    "restore_annotation_backup",
    "sha256_bytes",
    "sha256_file",
    "write_annotation_backup",
]
