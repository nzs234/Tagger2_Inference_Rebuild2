"""Unit tests for the tag wiki hybrid searcher."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from tagger2.tag_wiki.contracts import ERROR_WIKI_SEARCH_UNAVAILABLE
from tagger2.tag_wiki.searcher import WikiSearchError, WikiSearcher


class FakeEmbedder:
    """Deterministic fake embedder mapping queries to 4D unit vectors."""

    def __init__(self) -> None:
        self._dim = 4
        # Known embeddings for deterministic testing
        self.vectors: dict[str, np.ndarray] = {
            "canine": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            "feline": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
            "dragon": np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32),
        }

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_query(self, text: str) -> np.ndarray:
        # Default to a normalized vector along dimension 0 if unknown
        vec = self.vectors.get(text, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        return vec / np.linalg.norm(vec)

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        out = [self.embed_query(t) for t in texts]
        return np.vstack(out)

    def close(self) -> None:
        pass


class FakeStore:
    """Duck-typed store for WikiSearcher testing."""

    def __init__(
        self,
        *,
        chunks: list[dict[str, Any]] | None = None,
        embeddings: dict[int, np.ndarray] | None = None,
        raise_on_search: bool = False,
        raise_on_matrix: bool = False,
    ) -> None:
        self.chunks = chunks or []
        self.embeddings = embeddings or {}
        self.raise_on_search = raise_on_search
        self.raise_on_matrix = raise_on_matrix

    def search_text(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        if self.raise_on_search:
            raise RuntimeError("FTS search failure")
        results: list[dict[str, Any]] = []
        tokens = [t.lower() for t in query.split() if t.strip()]
        for c in self.chunks:
            full_text = f"{c.get('page_title', '')} {c.get('text', '')}".lower()
            if any(token in full_text for token in tokens):
                results.append(c)
                if len(results) >= limit:
                    break
        return results

    def load_embedding_matrix(self) -> tuple[list[int], np.ndarray]:
        if self.raise_on_matrix:
            raise RuntimeError("embedding matrix failure")
        if not self.embeddings:
            return [], np.empty((0, 4), dtype=np.float32)
        cids = sorted(self.embeddings.keys())
        matrix = np.vstack([self.embeddings[cid] for cid in cids]).astype(np.float32)
        return cids, matrix

    def embedded_chunk_count(self) -> int:
        return len(self.embeddings)


def test_pure_vector_ranking():
    """Verify vector-only retrieval and ranking order."""
    embedder = FakeEmbedder()
    # Chunk 1 is canine (1,0,0,0), Chunk 2 is feline (0,1,0,0)
    store = FakeStore(
        chunks=[],  # No keyword text match
        embeddings={
            1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            2: np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        },
    )

    def fake_chunk_loader(cids: list[int]) -> list[dict[str, Any]]:
        meta = {
            1: {"id": 1, "page_title": "wolf", "heading": "Overview", "text": "Canine predator"},
            2: {"id": 2, "page_title": "cat", "heading": "Overview", "text": "Feline pet"},
        }
        return [meta[c] for c in cids if c in meta]

    searcher = WikiSearcher(store, embedder, chunk_loader=fake_chunk_loader)
    hits = searcher.search("canine", top_k=2)

    assert len(hits) == 2
    assert hits[0]["page_title"] == "wolf"
    assert hits[0]["matched_by"] == ["vector"]
    assert hits[0]["score"] > 0
    assert hits[0]["text"] == "Canine predator"

    assert hits[1]["page_title"] == "cat"
    assert hits[1]["matched_by"] == ["vector"]


def test_pure_keyword_when_embedder_none():
    """Verify keyword retrieval when embedder is None."""
    chunks = [
        {"id": 10, "page_title": "fox", "heading": "Intro", "text": "A quick brown fox"},
        {"id": 20, "page_title": "dog", "heading": "Intro", "text": "A lazy dog"},
    ]
    store = FakeStore(chunks=chunks)
    searcher = WikiSearcher(store, embedder=None)

    hits = searcher.search("fox", top_k=5)
    assert len(hits) == 1
    assert hits[0]["page_title"] == "fox"
    assert hits[0]["matched_by"] == ["keyword"]
    assert hits[0]["text"] == "A quick brown fox"


def test_rrf_fusion_interleave_order():
    """Verify that a document matching both vector and keyword legs ranks highest."""
    embedder = FakeEmbedder()
    # Chunk 1: matches both keyword ("canine") and vector
    # Chunk 2: matches keyword only ("canine" in text) but vector orthogonal
    # Chunk 3: matches vector only
    chunks = [
        {"id": 1, "page_title": "wolf", "heading": "H1", "text": "wild canine"},
        {"id": 2, "page_title": "jackal", "heading": "H2", "text": "small canine"},
    ]
    embeddings = {
        1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),  # dot with canine = 1.0 (rank 0)
        2: np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),  # dot with canine = 0.0
        3: np.array([0.9, 0.0, 0.0, 0.0], dtype=np.float32),  # dot with canine = 0.9 (rank 1)
    }
    store = FakeStore(chunks=chunks, embeddings=embeddings)

    def loader(cids):
        return [{"id": 3, "page_title": "dingo", "heading": "H3", "text": "australian wild dog"}]

    searcher = WikiSearcher(store, embedder, rrf_k=60, chunk_loader=loader)
    hits = searcher.search("canine", top_k=5)

    assert len(hits) == 3
    # Wolf matched both vector and keyword -> highest score
    assert hits[0]["page_title"] == "wolf"
    assert "vector" in hits[0]["matched_by"] and "keyword" in hits[0]["matched_by"]
    # Verify RRF score: 1/(60+0) + 1/(60+0) = 2/60 = ~0.033333
    expected_score = 1.0 / 60 + 1.0 / 60
    assert pytest.approx(hits[0]["score"], 1e-5) == expected_score


def test_empty_store_returns_empty_list():
    """Verify empty store returns empty list gracefully."""
    store = FakeStore()
    searcher = WikiSearcher(store, embedder=None)
    hits = searcher.search("anything")
    assert hits == []

    # Whitespace-only query
    assert searcher.search("   ") == []


def test_vector_without_chunk_loader_defaults_text_to_empty():
    """Verify that vector hits without keyword row or chunk_loader have empty text."""
    embedder = FakeEmbedder()
    store = FakeStore(
        chunks=[],
        embeddings={1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)},
    )
    searcher = WikiSearcher(store, embedder, chunk_loader=None)
    hits = searcher.search("canine")
    assert len(hits) == 1
    assert hits[0]["text"] == ""
    assert hits[0]["page_title"] == ""


def test_search_unavailable_raised_when_embedder_none_and_search_fails():
    """Verify WikiSearchError is raised when embedder is None and store fails."""
    store = FakeStore(chunks=[], embeddings={}, raise_on_search=True)
    searcher = WikiSearcher(store, embedder=None)
    with pytest.raises(WikiSearchError) as exc_info:
        searcher.search("test")
    assert exc_info.value.code == ERROR_WIKI_SEARCH_UNAVAILABLE


def test_suggested_tags_dedup_and_cap():
    """Verify suggested_tags dedups page titles and respects max_tags."""
    chunks = [
        {"id": 1, "page_title": "wolf", "heading": "H1", "text": "wolf pack"},
        {"id": 2, "page_title": "wolf", "heading": "H2", "text": "wolf hunting"},
        {"id": 3, "page_title": "canine", "heading": "H1", "text": "canine species"},
        {"id": 4, "page_title": "fox", "heading": "H1", "text": "red fox"},
    ]
    store = FakeStore(chunks=chunks)
    searcher = WikiSearcher(store, embedder=None)

    tags = searcher.suggested_tags("wolf canine fox", max_tags=2)
    assert len(tags) == 2
    assert tags == ["wolf", "canine"]


# -- deterministic ranking ----------------------------------------------------


def test_fused_order_stable_on_score_ties():
    """Identical fused RRF scores tie-break by ascending chunk id.

    The keyword leg ranks cid 3 first while the vector leg ranks cid 5 first
    (mirrored ranks) — the swapped RRF contributions fuse to exactly the same
    score, and the candidate set's iteration order used to decide the winner.
    """

    embedder = FakeEmbedder()
    chunks = [
        {"id": 3, "page_title": "jackal", "heading": "H", "text": "canine jackal"},
        {"id": 5, "page_title": "wolf", "heading": "H", "text": "canine wolf"},
    ]
    embeddings = {
        5: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        3: np.array([0.5, 0.0, 0.0, 0.0], dtype=np.float32),
    }
    store = FakeStore(chunks=chunks, embeddings=embeddings)
    searcher = WikiSearcher(store, embedder, rrf_k=60)

    hits = searcher.search("canine", top_k=2)
    assert hits[0]["score"] == pytest.approx(hits[1]["score"])
    assert [hit["page_title"] for hit in hits] == ["jackal", "wolf"]
    # Repeating the query must reproduce the exact same order.
    assert [hit["page_title"] for hit in searcher.search("canine", top_k=2)] == [
        "jackal",
        "wolf",
    ]


def test_vector_leg_tie_breaks_by_chunk_id():
    """Vector candidates with equal similarity get ranks by ascending id."""

    embedder = FakeEmbedder()
    # Both chunks map to the same (zero) similarity for the "feline" query.
    store = FakeStore(
        chunks=[],
        embeddings={
            9: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            4: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        },
    )

    def loader(cids):
        meta = {
            4: {"id": 4, "page_title": "cat", "heading": "H", "text": "feline"},
            9: {"id": 9, "page_title": "panther", "heading": "H", "text": "feline"},
        }
        return [meta[cid] for cid in cids]

    searcher = WikiSearcher(store, embedder, chunk_loader=loader)
    hits = searcher.search("feline", top_k=2)
    # Equal similarity: the lower id wins the better rank deterministically
    # (argpartition/argsort used to leave this order to chance).
    assert [hit["page_title"] for hit in hits] == ["cat", "panther"]
    assert hits[0]["score"] > hits[1]["score"]
    assert [hit["page_title"] for hit in searcher.search("feline", top_k=2)] == [
        "cat",
        "panther",
    ]


# -- loader failure / ghost hits -----------------------------------------------


def test_loader_failure_drops_vector_only_hits():
    """A configured chunk_loader that raises must not surface ghost hits."""

    embedder = FakeEmbedder()
    store = FakeStore(
        chunks=[{"id": 10, "page_title": "fox", "heading": "H", "text": "quick fox"}],
        embeddings={
            10: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            11: np.array([0.9, 0.0, 0.0, 0.0], dtype=np.float32),  # not in chunks
        },
    )

    def broken_loader(_cids):
        raise RuntimeError("chunk table unavailable")

    searcher = WikiSearcher(store, embedder, chunk_loader=broken_loader)
    hits = searcher.search("fox", top_k=5)
    # The keyword-resolved hit renders; the vector-only chunk 11 is dropped
    # instead of emitted with an empty page_title/text.
    assert [hit["page_title"] for hit in hits] == ["fox"]
    assert all(hit["text"] for hit in hits)


def test_loader_missing_ids_are_dropped():
    """Vector candidates the loader cannot resolve are dropped, not blanked."""

    embedder = FakeEmbedder()
    store = FakeStore(
        chunks=[],
        embeddings={
            1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            2: np.array([0.9, 0.0, 0.0, 0.0], dtype=np.float32),
        },
    )

    def partial_loader(_cids):
        return [{"id": 1, "page_title": "wolf", "heading": "H", "text": "canine"}]

    searcher = WikiSearcher(store, embedder, chunk_loader=partial_loader)
    hits = searcher.search("canine", top_k=5)
    assert [hit["page_title"] for hit in hits] == ["wolf"]


def test_both_legs_failing_raises_unavailable():
    """Vector + keyword both failing raises instead of faking an empty result."""

    store = FakeStore(chunks=[], embeddings={}, raise_on_search=True, raise_on_matrix=True)
    searcher = WikiSearcher(store, FakeEmbedder())
    with pytest.raises(WikiSearchError) as exc_info:
        searcher.search("test")
    assert exc_info.value.code == ERROR_WIKI_SEARCH_UNAVAILABLE
    assert "向量" in exc_info.value.message and "全文" in exc_info.value.message


def test_vector_leg_failure_with_keyword_hits_still_returns_results():
    """A broken vector leg degrades to keyword-only results, not an error."""

    store = FakeStore(
        chunks=[{"id": 10, "page_title": "fox", "heading": "H", "text": "quick fox"}],
        embeddings={1: np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)},
        raise_on_matrix=True,
    )
    searcher = WikiSearcher(store, FakeEmbedder())
    hits = searcher.search("fox", top_k=5)
    assert [hit["page_title"] for hit in hits] == ["fox"]
    assert hits[0]["matched_by"] == ["keyword"]


def test_no_matches_with_working_legs_stays_empty():
    """Legs that succeed but match nothing keep the plain empty semantics."""

    store = FakeStore(chunks=[{"id": 1, "page_title": "wolf", "heading": "H", "text": "canine"}])
    searcher = WikiSearcher(store, embedder=None)
    assert searcher.search("unrelated query words", top_k=5) == []
