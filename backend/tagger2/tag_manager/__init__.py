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
    NlTranslateRequest,
    SaveTagsContent,
    StandardJsonContent,
    TagsJsonContent,
    TagEdit,
    TagTranslateRequest,
    TranslationLookupRequest,
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
from .translations import TagTranslations, default_translation_dir, default_user_translation_dir

__all__ = [
    "BatchOperationRequest",
    "CreateDatasetRequest",
    "ImageEditRequest",
    "ImageFilter",
    "NineFieldEdit",
    "NlTranslateRequest",
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
    "TagTranslations",
    "TagTranslateRequest",
    "TagsJsonContent",
    "ThumbnailError",
    "ThumbnailService",
    "TranslationLookupRequest",
    "dedup_tags",
    "default_tag_manager_database_path",
    "default_translation_dir",
    "default_user_translation_dir",
    "load_sidecar",
    "render_standard_json",
    "render_tag_txt",
    "render_tags_json",
]
