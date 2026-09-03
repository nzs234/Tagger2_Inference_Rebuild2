"""Unit tests for the tag wiki embedder logic without external downloads."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from tagger2.tag_wiki.contracts import ERROR_WIKI_EMBED_MODEL_UNAVAILABLE
from tagger2.tag_wiki.embedder import (
    EmbeddingModelError,
    OnnxEmbedder,
    _mean_pooling,
    create_embedder,
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
