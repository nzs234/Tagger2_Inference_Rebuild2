"""Durable, content-addressed projection checkpoints for review continuations."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import canonical_json

CHECKPOINT_SCHEMA_VERSION = 1
CHECKPOINT_DIRNAME = "checkpoints"
CHECKPOINT_FILES = {
    "projection": "projection.json",
    "count_review": "count_review.json",
    "token_review": "token_review.json",
}


class ProjectionCheckpointError(RuntimeError):
    """Raised when a private review checkpoint is missing or invalid."""


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _sample_manifest(samples: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "sample_id": int(sample.sample_id),
            "relative_image_path": str(sample.relative_image_path),
            "annotation_key": str(sample.annotation_key),
            "image_format": str(sample.image_format),
        }
        for sample in samples
    ]


def _path(workspace: Path, stage_cursor: str) -> Path:
    try:
        filename = CHECKPOINT_FILES[stage_cursor]
    except KeyError as exc:
        raise ProjectionCheckpointError(f"unsupported checkpoint stage: {stage_cursor}") from exc
    return Path(workspace) / CHECKPOINT_DIRNAME / filename


def write_projection_checkpoint(
    workspace: Path,
    *,
    stage_cursor: str,
    job_id: str,
    config_hash: str,
    resource_fingerprints: Mapping[str, str],
    samples: Sequence[Any],
    projections: Mapping[str, Mapping[str, Any]],
    report: Mapping[str, Any],
) -> tuple[Path, str, int]:
    """Write one immutable projection checkpoint and return path, digest and size."""

    if not projections:
        raise ProjectionCheckpointError("cannot checkpoint an empty projection set")
    normalized: dict[str, dict[str, Any]] = {}
    sample_ids = {str(int(sample.sample_id)) for sample in samples}
    for sample_id, projection in projections.items():
        key = str(sample_id)
        if key not in sample_ids:
            raise ProjectionCheckpointError(f"projection references unknown sample: {key}")
        if set(projection) != {
            "quality", "count", "character", "series", "artist",
            "appearance", "tags", "environment", "nl",
        }:
            raise ProjectionCheckpointError(f"projection for sample {key} is not nine-field data")
        normalized[key] = dict(projection)

    payload: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "stage_cursor": stage_cursor,
        "job_id": job_id,
        "config_hash": config_hash,
        "resource_fingerprints": dict(resource_fingerprints),
        "samples": _sample_manifest(samples),
        "projections": normalized,
        "report": dict(report),
    }
    envelope = dict(payload)
    envelope["digest"] = _digest(payload)
    data = (canonical_json(envelope) + "\n").encode("utf-8")

    target = _path(Path(workspace), stage_cursor)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            existing = target.read_bytes()
        except OSError as exc:
            raise ProjectionCheckpointError("projection checkpoint cannot be read") from exc
        if existing != data:
            raise ProjectionCheckpointError(
                f"immutable {stage_cursor} checkpoint already exists with different content"
            )
        return target, str(envelope["digest"]), len(existing)

    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ProjectionCheckpointError("projection checkpoint could not be written") from exc
    return target, str(envelope["digest"]), len(data)


def load_projection_checkpoint(
    workspace: Path,
    *,
    job_id: str,
    config_hash: str,
    resource_fingerprints: Mapping[str, str],
    samples: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    """Load the newest valid checkpoint, preferring the later token stage."""

    for stage_cursor in ("token_review", "count_review", "projection"):
        target = _path(Path(workspace), stage_cursor)
        if not target.is_file():
            continue
        try:
            envelope = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectionCheckpointError("projection checkpoint is unreadable") from exc
        if not isinstance(envelope, dict):
            raise ProjectionCheckpointError("projection checkpoint must be an object")
        supplied_digest = envelope.pop("digest", None)
        if not isinstance(supplied_digest, str) or supplied_digest != _digest(envelope):
            raise ProjectionCheckpointError("projection checkpoint digest mismatch")
        if envelope.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ProjectionCheckpointError("unsupported projection checkpoint schema")
        if envelope.get("stage_cursor") != stage_cursor:
            raise ProjectionCheckpointError("projection checkpoint stage mismatch")
        if envelope.get("job_id") != job_id:
            raise ProjectionCheckpointError("projection checkpoint job mismatch")
        if envelope.get("config_hash") != config_hash:
            raise ProjectionCheckpointError("projection checkpoint configuration mismatch")
        if dict(envelope.get("resource_fingerprints") or {}) != dict(resource_fingerprints):
            raise ProjectionCheckpointError("projection checkpoint resource fingerprint mismatch")
        stored_samples = envelope.get("samples")
        if not isinstance(stored_samples, list):
            raise ProjectionCheckpointError("projection checkpoint sample manifest is invalid")
        if samples is not None and stored_samples != _sample_manifest(samples):
            raise ProjectionCheckpointError("projection checkpoint sample manifest changed")
        projections = envelope.get("projections")
        if not isinstance(projections, dict) or not projections:
            raise ProjectionCheckpointError("projection checkpoint has no projections")
        report = envelope.get("report")
        if not isinstance(report, dict):
            raise ProjectionCheckpointError("projection checkpoint report is invalid")
        envelope["digest"] = supplied_digest
        return envelope
    return None


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "ProjectionCheckpointError",
    "load_projection_checkpoint",
    "write_projection_checkpoint",
]
