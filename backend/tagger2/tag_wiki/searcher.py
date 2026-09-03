"""Hybrid retrieval module for the tag wiki.

Combines semantic vector search (multilingual-e5-small) and keyword search
(FTS5 / LIKE) using Reciprocal Rank Fusion (RRF).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import numpy as np

from .contracts import (
    ERROR_WIKI_SEARCH_UNAVAILABLE,
    ChunkHit,
)

if TYPE_CHECKING:
    from .embedder import Embedder

logger = logging.getLogger("tagger2.tag_wiki.searcher")


class WikiSearchError(RuntimeError):
    """Raised when search cannot be performed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = ERROR_WIKI_SEARCH_UNAVAILABLE,
        status_code: int = 409,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class WikiSearcher:
    """Hybrid searcher combining vector and keyword retrieval over WikiStore.

    Accepts an optional ``chunk_loader`` callable to retrieve chunk details
    (page_title, heading, text) by IDs for vector-only search hits when the store
    embedding matrix does not contain full chunk metadata.
    """

    def __init__(
        self,
        store: Any,
        embedder: Embedder | None,
        *,
        rrf_k: int = 60,
        chunk_loader: Callable[[Sequence[int]], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.chunk_loader = chunk_loader

    def search(self, query: str, *, top_k: int = 8) -> list[ChunkHit]:
        """Perform hybrid search fusing vector and keyword results with RRF.

        Returns a list of ChunkHit dicts matching the contract specification.
        """
        trimmed_query = query.strip()
        if not trimmed_query:
            return []

        limit_candidates = max(top_k * 4, 32)

        # 1. Vector retrieval leg
        vector_rank_map: dict[int, int] = {}  # chunk_id -> rank (0-indexed)
        if self.embedder is not None:
            try:
                chunk_ids, matrix = self.store.load_embedding_matrix()
                if len(chunk_ids) > 0 and matrix is not None and matrix.shape[0] > 0:
                    q_vec = self.embedder.embed_query(trimmed_query)
                    # Compute cosine similarities via matrix multiplication:
                    # matrix shape [N, D], q_vec shape [D] -> scores shape [N]
                    scores = np.dot(matrix, q_vec)
                    n_items = len(chunk_ids)
                    k_cand = min(limit_candidates, n_items)
                    if k_cand < n_items:
                        # Vectorized top-k via argpartition
                        top_indices = np.argpartition(scores, -k_cand)[-k_cand:]
                        # Sort the top slice descending
                        top_indices = top_indices[np.argsort(-scores[top_indices])]
                    else:
                        top_indices = np.argsort(-scores)

                    for rank, idx in enumerate(top_indices):
                        cid = chunk_ids[idx]
                        vector_rank_map[cid] = rank
            except Exception as exc:
                logger.warning("Vector retrieval failed for query %r: %s", trimmed_query, exc)

        # 2. Keyword retrieval leg
        keyword_rank_map: dict[int, int] = {}  # chunk_id -> rank (0-indexed)
        keyword_rows: dict[int, dict[str, Any]] = {}  # chunk_id -> row dict
        keyword_failed = False
        try:
            kw_results = self.store.search_text(trimmed_query, limit=limit_candidates)
            if kw_results:
                for rank, row in enumerate(kw_results):
                    cid = row.get("id")
                    if cid is not None:
                        keyword_rank_map[cid] = rank
                        keyword_rows[cid] = row
        except Exception as exc:
            keyword_failed = True
            logger.warning("Keyword search failed for query %r: %s", trimmed_query, exc)

        # Error condition check: if embedder is None AND keyword search failed / unavailable
        if self.embedder is None and keyword_failed:
            embedded_count = 0
            try:
                embedded_count = self.store.embedded_chunk_count()
            except Exception:
                pass
            if embedded_count == 0:
                raise WikiSearchError(
                    "Wiki 搜索不可用：未配置嵌入模型且全文检索失败",
                    code=ERROR_WIKI_SEARCH_UNAVAILABLE,
                )

        # 3. Fuse candidate rankings with Reciprocal Rank Fusion (RRF)
        all_cids = set(vector_rank_map.keys()) | set(keyword_rank_map.keys())
        if not all_cids:
            return []

        scored_cids: list[tuple[int, float, list[str]]] = []
        for cid in all_cids:
            score = 0.0
            matched_by: list[str] = []
            if cid in vector_rank_map:
                score += 1.0 / (self.rrf_k + vector_rank_map[cid])
                matched_by.append("vector")
            if cid in keyword_rank_map:
                score += 1.0 / (self.rrf_k + keyword_rank_map[cid])
                matched_by.append("keyword")
            scored_cids.append((cid, score, matched_by))

        # Sort by fused score descending
        scored_cids.sort(key=lambda item: item[1], reverse=True)
        top_candidates = scored_cids[:top_k]

        # 4. Resolve row contents (page_title, heading, text) for hits
        missing_cids = [cid for cid, _, _ in top_candidates if cid not in keyword_rows]
        loaded_rows: dict[int, dict[str, Any]] = {}
        if missing_cids and self.chunk_loader is not None:
            try:
                fetched = self.chunk_loader(missing_cids)
                for row in fetched:
                    cid = row.get("id")
                    if cid is not None:
                        loaded_rows[cid] = row
            except Exception as exc:
                logger.warning("chunk_loader failed to load chunks %r: %s", missing_cids, exc)

        hits: list[ChunkHit] = []
        for cid, score, matched_by in top_candidates:
            row = keyword_rows.get(cid) or loaded_rows.get(cid) or {}
            page_title = str(row.get("page_title", ""))
            heading = str(row.get("heading", ""))
            text = str(row.get("text", ""))

            hit: ChunkHit = {
                "page_title": page_title,
                "heading": heading,
                "text": text,
                "score": float(score),
                "matched_by": matched_by,
                "summary": None,
                "tag": None,
            }
            hits.append(hit)

        return hits

    def suggested_tags(
        self,
        query: str,
        *,
        top_k: int = 8,
        max_tags: int = 12,
    ) -> list[str]:
        """Retrieve suggested tag names based on the search query.

        Runs search, collects unique page_title values (which represent normalized
        tag names), preserving the order of their best match score, capped at max_tags.
        """
        hits = self.search(query, top_k=top_k)
        tags: list[str] = []
        seen: set[str] = set()
        for hit in hits:
            title = hit.get("page_title", "").strip()
            if title and title not in seen:
                seen.add(title)
                tags.append(title)
                if len(tags) >= max_tags:
                    break
        return tags


__all__ = [
    "WikiSearchError",
    "WikiSearcher",
]
