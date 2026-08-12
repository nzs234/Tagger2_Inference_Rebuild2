"""Workflow contracts: versioned job config, resource references, and protocols.

All contracts are immutable dataclasses with explicit schema versions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, Literal

# Schema versions
WORKFLOW_CONFIG_VERSION = 1
WORKFLOW_CONFIG_V2_VERSION = 2
WORKFLOW_MANIFEST_VERSION = 1
WORKFLOW_ISSUE_VERSION = 1

# Type aliases
Profile = Literal["e621", "danbooru"]
WorkMode = Literal["in_place", "full_copy"]
OverwriteMode = Literal["incremental", "rebuild"]
ModuleId = Literal["caption", "classify", "replace", "ocr", "nl", "count_review", "policy", "token_budget", "export"]

# Per-section configuration schema.
#
# ``from_payload`` accepts partial sections and merges them onto the dataclass
# defaults, so validation is declarative rather than a chain of hand-written
# checks. Each entry is (type, constraint):
#   bool                     -> must be a real bool, not a truthy string
#   ("enum", allowed)        -> must be one of ``allowed``
#   ("num", low, high)       -> int/float within the inclusive range
#   ("int", low, high)       -> int within the inclusive range
#   "str"                    -> any string
#   "str?"                   -> string or None
_UNIT = ("num", 0.0, 1.0)

SECTION_SCHEMA: dict[str, dict[str, Any]] = {
    "caption": {
        "enabled": bool,
        "model_id": "str?",
        "threshold_mode": ("enum", ("model_default", "manual")),
        "threshold": ("num", 0.0, 1.0),
        "overwrite_txt": bool,
        "input_txt_mode": ("enum", ("tag", "nl")),
    },
    "classify": {
        "enabled": bool,
        "resource_id": "str?",
        "overwrite_json": bool,
        "overwrite_count": bool,
    },
    "replace": {
        "enabled": bool,
        "resource_id": "str?",
    },
    "ocr": {
        "enabled": bool,
        "resource_id": "str?",
        "min_confidence": _UNIT,
        "force_reprocess": bool,
    },
    "nl": {
        "enabled": bool,
        "provider_id": "str?",
        "model": "str?",
        "reuse_original_nl": bool,
        "api_enabled": bool,
        "use_image": bool,
        "use_full_json": bool,
        "prompt_preset": "str",
        "length": ("enum", ("short", "medium", "long")),
    },
    "count_review": {
        "enabled": bool,
    },
    "policy": {
        "enabled": bool,
        "seed": "str",
        "directory_to_artist": bool,
        "artist_dropout": _UNIT,
        "quality_dropout": _UNIT,
        "appearance_nl_solo_drop_nl": _UNIT,
        "appearance_nl_solo_drop_appearance": _UNIT,
        "appearance_nl_non_solo_drop_nl": _UNIT,
        "appearance_nl_non_solo_drop_appearance": _UNIT,
    },
    "token_budget": {
        "enabled": bool,
        "tokenizer_resource_id": "str?",
        "max_tokens": ("int", 1, 32768),
    },
    "export": {
        "format": ("enum", ("json", "txt", "flat_txt", "both")),
    },
}

ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "profile": ("e621", "danbooru"),
    "work_mode": ("in_place", "full_copy"),
    "overwrite_mode": ("incremental", "rebuild"),
}


def _validate_scalar(section: str, key: str, value: Any, rule: Any) -> None:
    """Raise ValueError when ``value`` violates ``rule``."""

    label = f"{section}.{key}" if section else key

    if rule is bool:
        # bool is a subclass of int, so an explicit type check is required to
        # reject 1/0 and truthy strings that would silently enable a stage.
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be true or false")
        return

    if rule == "str":
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} must be a non-empty string")
        return

    if rule == "str?":
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{label} must be a string or null")
        return

    kind = rule[0]
    if kind == "enum":
        allowed = rule[1]
        if value not in allowed:
            raise ValueError(f"{label} must be one of: {', '.join(map(str, allowed))}")
        return

    low, high = rule[1], rule[2]
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label} must be an integer")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{label} must be a number")
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}")


def _validate_section(section: str, values: Mapping[str, Any]) -> None:
    """Reject unknown keys and out-of-contract values inside one section."""

    rules = SECTION_SCHEMA[section]
    unknown = sorted(set(values) - set(rules))
    if unknown:
        raise ValueError(
            f"unknown {section} fields: {', '.join(unknown)}"
        )
    for key, value in values.items():
        _validate_scalar(section, key, value, rules[key])


# Validation patterns
RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
RESOURCE_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def utc_now() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Canonical JSON encoding for consistent hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    """Compute SHA-256 hash of canonical JSON."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# Nine-field standard annotation structure.
#
# This is the canonical output format for workflow jobs. All stages produce
# projections that conform to this shape, and the export stage validates them
# before writing JSON/TXT.
#
# Field semantics:
#   quality: rating bucket (e.g., ["safe"], ["questionable"])
#   count: character count bucket ("solo", "duo", "trio", "group", "")
#   character: comma-separated character names
#   series: comma-separated series/copyright names
#   artist: comma-separated artist names
#   appearance: appearance tags (e.g., ["blue_eyes", "long_hair"])
#   tags: general tags (e.g., ["forest", "outdoors"])
#   environment: environment/setting tags
#   nl: natural language description

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict  # type: ignore[assignment]


class NineFieldAnnotation(TypedDict):
    """Type-safe nine-field annotation payload.
    
    All nine keys are required at the export boundary.  Stages that build a
    partial projection use ``PartialNineFieldAnnotation`` and normalize it
    before it crosses this contract.
    """
    quality: list[str]
    count: str
    character: str
    series: str
    artist: str
    appearance: list[str]
    tags: list[str]
    environment: list[str]
    nl: str


class PartialNineFieldAnnotation(TypedDict, total=False):
    quality: list[str]
    count: str
    character: str
    series: str
    artist: str
    appearance: list[str]
    tags: list[str]
    environment: list[str]
    nl: str


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


def _path_ref(value: Any, field_name: str) -> WorkflowPathRef:
    """Coerce a decoded JSON object into a :class:`WorkflowPathRef`."""

    if isinstance(value, WorkflowPathRef):
        return value
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be an object with root_id and relative_path")
    unknown = sorted(set(value) - {"root_id", "relative_path"})
    if unknown:
        raise ValueError(f"{field_name} has unknown fields: {', '.join(unknown)}")
    root_id = value.get("root_id")
    if not isinstance(root_id, str) or not root_id:
        raise ValueError(f"{field_name}.root_id must be a non-empty string")
    relative_path = value.get("relative_path", "")
    if not isinstance(relative_path, str):
        raise TypeError(f"{field_name}.relative_path must be a string")
    return WorkflowPathRef(root_id=root_id, relative_path=relative_path)


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
    size_bytes: int = 0
    profile: str | None = None
    license_status: str = "unknown"
    source_digest: str | None = None

    def __post_init__(self) -> None:
        if not RESOURCE_ID_PATTERN.match(self.resource_id):
            raise ValueError(f"invalid resource_id: {self.resource_id}")
        if not RESOURCE_FINGERPRINT_PATTERN.match(self.resource_fingerprint):
            raise ValueError(f"invalid resource_fingerprint: {self.resource_fingerprint}")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")


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
        "model_id": "caption-e621-eva02-large-full-v1",
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
        "provider_id": None,
        "model": None,
        "use_image": True,
        "use_full_json": False,
        "prompt_preset": "general",
        "length": "medium",  # "short" (2-3), "medium" (4-5), "long" (6-8) sentences
    })
    
    # Count review configuration
    count_review: dict[str, Any] = field(default_factory=lambda: {
        # Review is an explicit opt-in stage.  Legacy jobs that omit this
        # section must remain runnable; production profiles enable it in their
        # immutable V2 snapshot.
        "enabled": False,
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
        "format": "both",  # "json", "flat_txt", "both"
    })
    
    compatibility_mode: bool = True
    schema_version: int = WORKFLOW_CONFIG_VERSION

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WorkflowJobConfigV1:
        """Build a config from a decoded JSON object.

        Nested path references arrive as plain objects, so they are converted
        explicitly; unknown top-level keys are rejected rather than ignored so a
        typo in a client payload cannot silently disable a stage.
        """

        if not isinstance(payload, Mapping):
            raise TypeError("job config must be an object")

        known = {field_info.name for field_info in fields(cls)}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(f"unknown job config fields: {', '.join(unknown)}")

        values = dict(payload)

        schema_version = values.get("schema_version", 1)
        if schema_version == 1:
            # Deterministic V1 -> V2 migration.  V1 remains readable for old
            # jobs, but new rows always store the explicit V2 shape.
            values["schema_version"] = WORKFLOW_CONFIG_VERSION
            caption = values.get("caption")
            if isinstance(caption, Mapping) and "resource_id" in caption and "model_id" not in caption:
                caption = dict(caption)
                caption["model_id"] = caption.pop("resource_id")
                values["caption"] = caption
        elif schema_version == WORKFLOW_CONFIG_V2_VERSION:
            # V2 is accepted by the same immutable reader while the dedicated
            # V2 public alias is rolled out.  The normalized in-memory contract
            # remains backward compatible with V1 callers.
            values["schema_version"] = WORKFLOW_CONFIG_V2_VERSION
        elif schema_version != WORKFLOW_CONFIG_VERSION:
            raise ValueError(f"unsupported job config schema_version: {schema_version!r}")

        if "source_root" not in values:
            raise ValueError("job config requires source_root")
        values["source_root"] = _path_ref(values["source_root"], "source_root")

        output_root = values.get("output_root")
        values["output_root"] = (
            None if output_root is None else _path_ref(output_root, "output_root")
        )

        for name, allowed in ENUM_FIELDS.items():
            if name in values and values[name] not in allowed:
                raise ValueError(
                    f"{name} must be one of: {', '.join(allowed)}"
                )

        if "recursive" in values:
            _validate_scalar("", "recursive", values["recursive"], bool)
        if "compatibility_mode" in values:
            _validate_scalar("", "compatibility_mode", values["compatibility_mode"], bool)

        # A partial section is merged onto its defaults so a caller can override
        # one key without restating the whole block, and the merged result is
        # validated as a whole.
        defaults = cls(
            profile=values.get("profile", "e621"),
            work_mode=values.get("work_mode", "in_place"),
            overwrite_mode=values.get("overwrite_mode", "incremental"),
            source_root=values["source_root"],
        )
        for section in SECTION_SCHEMA:
            supplied = values.get(section)
            if supplied is None:
                continue
            if not isinstance(supplied, Mapping):
                # ValueError keeps one exception contract for all config problems;
                # callers catch it alongside TypeError.
                raise ValueError(f"{section} must be an object")  # noqa: TRY004
            _validate_section(section, supplied)
            merged = dict(getattr(defaults, section))
            merged.update(supplied)
            if section == "export" and merged.get("format") == "flat_txt":
                merged["format"] = "txt"
            values[section] = merged

        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot of this configuration."""
        return asdict(self)

    def config_hash(self) -> str:
        """Compute configuration hash for change detection.

        ``asdict`` flattens nested path references so the digest stays stable
        and JSON-serializable; hashing ``__dict__`` would fail on dataclasses.
        """
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class WorkflowJobConfigV2(WorkflowJobConfigV1):
    """Strict current job contract with a normalized schema-2 snapshot."""

    schema_version: int = WORKFLOW_CONFIG_V2_VERSION

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> WorkflowJobConfigV2:
        # The V1 reader is retained solely as the deterministic migration path;
        # every newly-created V2 instance is stored with schema_version=2.
        legacy = WorkflowJobConfigV1.from_payload(payload)
        values = dict(legacy.__dict__)
        values["schema_version"] = WORKFLOW_CONFIG_V2_VERSION
        return cls(**values)


__all__ = [
    "WORKFLOW_CONFIG_V2_VERSION",
    "WORKFLOW_CONFIG_VERSION",
    "WORKFLOW_ISSUE_VERSION",
    "WORKFLOW_MANIFEST_VERSION",
    "ModuleId",
    "NineFieldAnnotation",
    "OverwriteMode",
    "PartialNineFieldAnnotation",
    "Profile",
    "WorkMode",
    "WorkflowIssueV1",
    "WorkflowJobConfigV1",
    "WorkflowJobConfigV2",
    "WorkflowPathRef",
    "WorkflowResourceManifestV1",
    "WorkflowSnapshotV1",
    "canonical_json",
    "sha256_json",
    "utc_now",
]
