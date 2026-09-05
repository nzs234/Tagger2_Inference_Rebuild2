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

    Ranking rules (Reciprocal Rank Fusion):

    - Each leg ranks its candidates 0-indexed by descending leg score; a
      candidate a leg did not return contributes nothing for that leg.
    - The fused score is ``sum(1 / (rrf_k + rank))`` over the legs that
      returned the candidate. The fused ordering is ``(-score, chunk_id)``:
      equal fused scores tie-break by ascending chunk id, so the ranking is a
      deterministic function of the store contents (never of set/hash
      iteration order), and candidates whose in-leg scores tie are ordered by
      ascending chunk id inside each leg as well.
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
        vector_failed = False
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
                    id_array = np.asarray(chunk_ids)
                    if k_cand < n_items:
                        # Vectorized top-k via argpartition
                        top_indices = np.argpartition(scores, -k_cand)[-k_cand:]
                    else:
                        top_indices = np.arange(n_items)
                    # Sort the top slice descending; equal scores tie-break by
                    # chunk id (argpartition/argsort leave equal-score rows in
                    # arbitrary order, which made the ranking unstable).
                    top_indices = top_indices[
                        np.lexsort((id_array[top_indices], -scores[top_indices]))
                    ]

                    for rank, idx in enumerate(top_indices):
                        vector_rank_map[int(id_array[idx])] = rank
            except Exception as exc:
                vector_failed = True
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

        # Error condition: every retrieval leg is dead. Returning [] here
        # would mask the failure as a legitimate "no matches" answer, so
        # raise a retrieval error that names the dead legs instead. When at
        # least one leg succeeded, an empty result keeps its normal meaning:
        # the query genuinely matched nothing.
        if keyword_failed and (self.embedder is None or vector_failed):
            if self.embedder is None:
                raise WikiSearchError(
                    "Wiki 搜索不可用：未配置嵌入模型且全文检索失败",
                    code=ERROR_WIKI_SEARCH_UNAVAILABLE,
                )
            raise WikiSearchError(
                "Wiki 搜索不可用：向量检索与全文检索均失败，请检查 wiki 数据库或重建索引",
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

        # Sort by fused score descending; equal fused scores tie-break by
        # ascending chunk id so the candidate set's iteration order can never
        # leak into the ranking (stable across processes and runs).
        scored_cids.sort(key=lambda item: (-item[1], item[0]))
        top_candidates = scored_cids[:top_k]

        # 4. Resolve row contents (page_title, heading, text) for hits
        missing_cids = [cid for cid, _, _ in top_candidates if cid not in keyword_rows]
        loaded_rows: dict[int, dict[str, Any]] = {}
        loader_failed = False
        if missing_cids and self.chunk_loader is not None:
            try:
                fetched = self.chunk_loader(missing_cids)
                for row in fetched:
                    cid = row.get("id")
                    if cid is not None:
                        loaded_rows[cid] = row
            except Exception as exc:
                loader_failed = True
                logger.warning("chunk_loader failed to load chunks %r: %s", missing_cids, exc)

        hits: list[ChunkHit] = []
        for cid, score, matched_by in top_candidates:
            row = keyword_rows.get(cid) or loaded_rows.get(cid)
            if row is None:
                if self.chunk_loader is None:
                    # No loader configured at all: keep the legacy score-only
                    # hit (empty page_title/text) instead of hiding vector
                    # matches entirely.
                    row = {}
                else:
                    # A configured loader that raised (or resolved nothing for
                    # the id — e.g. a stale embedding-matrix entry) must not
                    # surface as a ghost hit with empty content; such hits
                    # would also leak into the ask context. Drop them.
                    logger.warning(
                        "Dropping chunk %d from search results: chunk_loader %s",
                        cid,
                        "failed" if loader_failed else "resolved nothing",
                    )
                    continue
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
