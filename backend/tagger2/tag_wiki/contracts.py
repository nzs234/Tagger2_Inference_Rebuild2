"""Strict public request models and shared shapes for the tag wiki.

The tag wiki is a local mirror of the e621 tag wiki (official ``wiki_pages``
db_export CSV) plus a retrieval stack: SQLite FTS5 keyword search and a
multilingual-e5 vector search fused with reciprocal-rank fusion, and an
optional LLM answer mode built on the app's configured online providers.

Like the tag manager, request models are strict pydantic objects while
responses are plain JSON dicts. The dict shapes are documented here as
TypedDicts so the backend service, the FastAPI layer and the frontend client
agree on one contract.

Endpoints (all under ``/api/v1/tag-wiki``, behind the shared authorize
dependency):

- ``GET  /status``                -> :class:`StatusDict`
- ``POST /build`` (202)           -> :class:`StatusDict` (build started/updated)
- ``POST /translate`` (202)       -> :class:`TranslateStatusDict`
- ``GET  /translate/progress``    -> :class:`TranslateStatusDict`
- ``GET  /lookup?tag=&profile=``  -> :class:`LookupDict`
- ``POST /search``                -> :class:`SearchDict`
- ``POST /ask``                   -> :class:`AskDict`
- ``GET  /page/{title}``          -> :class:`PageDict`

Errors use the app-wide shape ``{code, message, fields, request_id,
retryable}``. Notable codes: ``wiki_not_built`` (409, no wiki database yet),
``wiki_busy`` (409, a build or translate run is already active),
``wiki_embed_model_unavailable`` (409, the embedding model must be downloaded),
``wiki_search_unavailable`` (409, chunks exist but none are embedded),
``wiki_ask_unavailable`` (409, no online provider configured) and
``wiki_tag_db_unavailable`` (409, the classification snapshot is missing).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

TagWikiProfile = Literal["e621", "danbooru"]

# The embedding model is downloaded through the Hugging Face Hub into
# ``data/tag_wiki/models/<repo>``; see embedder.py for the onnx/torch fallback.
DEFAULT_EMBED_MODEL_REPO = "intfloat/multilingual-e5-small"

# Chunk text is capped at this many characters (a section longer than the cap
# is split into paragraph chunks) so e5's 512-token window is not truncated.
MAX_CHUNK_CHARS = 1200

# Chunks shorter than this are dropped at import time (together with a
# minimum of three word-like tokens; see importer.parse_dtext_sections).
# Tiny fragments (a lone punctuation mark, one link line, ASCII art) embed
# into degenerate vectors that otherwise flood the top of every semantic
# query. Pass 0 to disable the filter.
MIN_CHUNK_CHARS = 16

# Error codes shared by the service and the frontend.
ERROR_WIKI_NOT_BUILT = "wiki_not_built"
ERROR_WIKI_BUSY = "wiki_busy"
ERROR_WIKI_EMBED_MODEL_UNAVAILABLE = "wiki_embed_model_unavailable"
ERROR_WIKI_SEARCH_UNAVAILABLE = "wiki_search_unavailable"
ERROR_WIKI_ASK_UNAVAILABLE = "wiki_ask_unavailable"
ERROR_WIKI_TAG_DB_UNAVAILABLE = "wiki_tag_db_unavailable"
ERROR_WIKI_PAGE_NOT_FOUND = "wiki_page_not_found"
ERROR_WIKI_LOOKUP_FAILED = "wiki_lookup_failed"
ERROR_WIKI_SEARCH_FAILED = "wiki_search_failed"
ERROR_WIKI_ASK_FAILED = "wiki_ask_failed"
ERROR_WIKI_BUILD_FAILED = "wiki_build_failed"
ERROR_WIKI_TRANSLATE_FAILED = "wiki_translate_failed"


class BuildRequest(BaseModel):
    """Start (or resume) the local wiki build pipeline."""

    model_config = ConfigDict(extra="forbid")

    # Which mirror to build. The danbooru corpus ships pre-imported (see
    # scripts/fetch_danbooru_wiki.py), so its build only refreshes pruning and
    # the vector index; download_dump/reindex are e621-only and ignored.
    profile: TagWikiProfile = "e621"
    # Re-check e621 db_export for a newer wiki_pages dump. When False the
    # build reuses the newest dump file already cached under
    # ``data/tag_wiki/downloads/`` (if any).
    download_dump: bool = True
    # Re-import pages from the dump (incremental by updated_at) and rebuild
    # missing embeddings/FTS rows.
    reindex: bool = True
    # Re-embed every chunk even when its content hash is unchanged.
    force_reembed: bool = False


class TranslateRequest(BaseModel):
    """Batch-translate wiki pages into structured Chinese summaries."""

    model_config = ConfigDict(extra="forbid")

    # Which mirror's pages to summarize.
    profile: TagWikiProfile = "e621"
    # ``model_vocab``: pages for tags that appear in any local tagger model's
    # vocabulary. ``popular``: pages whose tag post_count >= min_post_count.
    # ``all``: every page that resolves to a known tag.
    scope: Literal["model_vocab", "popular", "all"] = "model_vocab"
    min_post_count: int = Field(default=1000, ge=0)
    # Upper bound on pages translated by one run; the run is resumable, so a
    # large scope is simply continued by starting the job again.
    max_pages: int = Field(default=2000, ge=1, le=50_000)
    # How many pages to summarize in parallel. Upstream providers (and their
    # rate limits) decide what is safe; 1 restores strictly sequential calls.
    concurrency: int = Field(default=4, ge=1, le=12)
    provider_id: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)


class SearchRequest(BaseModel):
    """Semantic + keyword search over wiki chunks."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    profile: TagWikiProfile = "e621"


class AskRequest(BaseModel):
    """Retrieval-augmented question over the local wiki."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=8, ge=1, le=50)
    provider_id: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=256)
    profile: TagWikiProfile = "e621"


# -- shared response shapes -------------------------------------------------


class TagRef(TypedDict, total=False):
    """One booru tag as exposed by the API (mirrors the tag manager shape)."""

    name: str
    category: str
    post_count: int | None
    alias_of: str | None
    translation: str | None


class WikiSummaryInfo(TypedDict, total=False):
    """Structured Chinese summary produced by the translate job."""

    meaning: str
    usage: str
    pairing: str
    notes: str
    tags: list[str]
    provider_id: str
    model: str
    updated_at: str


class PageSection(TypedDict):
    heading: str
    text: str


class WikiPageInfo(TypedDict, total=False):
    """One wiki page with its summary and parsed sections."""

    title: str
    wiki_id: int | None
    updated_at: str | None
    url: str | None
    summary: WikiSummaryInfo | None
    sections: list[PageSection]
    related_tags: list[str]


class LookupDict(TypedDict):
    """Response of ``GET /lookup``."""

    query: str
    resolved: bool
    tag: TagRef | None
    implications: list[TagRef]
    page: WikiPageInfo | None


class ChunkHit(TypedDict):
    """One retrieved wiki chunk."""

    page_title: str
    heading: str
    text: str
    score: float
    matched_by: list[str]
    summary: WikiSummaryInfo | None
    tag: TagRef | None


class SearchDict(TypedDict):
    """Response of ``POST /search``."""

    query: str
    items: list[ChunkHit]
    suggested_tags: list[TagRef]


class AskDict(TypedDict):
    """Response of ``POST /ask``."""

    query: str
    answer: str
    tags: list[str]
    provider_id: str
    model: str
    sources: list[str]


class BuildStatusDict(TypedDict, total=False):
    """Build pipeline status as reported by ``GET /status``."""

    state: Literal["idle", "running", "error"]
    phase: Literal["idle", "download", "parse", "model", "embed", "done"]
    message: str
    started_at: str | None
    updated_at: str | None
    error: str | None


class TranslateStatusDict(TypedDict, total=False):
    """Translate job progress as reported by ``GET /translate/progress``."""

    state: Literal["idle", "running", "error"]
    done: int
    failed: int
    total: int
    provider_id: str
    model: str
    message: str
    started_at: str | None
    updated_at: str | None
    error: str | None


class StatusDict(TypedDict, total=False):
    """Response of ``GET /status``.

    ``profiles`` carries the per-mirror ``database``/``index`` documents; the
    top-level ``database``/``index`` keys mirror the e621 profile for
    backward compatibility with older clients.
    """

    profiles: dict[str, Any]
    database: dict[str, Any]
    index: dict[str, Any]
    build: BuildStatusDict
    translate: TranslateStatusDict


__all__ = [
    "AskDict",
    "AskRequest",
    "BuildRequest",
    "BuildStatusDict",
    "ChunkHit",
    "DEFAULT_EMBED_MODEL_REPO",
    "ERROR_WIKI_ASK_FAILED",
    "ERROR_WIKI_ASK_UNAVAILABLE",
    "ERROR_WIKI_BUILD_FAILED",
    "ERROR_WIKI_BUSY",
    "ERROR_WIKI_EMBED_MODEL_UNAVAILABLE",
    "ERROR_WIKI_LOOKUP_FAILED",
    "ERROR_WIKI_NOT_BUILT",
    "ERROR_WIKI_PAGE_NOT_FOUND",
    "ERROR_WIKI_SEARCH_FAILED",
    "ERROR_WIKI_SEARCH_UNAVAILABLE",
    "ERROR_WIKI_TAG_DB_UNAVAILABLE",
    "ERROR_WIKI_TRANSLATE_FAILED",
    "LookupDict",
    "MAX_CHUNK_CHARS",
    "MIN_CHUNK_CHARS",
    "PageSection",
    "SearchDict",
    "SearchRequest",
    "StatusDict",
    "TagRef",
    "TagWikiProfile",
    "TranslateRequest",
    "TranslateStatusDict",
    "WikiPageInfo",
    "WikiSummaryInfo",
]
