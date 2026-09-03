"""Local e621 tag wiki mirror with FTS + vector retrieval and an LLM ask mode.

The module follows the same layout rules as the workflow and tag-manager
modules: an isolated SQLite database under ``data/tag_wiki/``, an import
pipeline fed by the official ``wiki_pages`` db_export CSV, a small FastAPI
router mounted by ``main.py`` and offline-first behaviour (everything except
the LLM summary/answer modes works without network access).
"""

from .api import create_tag_wiki_router
from .contracts import (
    AskRequest,
    BuildRequest,
    SearchRequest,
    TranslateRequest,
)
from .service import TagWikiError, TagWikiService
from .wiki_store import WikiStore, default_tag_wiki_database_path, normalize_title

__all__ = [
    "AskRequest",
    "BuildRequest",
    "SearchRequest",
    "TagWikiError",
    "TagWikiService",
    "TranslateRequest",
    "WikiStore",
    "create_tag_wiki_router",
    "default_tag_wiki_database_path",
    "normalize_title",
]
