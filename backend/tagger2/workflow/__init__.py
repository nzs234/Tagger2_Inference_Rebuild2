"""Dataset workflow module for transactional annotation processing.

This module provides a complete workflow system for dataset annotation:
- Transactional workspace with pause/resume/recovery
- Nine-field Anima JSON output (quality, count, character, series, artist, appearance, tags, environment, nl)
- Resource fingerprinting and version tracking
- Count review and token budget validation
- Atomic commit with backup/restore
- e621 and Danbooru profile support
"""

__version__ = "1.0.0"

from .contracts import (
    OverwriteMode,
    Profile,
    WorkMode,
    WorkflowIssueV1,
    WorkflowJobConfigV1,
    WorkflowJobConfigV2,
    WorkflowPathRef,
    WorkflowResourceManifestV1,
    WorkflowSnapshotV1,
)

__all__ = [
    "OverwriteMode",
    "Profile",
    "WorkMode",
    "WorkflowIssueV1",
    "WorkflowJobConfigV1",
    "WorkflowJobConfigV2",
    "WorkflowPathRef",
    "WorkflowResourceManifestV1",
    "WorkflowSnapshotV1",
    "__version__",
]
