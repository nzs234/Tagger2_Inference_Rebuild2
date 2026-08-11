"""Workflow contracts: versioned job config, resource references, and protocols.

All contracts are immutable dataclasses with explicit schema versions.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# Schema versions
WORKFLOW_CONFIG_VERSION = 1
WORKFLOW_MANIFEST_VERSION = 1
WORKFLOW_ISSUE_VERSION = 1

# Type aliases
Profile = Literal["e621", "danbooru"]
WorkMode = Literal["in_place", "full_copy"]
OverwriteMode = Literal["incremental", "rebuild"]
ModuleId = Literal["caption", "classify", "replace", "ocr", "nl", "count_review", "policy", "token_budget", "export"]

# Validation patterns
RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
RESOURCE_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Canonical JSON encoding for consistent hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    """Compute SHA-256 hash of canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowPathRef:
    """Path reference using root ID + relative path.
    
    API never returns absolute paths; all paths are relative to a registered root.
    """
    root_id: str
    relative_path: str

    def __post_init__(self) -> None:
        if not self.root_id:
            raise ValueError("root_id cannot be empty")
        if "\x00" in self.relative_path:
            raise ValueError("relative_path cannot contain NUL")


@dataclass(frozen=True)
class WorkflowResourceManifestV1:
    """Resource manifest with content-addressed fingerprint."""
    resource_id: str
    resource_fingerprint: str
    category: str
    created_at: str
    source_url: str | None = None
    source_timestamp: str | None = None
    builder_version: str | None = None

    def __post_init__(self) -> None:
        if not RESOURCE_ID_PATTERN.match(self.resource_id):
            raise ValueError(f"invalid resource_id: {self.resource_id}")
        if not RESOURCE_FINGERPRINT_PATTERN.match(self.resource_fingerprint):
            raise ValueError(f"invalid resource_fingerprint: {self.resource_fingerprint}")


@dataclass(frozen=True)
class WorkflowIssueV1:
    """Workflow issue with severity and blocking status."""
    issue_id: str
    module_id: str
    code: str
    severity: Literal["info", "warning", "error"]
    blocking: bool
    message: str
    sample_id: int | None = None
    relative_image_path: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class WorkflowSnapshotV1:
    """Immutable job configuration snapshot."""
    job_id: str
    config_version: int
    profile: Profile
    work_mode: WorkMode
    overwrite_mode: OverwriteMode
    source_root: WorkflowPathRef
    output_root: WorkflowPathRef | None
    config_hash: str
    resource_fingerprints: dict[str, str]
    created_at: str


@dataclass(frozen=True)
class WorkflowJobConfigV1:
    """Versioned workflow job configuration.
    
    This is the single source of truth for a workflow job.
    All paths are WorkflowPathRef to enforce root boundaries.
    """
    profile: Profile
    work_mode: WorkMode
    overwrite_mode: OverwriteMode
    source_root: WorkflowPathRef
    output_root: WorkflowPathRef | None = None
    recursive: bool = False
    
    # Caption configuration
    caption: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "resource_id": "caption-e621-eva02-large-full-v1",
        "threshold_mode": "model_default",
        "overwrite_txt": False,
        "input_txt_mode": "tag",  # "tag" or "nl"
    })
    
    # Classify configuration
    classify: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "resource_id": "classify-e621-20260724-v1",
        "overwrite_json": False,
        "overwrite_count": False,
    })
    
    # Replace configuration
    replace: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "resource_id": "replace-e621-20260726-v2",
    })
    
    # OCR configuration
    ocr: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "resource_id": "ocr-ppocrv5-server-paddle-v1",
        "min_confidence": 0.5,
        "force_reprocess": False,
    })
    
    # NL configuration
    nl: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "reuse_original_nl": True,
        "api_enabled": True,
        "use_image": True,
        "use_full_json": False,
        "prompt_preset": "general",
        "length": "medium",  # "short" (2-3), "medium" (4-5), "long" (6-8) sentences
    })
    
    # Count review configuration
    count_review: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
    })
    
    # Policy configuration
    policy: dict[str, Any] = field(default_factory=lambda: {
        "enabled": False,
        "seed": "workflow-default-v1",
        "directory_to_artist": True,
        "artist_dropout": 0.0,
        "quality_dropout": 0.0,
        "appearance_nl_solo_drop_nl": 0.70,
        "appearance_nl_solo_drop_appearance": 0.05,
        "appearance_nl_non_solo_drop_nl": 0.05,
        "appearance_nl_non_solo_drop_appearance": 0.70,
    })
    
    # Token budget configuration
    token_budget: dict[str, Any] = field(default_factory=lambda: {
        "enabled": True,
        "tokenizer_resource_id": "Qwen/Qwen3-0.6B",
        "max_tokens": 512,
    })
    
    # Export configuration
    export: dict[str, Any] = field(default_factory=lambda: {
        "format": "both",  # "json", "txt", "both"
    })
    
    compatibility_mode: bool = True
    schema_version: int = WORKFLOW_CONFIG_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of this configuration."""
        return asdict(self)

    def config_hash(self) -> str:
        """Compute configuration hash for change detection.

        ``asdict`` flattens nested path references so the digest stays stable
        and JSON-serializable; hashing ``__dict__`` would fail on dataclasses.
        """
        return sha256_json(self.to_dict())


__all__ = [
    "WORKFLOW_CONFIG_VERSION",
    "WORKFLOW_MANIFEST_VERSION",
    "WORKFLOW_ISSUE_VERSION",
    "Profile",
    "WorkMode",
    "OverwriteMode",
    "ModuleId",
    "utc_now",
    "canonical_json",
    "sha256_json",
    "WorkflowPathRef",
    "WorkflowResourceManifestV1",
    "WorkflowIssueV1",
    "WorkflowSnapshotV1",
    "WorkflowJobConfigV1",
]
