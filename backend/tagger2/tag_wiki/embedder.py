"""Embedding model wrapper for the tag wiki.

Provides multilingual-e5-small text embeddings (dim 384) with an ONNX runtime
engine by default and a PyTorch fallback when ONNX weights are not present.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

from .contracts import ERROR_WIKI_EMBED_MODEL_UNAVAILABLE

logger = logging.getLogger("tagger2.tag_wiki.embedder")

E5_PASSAGE_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "
DEFAULT_EMBED_DIM = 384
MAX_TOKEN_LENGTH = 512

TOKENIZER_PATTERNS = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "tokenizer.model",
]


class EmbeddingModelError(RuntimeError):
    """Raised when the embedding model cannot be downloaded, loaded, or run."""

    def __init__(
        self,
        message: str,
        *,
        code: str = ERROR_WIKI_EMBED_MODEL_UNAVAILABLE,
        status_code: int = 409,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class Embedder(Protocol):
    """Protocol for text embedding models."""

    @property
    def dimension(self) -> int: ...

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        """Embed passages with prefix 'passage: ', returning float32 [N, D] L2-normalized."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a single query with prefix 'query: ', returning float32 [D] L2-normalized."""
        ...

    def close(self) -> None: ...


def model_dir_for(repo_id: str, models_root: Path) -> Path:
    """Resolve the local directory for a model repository under models_root."""
    safe_name = repo_id.replace("/", "__")
    return models_root / safe_name


def ensure_model_downloaded(
    repo_id: str,
    models_root: Path,
    *,
    timeout: float = 1800,
) -> Path:
    """Download the embedding model snapshot via huggingface_hub if not present.

    Attempts downloading tokenizer files and onnx/model.onnx first; if ONNX weights
    are missing, downloads PyTorch weights as fallback.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise EmbeddingModelError("huggingface_hub 未安装，无法下载嵌入模型") from exc

    target_dir = model_dir_for(repo_id, models_root)
    onnx_file = target_dir / "onnx" / "model.onnx"
    safetensors_file = target_dir / "model.safetensors"
    pytorch_bin_file = target_dir / "pytorch_model.bin"

    if onnx_file.is_file() or safetensors_file.is_file() or pytorch_bin_file.is_file():
        return target_dir

    allow_patterns = list(TOKENIZER_PATTERNS) + ["onnx/model.onnx"]
    try:
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(target_dir),
            allow_patterns=allow_patterns,
        )
    except Exception as exc:
        raise EmbeddingModelError(
            f"下载嵌入模型 {repo_id} 失败: {exc}",
            code=ERROR_WIKI_EMBED_MODEL_UNAVAILABLE,
        ) from exc

    if not onnx_file.is_file():
        fallback_patterns = list(TOKENIZER_PATTERNS) + ["model.safetensors", "pytorch_model.bin"]
        try:
            snapshot_download(
                repo_id=repo_id,
                local_dir=str(target_dir),
                allow_patterns=fallback_patterns,
            )
        except Exception as exc:
            raise EmbeddingModelError(
                f"下载嵌入模型 PyTorch 权重 {repo_id} 失败: {exc}",
                code=ERROR_WIKI_EMBED_MODEL_UNAVAILABLE,
            ) from exc

    return target_dir


def _mean_pooling(last_hidden_state: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    """Perform mask-aware mean pooling and L2 normalization over token embeddings.

    Args:
        last_hidden_state: float array of shape [B, T, H].
        attention_mask: int/float array of shape [B, T].

    Returns:
        float32 array of shape [B, H], L2-normalized along the feature dimension.
    """
    # Expand attention_mask to [B, T, 1] matching last_hidden_state
    mask_expanded = np.expand_dims(attention_mask, axis=-1).astype(np.float32)
    # Sum token embeddings weighted by attention mask
    sum_embeddings = np.sum(last_hidden_state * mask_expanded, axis=1)
    # Sum mask across token dimension, clamp to avoid divide by zero: [B, 1]
    sum_mask = np.clip(np.sum(mask_expanded, axis=1), a_min=1e-9, a_max=None)
    pooled = sum_embeddings / sum_mask

    # L2 normalize
    norms = np.linalg.norm(pooled, ord=2, axis=1, keepdims=True)
    norms = np.clip(norms, a_min=1e-12, a_max=None)
    normalized = pooled / norms
    return normalized.astype(np.float32)


class OnnxEmbedder:
    """ONNX Runtime implementation of the Embedder protocol."""

    def __init__(
        self,
        model_dir: Path,
        *,
        batch_size: int = 32,
        max_length: int = MAX_TOKEN_LENGTH,
        intra_op_threads: int = 0,
        providers: list[str] | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.batch_size = max(1, batch_size)
        self.max_length = max_length
        self._lock = threading.Lock()

        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise EmbeddingModelError("onnxruntime 未安装") from exc

        try:
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise EmbeddingModelError("transformers 未安装") from exc

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        except Exception as exc:
            raise EmbeddingModelError(f"加载 Tokenizer 失败 ({self.model_dir}): {exc}") from exc

        onnx_path = self.model_dir / "onnx" / "model.onnx"
        if not onnx_path.is_file():
            # Check if model.onnx is directly in model_dir
            if (self.model_dir / "model.onnx").is_file():
                onnx_path = self.model_dir / "model.onnx"
            else:
                raise EmbeddingModelError(f"未找到 ONNX 模型权重: {onnx_path}")

        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if intra_op_threads > 0:
            sess_options.intra_op_num_threads = intra_op_threads

        if providers is None:
            available = set(ort.get_available_providers())
            chosen_providers: list[str] = []
            if "CUDAExecutionProvider" in available:
                chosen_providers.append("CUDAExecutionProvider")
            chosen_providers.append("CPUExecutionProvider")
        else:
            chosen_providers = providers

        try:
            self._session: Any = ort.InferenceSession(
                str(onnx_path),
                sess_options=sess_options,
                providers=chosen_providers,
            )
        except Exception as exc:
            raise EmbeddingModelError(f"初始化 ONNX 推理会话失败: {exc}") from exc

        session_inputs = self._session.get_inputs()
        self._input_names = {inp.name for inp in session_inputs}

        # Determine embedding dimension from session output shape if possible
        self._dimension = DEFAULT_EMBED_DIM
        try:
            outputs = self._session.get_outputs()
            if outputs and len(outputs[0].shape) >= 3:
                last_dim = outputs[0].shape[-1]
                if isinstance(last_dim, int) and last_dim > 0:
                    self._dimension = last_dim
        except Exception:
            self._dimension = DEFAULT_EMBED_DIM

    @property
    def dimension(self) -> int:
        return self._dimension

    def _encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)

        if self._session is None:
            raise EmbeddingModelError("ONNX 推理会话已关闭")

        prefixed_texts = [f"{prefix}{t}" for t in texts]
        batches_out: list[np.ndarray] = []

        for i in range(0, len(prefixed_texts), self.batch_size):
            batch_texts = prefixed_texts[i : i + self.batch_size]
            encoded = self._tokenizer(
                batch_texts,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="np",
            )

            onnx_inputs: dict[str, np.ndarray] = {}
            input_ids = encoded["input_ids"].astype(np.int64)
            if "input_ids" in self._input_names:
                onnx_inputs["input_ids"] = input_ids
            if "attention_mask" in self._input_names and "attention_mask" in encoded:
                onnx_inputs["attention_mask"] = encoded["attention_mask"].astype(np.int64)
            if "token_type_ids" in self._input_names:
                # XLM-RoBERTa tokenizers never emit token_type_ids, but the
                # official ONNX export still lists it as a required input:
                # feed the all-zeros matrix the model expects.
                provided = encoded.get("token_type_ids")
                onnx_inputs["token_type_ids"] = (
                    provided.astype(np.int64)
                    if provided is not None
                    else np.zeros_like(input_ids)
                )

            with self._lock:
                outputs = self._session.run(None, onnx_inputs)

            # First output is typically last_hidden_state [B, T, H]
            first_out = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
            att_mask = encoded["attention_mask"]
            pooled = _mean_pooling(first_out, att_mask)
            batches_out.append(pooled)

        return np.vstack(batches_out).astype(np.float32)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, E5_PASSAGE_PREFIX)

    def embed_query(self, text: str) -> np.ndarray:
        res = self._encode([text], E5_QUERY_PREFIX)
        return res[0]

    def close(self) -> None:
        self._session = None


class TorchEmbedder:
    """PyTorch implementation of the Embedder protocol."""

    def __init__(
        self,
        model_dir: Path,
        *,
        batch_size: int = 32,
        max_length: int = MAX_TOKEN_LENGTH,
        device: str = "cpu",
    ) -> None:
        self.model_dir = Path(model_dir)
        self.batch_size = max(1, batch_size)
        self.max_length = max_length
        self.device = device
        self._lock = threading.Lock()
        # Typed loosely: transformers models have no usable stubs and close()
        # clears both attributes to release memory while the object is alive.
        self._tokenizer: Any = None
        self._model: Any = None

        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise EmbeddingModelError("PyTorch 或 transformers 未安装") from exc

        self._torch = torch

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        except Exception as exc:
            raise EmbeddingModelError(f"加载 Tokenizer 失败 ({self.model_dir}): {exc}") from exc

        try:
            self._model = AutoModel.from_pretrained(str(self.model_dir))
            self._model.to(self.device)
            self._model.eval()
        except Exception as exc:
            raise EmbeddingModelError(f"加载 PyTorch 模型失败 ({self.model_dir}): {exc}") from exc

        self._dimension = getattr(
            self._model.config,
            "hidden_size",
            DEFAULT_EMBED_DIM,
        )

    @property
    def dimension(self) -> int:
        return self._dimension

    def _encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)

        if self._model is None:
            raise EmbeddingModelError("PyTorch 模型已关闭")

        torch = self._torch
        prefixed_texts = [f"{prefix}{t}" for t in texts]
        batches_out: list[np.ndarray] = []

        for i in range(0, len(prefixed_texts), self.batch_size):
            batch_texts = prefixed_texts[i : i + self.batch_size]
            encoded = self._tokenizer(
                batch_texts,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            input_ids = encoded["input_ids"].to(self.device)
            attention_mask = encoded["attention_mask"].to(self.device)

            with self._lock, torch.inference_mode():
                outputs = self._model(input_ids=input_ids, attention_mask=attention_mask)
                last_hidden = outputs.last_hidden_state.detach().cpu().numpy()
                att_mask = attention_mask.detach().cpu().numpy()

            pooled = _mean_pooling(last_hidden, att_mask)
            batches_out.append(pooled)

        return np.vstack(batches_out).astype(np.float32)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, E5_PASSAGE_PREFIX)

    def embed_query(self, text: str) -> np.ndarray:
        res = self._encode([text], E5_QUERY_PREFIX)
        return res[0]

    def close(self) -> None:
        self._model = None
        self._tokenizer = None


def create_embedder(
    model_dir: Path,
    *,
    prefer: Literal["onnx", "torch", "auto"] = "auto",
) -> Embedder:
    """Factory function to instantiate an Embedder from a local model directory."""
    model_dir = Path(model_dir)
    onnx_file = model_dir / "onnx" / "model.onnx"
    direct_onnx_file = model_dir / "model.onnx"
    has_onnx = onnx_file.is_file() or direct_onnx_file.is_file()

    safetensors_file = model_dir / "model.safetensors"
    pytorch_bin_file = model_dir / "pytorch_model.bin"
    has_torch = safetensors_file.is_file() or pytorch_bin_file.is_file()

    if prefer == "onnx":
        if has_onnx:
            return OnnxEmbedder(model_dir)
        raise EmbeddingModelError(f"未找到 ONNX 权重文件: {model_dir}")

    if prefer == "torch":
        if has_torch or has_onnx:
            return TorchEmbedder(model_dir)
        raise EmbeddingModelError(f"未找到 PyTorch 模型权重: {model_dir}")

    # auto
    if has_onnx:
        return OnnxEmbedder(model_dir)
    if has_torch:
        return TorchEmbedder(model_dir)

    raise EmbeddingModelError(
        f"目录中未找到任何支持的嵌入模型权重 (ONNX 或 PyTorch): {model_dir}",
        code=ERROR_WIKI_EMBED_MODEL_UNAVAILABLE,
    )


__all__ = [
    "DEFAULT_EMBED_DIM",
    "E5_PASSAGE_PREFIX",
    "E5_QUERY_PREFIX",
    "Embedder",
    "EmbeddingModelError",
    "MAX_TOKEN_LENGTH",
    "OnnxEmbedder",
    "TorchEmbedder",
    "create_embedder",
    "ensure_model_downloaded",
    "model_dir_for",
]
