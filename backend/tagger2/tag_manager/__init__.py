"""Tag manager: BooruDatasetTagManager-style dataset tag editing.

The module follows the same layout rules as the workflow and image-generation
modules: an isolated SQLite database, path access only through the shared
``PathAllowlist`` roots, atomic sidecar writes and a small FastAPI router.
"""

from .contracts import (
    BatchOperationRequest,
    CreateDatasetRequest,
    ImageEditRequest,
    ImageFilter,
    NineFieldEdit,
    SaveTagsContent,
    StandardJsonContent,
    TagsJsonContent,
    TagEdit,
)
from .sidecar_io import (
    SidecarContent,
    SidecarKind,
    dedup_tags,
    load_sidecar,
    render_standard_json,
    render_tag_txt,
    render_tags_json,
)
from .storage import TagManagerStore, default_tag_manager_database_path
from .service import TagManagerError, TagManagerService
from .tag_db import TagDatabase, TagDatabaseError, TagInfo
from .thumbnails import ThumbnailError, ThumbnailService

__all__ = [
    "BatchOperationRequest",
    "CreateDatasetRequest",
    "ImageEditRequest",
    "ImageFilter",
    "NineFieldEdit",
    "SaveTagsContent",
    "SidecarContent",
    "SidecarKind",
    "StandardJsonContent",
    "TagDatabase",
    "TagDatabaseError",
    "TagEdit",
    "TagInfo",
    "TagManagerError",
    "TagManagerService",
    "TagManagerStore",
    "TagsJsonContent",
    "ThumbnailError",
    "ThumbnailService",
    "dedup_tags",
    "default_tag_manager_database_path",
    "load_sidecar",
    "render_standard_json",
    "render_tag_txt",
    "render_tags_json",
]
