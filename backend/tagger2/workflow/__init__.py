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
    WorkflowJobConfigV1,
    WorkflowPathRef,
    WorkflowResourceManifestV1,
    WorkflowIssueV1,
    WorkflowSnapshotV1,
    Profile,
    WorkMode,
    OverwriteMode,
)

__all__ = [
    "__version__",
    "WorkflowJobConfigV1",
    "WorkflowPathRef",
    "WorkflowResourceManifestV1",
    "WorkflowIssueV1",
    "WorkflowSnapshotV1",
    "Profile",
    "WorkMode",
    "OverwriteMode",
]
