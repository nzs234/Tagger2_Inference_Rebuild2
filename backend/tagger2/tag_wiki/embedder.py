"""Embedding model wrapper for the tag wiki.

Provides multilingual-e5-small text embeddings (dim 384) with an ONNX runtime
engine by default and a PyTorch fallback when ONNX weights are not present.
"""

from __future__ import annotations

import json
import logging
import threading
import time
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
DEFAULT_REMOTE_ENDPOINT = "http://127.0.0.1:1234/v1"
DEFAULT_REMOTE_BATCH_SIZE = 32
DEFAULT_REMOTE_TIMEOUT = 120.0
REMOTE_RETRY_ATTEMPTS = 3
REMOTE_RETRY_BACKOFF_SECONDS = 2.0

TOKENIZER_PATTERNS = [
    "config.json",
    "1_Pooling/config.json",
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
    direct_onnx_file = target_dir / "model.onnx"
    onnx_external_data = target_dir / "onnx" / "model.onnx_data"
    direct_onnx_external_data = target_dir / "model.onnx_data"
    safetensors_file = target_dir / "model.safetensors"
    pytorch_bin_file = target_dir / "pytorch_model.bin"

    # Large ONNX exports may keep tensors in a sibling external-data file;
    # both files are required for ONNX Runtime to initialize the session.
    onnx_ready = (
        onnx_file.is_file()
        and (not onnx_external_data.exists() or onnx_external_data.is_file())
    ) or (
        direct_onnx_file.is_file()
        and (not direct_onnx_external_data.exists() or direct_onnx_external_data.is_file())
    )
    if onnx_ready or safetensors_file.is_file() or pytorch_bin_file.is_file():
        return target_dir

    allow_patterns = list(TOKENIZER_PATTERNS) + [
        "onnx/model.onnx",
        "onnx/model.onnx_data",
        "model.onnx",
        "model.onnx_data",
    ]
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

    if not onnx_file.is_file() and not direct_onnx_file.is_file():
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
        passage_prefix: str | None = None,
        query_prefix: str | None = None,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.batch_size = max(1, batch_size)
        self.max_length = max_length
        self._lock = threading.Lock()
        self._pooling_mode = "mean"
        pooling_config = self.model_dir / "1_Pooling" / "config.json"
        if pooling_config.is_file():
            try:
                with pooling_config.open("r", encoding="utf-8") as handle:
                    if json.load(handle).get("pooling_mode_lasttoken"):
                        self._pooling_mode = "last_token"
            except (OSError, ValueError, TypeError):
                logger.warning("无法读取 pooling 配置，使用 mean pooling: %s", pooling_config)
        elif "qwen3-embedding" in self.model_dir.name.lower():
            # Some community ONNX exports omit Sentence Transformers'
            # 1_Pooling/config.json; the official Qwen3-Embedding config is
            # nevertheless last-token pooling.
            self._pooling_mode = "last_token"
        guessed_passage, guessed_query = default_remote_prefixes(self.model_dir.name)
        self._passage_prefix = guessed_passage if passage_prefix is None else passage_prefix
        self._query_prefix = guessed_query if query_prefix is None else query_prefix

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
            if "position_ids" in self._input_names:
                # Qwen-style ONNX exports commonly require explicit absolute
                # positions even for a plain feature-extraction pass.
                batch, seq_len = input_ids.shape
                onnx_inputs["position_ids"] = np.broadcast_to(
                    np.arange(seq_len, dtype=np.int64), (batch, seq_len)
                ).copy()
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

            # First output is typically last_hidden_state [B, T, H].
            first_out = outputs[0] if isinstance(outputs, (list, tuple)) else outputs
            att_mask = encoded["attention_mask"]
            if self._pooling_mode == "last_token":
                # Qwen3-Embedding's official Sentence Transformers config
                # selects the final non-padding token, not mean pooling.
                last_indices = np.maximum(np.sum(att_mask, axis=1).astype(np.int64) - 1, 0)
                pooled = first_out[np.arange(first_out.shape[0]), last_indices]
                norms = np.clip(np.linalg.norm(pooled, ord=2, axis=1, keepdims=True), 1e-12, None)
                pooled = (pooled / norms).astype(np.float32)
            else:
                pooled = _mean_pooling(first_out, att_mask)
            batches_out.append(pooled)

        return np.vstack(batches_out).astype(np.float32)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, self._passage_prefix)

    def embed_query(self, text: str) -> np.ndarray:
        res = self._encode([text], self._query_prefix)
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
        # A CUDA context holds freed tensors until the cache is emptied.
        cuda = getattr(self._torch, "cuda", None)
        if cuda is not None and cuda.is_available():
            cuda.empty_cache()


def default_remote_prefixes(model_name: str) -> tuple[str, str]:
    """Derive (passage, query) prefixes from a served model name.

    Covers the prefixes the common OpenAI-compatible embedding servers
    expect; models that need none (bge-m3, Qwen3-Embedding, gte, ...) get
    empty prefixes. Explicit configuration always wins over this guess.
    """

    lowered = (model_name or "").lower()
    if "nomic" in lowered:
        return ("search_document: ", "search_query: ")
    if "e5" in lowered:
        return (E5_PASSAGE_PREFIX, E5_QUERY_PREFIX)
    return ("", "")


class OpenAIEmbedder:
    """Embedder backed by an OpenAI-compatible ``/v1/embeddings`` endpoint.

    Designed for LM Studio's local server (``http://127.0.0.1:1234/v1``) but
    works with any OpenAI-compatible embeddings API. The served model name
    and vector dimension are resolved against the live server at
    construction time so a misconfigured backend fails fast during a build
    instead of silently producing keyword-only search or mismatched vectors.
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_REMOTE_ENDPOINT,
        model: str = "",
        api_key: str = "",
        passage_prefix: str | None = None,
        query_prefix: str | None = None,
        batch_size: int = DEFAULT_REMOTE_BATCH_SIZE,
        timeout: float = DEFAULT_REMOTE_TIMEOUT,
        transport: Any | None = None,
    ) -> None:
        import httpx

        self._endpoint = (endpoint or DEFAULT_REMOTE_ENDPOINT).rstrip("/")
        self._model = (model or "").strip()
        self._api_key = api_key or ""
        self.batch_size = max(1, batch_size)
        self._timeout = timeout
        self._transport = transport
        self._httpx = httpx

        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
            transport=transport,
        )

        if not self._model:
            self._model = self._resolve_model()

        guessed_passage, guessed_query = default_remote_prefixes(self._model)
        self._passage_prefix = guessed_passage if passage_prefix is None else passage_prefix
        self._query_prefix = guessed_query if query_prefix is None else query_prefix

        # One warm-up call pins the dimension (and JIT-loads the model on
        # LM Studio) so the first real request cannot return a ragged batch.
        probe = self._request_embeddings(["dimension"])
        self._dimension = int(len(probe[0]))

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model(self) -> str:
        return self._model

    def _resolve_model(self) -> str:
        url = f"{self._endpoint}/models"
        try:
            resp = self._client.get(url)
        except (self._httpx.ConnectError, self._httpx.TimeoutException) as exc:
            raise EmbeddingModelError(
                f"无法连接嵌入服务 {self._endpoint}: {exc}。"
                "请确认 LM Studio 已启动、Server 已开启并加载了一个 Embedding 模型",
                retryable=True,
            ) from exc
        if resp.status_code >= 400:
            raise EmbeddingModelError(
                f"嵌入服务 {self._endpoint} 返回 {resp.status_code}: {_body_snippet(resp.text)}"
            )
        data = resp.json().get("data") or []
        ids = [str(item.get("id", "")).strip() for item in data if item.get("id")]
        if not ids:
            raise EmbeddingModelError(
                f"嵌入服务 {self._endpoint} 没有可用模型，"
                "请先在 LM Studio 下载并加载一个 Embedding 模型（如 bge-m3、text-embedding-nomic-embed-text-v1.5）"
            )
        return ids[0]

    def _request_embeddings(self, texts: Sequence[str]) -> list[list[float]]:
        url = f"{self._endpoint}/embeddings"
        payload = {"model": self._model, "input": list(texts)}
        last_error: Exception | None = None
        for attempt in range(REMOTE_RETRY_ATTEMPTS):
            try:
                resp = self._client.post(url, json=payload)
                if resp.status_code >= 500:
                    last_error = EmbeddingModelError(
                        f"嵌入服务 {self._endpoint} 返回 {resp.status_code}: {_body_snippet(resp.text)}",
                        retryable=True,
                    )
                elif resp.status_code >= 400:
                    raise EmbeddingModelError(
                        f"嵌入服务 {self._endpoint} 返回 {resp.status_code}: {_body_snippet(resp.text)}"
                    )
                else:
                    data = resp.json().get("data") or []
                    # Sort by the echoed index: the API does not promise
                    # response order, and a silently reordered batch would
                    # attach vectors to the wrong chunks.
                    indexed = sorted(
                        ((int(item.get("index", i)), item.get("embedding") or []) for i, item in enumerate(data)),
                        key=lambda pair: pair[0],
                    )
                    return [list(vec) for _idx, vec in indexed]
            except (self._httpx.ConnectError, self._httpx.TimeoutException) as exc:
                last_error = EmbeddingModelError(
                    f"无法连接嵌入服务 {self._endpoint}: {exc}。"
                    "请确认 LM Studio 已启动、Server 已开启并加载了一个 Embedding 模型",
                    retryable=True,
                )
            if attempt + 1 < REMOTE_RETRY_ATTEMPTS:
                time.sleep(REMOTE_RETRY_BACKOFF_SECONDS * (attempt + 1))
        assert last_error is not None
        raise last_error

    def _encode(self, texts: Sequence[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dimension), dtype=np.float32)

        prefixed_texts = [f"{prefix}{t}" for t in texts]
        vectors: list[list[float]] = []
        for i in range(0, len(prefixed_texts), self.batch_size):
            vectors.extend(self._request_embeddings(prefixed_texts[i : i + self.batch_size]))

        if len(vectors) != len(texts):
            raise EmbeddingModelError(
                f"嵌入服务返回 {len(vectors)} 条向量，与输入 {len(texts)} 条不一致"
            )
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.clip(np.linalg.norm(arr, ord=2, axis=1, keepdims=True), a_min=1e-12, a_max=None)
        return (arr / norms).astype(np.float32)

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        return self._encode(texts, self._passage_prefix)

    def embed_query(self, text: str) -> np.ndarray:
        res = self._encode([text], self._query_prefix)
        return res[0]

    def close(self) -> None:
        self._client.close()


def _body_snippet(body: str, limit: int = 200) -> str:
    text = (body or "").strip().replace("\n", " ")
    return text[:limit]


def create_embedder(
    model_dir: Path,
    *,
    prefer: Literal["onnx", "torch", "auto"] = "auto",
    passage_prefix: str | None = None,
    query_prefix: str | None = None,
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
            return OnnxEmbedder(
                model_dir,
                passage_prefix=passage_prefix,
                query_prefix=query_prefix,
            )
        raise EmbeddingModelError(f"未找到 ONNX 权重文件: {model_dir}")

    if prefer == "torch":
        if has_torch or has_onnx:
            return TorchEmbedder(model_dir)
        raise EmbeddingModelError(f"未找到 PyTorch 模型权重: {model_dir}")

    # auto
    if has_onnx:
        return OnnxEmbedder(
            model_dir,
            passage_prefix=passage_prefix,
            query_prefix=query_prefix,
        )
    if has_torch:
        return TorchEmbedder(model_dir)

    raise EmbeddingModelError(
        f"目录中未找到任何支持的嵌入模型权重 (ONNX 或 PyTorch): {model_dir}",
        code=ERROR_WIKI_EMBED_MODEL_UNAVAILABLE,
    )


__all__ = [
    "DEFAULT_EMBED_DIM",
    "DEFAULT_REMOTE_ENDPOINT",
    "E5_PASSAGE_PREFIX",
    "E5_QUERY_PREFIX",
    "Embedder",
    "EmbeddingModelError",
    "MAX_TOKEN_LENGTH",
    "OnnxEmbedder",
    "OpenAIEmbedder",
    "TorchEmbedder",
    "create_embedder",
    "default_remote_prefixes",
    "ensure_model_downloaded",
    "model_dir_for",
]
