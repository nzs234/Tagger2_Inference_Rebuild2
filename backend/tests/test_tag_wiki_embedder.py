"""Unit tests for the tag wiki embedder logic without external downloads."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import httpx
import numpy as np
import pytest

from tagger2.tag_wiki.contracts import ERROR_WIKI_EMBED_MODEL_UNAVAILABLE
from tagger2.tag_wiki.embedder import (
    EmbeddingModelError,
    OnnxEmbedder,
    OpenAIEmbedder,
    _mean_pooling,
    create_embedder,
    default_remote_prefixes,
    model_dir_for,
)


def test_model_dir_for():
    """Verify repo_id with slash is mapped to double underscore directory name."""
    root = Path("/tmp/models")
    res = model_dir_for("intfloat/multilingual-e5-small", root)
    assert res == root / "intfloat__multilingual-e5-small"


def test_mean_pooling_and_normalization():
    """Verify mask-aware mean pooling and L2 normalization calculation."""
    # Batch size 2, Sequence length 3, Dim 2
    # Item 0: 3 tokens, all active (mask = [1, 1, 1])
    # Item 1: 3 tokens, only first active (mask = [1, 0, 0])
    hidden = np.array(
        [
            [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]],
            [[10.0, 20.0], [100.0, 200.0], [1000.0, 2000.0]],
        ],
        dtype=np.float32,
    )
    mask = np.array(
        [
            [1, 1, 1],
            [1, 0, 0],
        ],
        dtype=np.int64,
    )

    pooled = _mean_pooling(hidden, mask)
    assert pooled.shape == (2, 2)
    assert pooled.dtype == np.float32

    # For item 0: mean = [(1+3+5)/3, (2+4+6)/3] = [3, 4]. Norm = 5. Normalized = [0.6, 0.8]
    assert np.allclose(pooled[0], [0.6, 0.8], atol=1e-5)

    # For item 1: mean = [10, 20]. Norm = sqrt(500) = 10*sqrt(5). Normalized = [1/sqrt(5), 2/sqrt(5)]
    expected_item1 = np.array([10.0, 20.0]) / np.sqrt(500.0)
    assert np.allclose(pooled[1], expected_item1, atol=1e-5)

    # Verify L2 norms are 1.0
    norms = np.linalg.norm(pooled, axis=1)
    assert np.allclose(norms, [1.0, 1.0], atol=1e-5)


def test_create_embedder_missing_weights(tmp_path: Path):
    """Verify create_embedder raises EmbeddingModelError on empty directory."""
    empty_dir = tmp_path / "empty_model"
    empty_dir.mkdir()
    with pytest.raises(EmbeddingModelError) as exc_info:
        create_embedder(empty_dir)
    assert exc_info.value.code == ERROR_WIKI_EMBED_MODEL_UNAVAILABLE


class _FakeTokenizer:
    """RoBERTa-style fast tokenizer: never emits token_type_ids."""

    def __call__(self, texts, *, padding=None, truncation=None, max_length=None, return_tensors=None):
        batch = len(texts)
        return {
            "input_ids": np.arange(batch * 3, dtype=np.int64).reshape(batch, 3),
            "attention_mask": np.ones((batch, 3), dtype=np.int64),
        }


class _FakeSession:
    last_instance: "_FakeSession | None" = None

    def __init__(self, input_names: list[str]) -> None:
        self._input_names = input_names
        self.feeds: dict[str, np.ndarray] | None = None

    def get_inputs(self):
        return [types.SimpleNamespace(name=name) for name in self._input_names]

    def get_outputs(self):
        return [types.SimpleNamespace(shape=("B", "T", 4))]

    def run(self, _output_names, feeds):
        self.feeds = feeds
        ids = feeds["input_ids"]
        return [np.ones((ids.shape[0], ids.shape[1], 4), dtype=np.float32)]


def test_onnx_embedder_feeds_required_token_type_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """XLM-R tokenizers emit no token_type_ids, yet the ONNX export requires them.

    Regression: the real multilingual-e5-small ONNX model failed with
    "Required inputs (['token_type_ids']) are missing" because the feed was
    gated on the tokenizer providing the key.
    """

    session = _FakeSession(["input_ids", "attention_mask", "token_type_ids"])
    _FakeSession.last_instance = session

    fake_ort = types.SimpleNamespace(
        SessionOptions=lambda: types.SimpleNamespace(graph_optimization_level=None, intra_op_num_threads=0),
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL=1),
        get_available_providers=lambda: ["CPUExecutionProvider"],
        InferenceSession=lambda path, sess_options=None, providers=None: session,
    )
    fake_transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda _path: _FakeTokenizer())
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model_dir = tmp_path / "model"
    (model_dir / "onnx").mkdir(parents=True)
    (model_dir / "onnx" / "model.onnx").write_bytes(b"stub")
    embedder = OnnxEmbedder(model_dir)
    assert embedder.dimension == 4
    vectors = embedder.embed_passages(["hugging pose", "an embrace"])
    assert vectors.shape == (2, 4)
    assert session.feeds is not None
    # The required input was synthesized as zeros with input_ids' shape.
    assert "token_type_ids" in session.feeds
    assert np.array_equal(session.feeds["token_type_ids"], np.zeros_like(session.feeds["input_ids"]))


# -- OpenAIEmbedder (OpenAI-compatible /v1/embeddings, e.g. LM Studio) -------


class _FakeServer:
    """Minimal OpenAI-compatible embeddings server over httpx.MockTransport.

    Vectors encode which marker the text contains so per-text routing can be
    asserted even though the fake replies with the ``index`` fields reversed.
    """

    def __init__(self, models: list[str], *, status: int = 200) -> None:
        self.httpx = httpx
        self.models = models
        self.status = status
        self.embedding_requests: list[list[str]] = []

    def handler(self, request: "httpx.Request") -> "httpx.Response":
        if request.url.path.endswith("/models"):
            return self.httpx.Response(
                200, json={"data": [{"id": name} for name in self.models]}
            )
        if request.url.path.endswith("/embeddings"):
            if self.status >= 400:
                return self.httpx.Response(self.status, text="boom")
            import json

            body = json.loads(request.content)
            self.embedding_requests.append([str(t) for t in body["input"]])
            vectors = [[1.0, 0.0] if "alpha" in t else [0.0, 2.0] for t in body["input"]]
            data = [
                {"index": i, "embedding": v}
                for i, v in reversed(list(enumerate(vectors)))
            ]
            return self.httpx.Response(200, json={"data": data})
        return self.httpx.Response(404)

    def transport(self) -> "httpx.MockTransport":
        return self.httpx.MockTransport(self.handler)


def test_openai_embedder_resolves_model_and_dimension():
    server = _FakeServer(["nomic-embed-text-v1.5"])
    embedder = OpenAIEmbedder(endpoint="http://lm.studio/v1/", transport=server.transport())
    assert embedder.model == "nomic-embed-text-v1.5"
    assert embedder.dimension == 2
    embedder.close()


def test_openai_embedder_preserves_order_and_normalizes():
    server = _FakeServer(["bge-m3"])
    embedder = OpenAIEmbedder(
        endpoint="http://lm.studio/v1",
        transport=server.transport(),
    )
    vectors = embedder.embed_passages(["alpha one", "beta two", "alpha three"])
    assert vectors.shape == (3, 2)
    assert vectors.dtype == np.float32
    # The fake answers in reversed index order; the embedder must restore it.
    assert np.allclose(vectors[0], [1.0, 0.0])
    assert np.allclose(vectors[1], [0.0, 1.0])
    assert np.allclose(vectors[2], [1.0, 0.0])
    # Rows are unit vectors: cosine search relies on the dot product.
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    query = embedder.embed_query("beta query")
    assert query.shape == (2,)
    assert np.allclose(query, [0.0, 1.0])
    embedder.close()


def test_openai_embedder_applies_prefixes_by_model_family():
    server = _FakeServer(["text-embedding-nomic-embed-text-v1.5"])
    embedder = OpenAIEmbedder(endpoint="http://x/v1", transport=server.transport())
    embedder.embed_passages(["a", "b"])
    embedder.embed_query("q")
    # Request 0 is the construction-time dimension probe.
    assert server.embedding_requests[1] == ["search_document: a", "search_document: b"]
    assert server.embedding_requests[2] == ["search_query: q"]

    server2 = _FakeServer(["intfloat/multilingual-e5-small"])
    embedder2 = OpenAIEmbedder(endpoint="http://x/v1", transport=server2.transport())
    embedder2.embed_passages(["a"])
    embedder2.embed_query("q")
    assert server2.embedding_requests[1] == ["passage: a"]
    assert server2.embedding_requests[2] == ["query: q"]
    embedder.close()
    embedder2.close()


def test_openai_embedder_explicit_prefix_override():
    server = _FakeServer(["bge-m3"])
    embedder = OpenAIEmbedder(
        endpoint="http://x/v1",
        transport=server.transport(),
        passage_prefix="",
        query_prefix="Q|",
    )
    embedder.embed_passages(["raw text"])
    embedder.embed_query("q")
    assert server.embedding_requests[1] == ["raw text"]
    assert server.embedding_requests[2] == ["Q|q"]
    embedder.close()


def test_openai_embedder_batches_requests():
    server = _FakeServer(["bge-m3"])
    embedder = OpenAIEmbedder(
        endpoint="http://x/v1",
        transport=server.transport(),
        batch_size=2,
    )
    vectors = embedder.embed_passages(["t1", "t2", "t3 alpha", "t4", "t5"])
    assert vectors.shape == (5, 2)
    # 5 texts at batch size 2 → 3 requests, plus the warm-up probe.
    assert len(server.embedding_requests) == 4
    embedder.close()


def test_openai_embedder_errors_without_server(monkeypatch: pytest.MonkeyPatch):
    import httpx

    def refusing_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    embedder_module = sys.modules["tagger2.tag_wiki.embedder"]
    monkeypatch.setattr(embedder_module, "REMOTE_RETRY_ATTEMPTS", 1)
    with pytest.raises(EmbeddingModelError) as exc_info:
        OpenAIEmbedder(
            endpoint="http://127.0.0.1:9/v1",
            transport=httpx.MockTransport(refusing_handler),
        )
    assert exc_info.value.retryable is True


def test_openai_embedder_http_error(monkeypatch: pytest.MonkeyPatch):
    server = _FakeServer(["bge-m3"], status=500)
    embedder_module = sys.modules["tagger2.tag_wiki.embedder"]
    monkeypatch.setattr(embedder_module, "REMOTE_RETRY_ATTEMPTS", 1)
    with pytest.raises(EmbeddingModelError):
        embedder = OpenAIEmbedder(endpoint="http://x/v1", transport=server.transport())
        embedder.embed_passages(["a"])


def test_default_remote_prefixes():
    assert default_remote_prefixes("nomic-embed-text-v1.5") == (
        "search_document: ",
        "search_query: ",
    )
    assert default_remote_prefixes("multilingual-e5-large") == ("passage: ", "query: ")
    assert default_remote_prefixes("bge-m3") == ("", "")
    assert default_remote_prefixes("Qwen3-Embedding-0.6B") == ("", "")
