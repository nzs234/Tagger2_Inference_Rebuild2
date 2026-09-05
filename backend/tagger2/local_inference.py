"""Thread-safe local ONNX/PyTorch inference with explicit model lifecycle."""

from __future__ import annotations

import gc
import logging
import os
import pickle
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

import numpy as np
from PIL import Image

from .common import empty_cuda_cache, is_out_of_memory
from .model_registry import ModelBackend, ModelRecord, ModelRegistry
from .preprocessing import preprocess_batch, preprocess_image
from .schemas import TagItem
from .tag_text import canonical_tag_name


logger = logging.getLogger("tagger2.local_inference")


class InferenceError(RuntimeError):
    code = "inference_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        if code is not None:
            self.code = code
        if retryable is not None:
            self.retryable = bool(retryable)


class UnsafeModelError(InferenceError):
    code = "unsafe_weights"


class ModelLoadError(InferenceError):
    code = "model_load_failed"


class AdapterError(InferenceError):
    code = "adapter_error"


class ClassifierHook(Protocol):
    def classify(self, image: Image.Image, tags: Sequence[TagItem]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    kind: str = "none"
    path: Path | None = None
    scale: float = 1.0

    def __post_init__(self) -> None:
        kind = self.kind.casefold()
        if kind not in {"none", "lora", "lokr"}:
            raise ValueError(f"unsupported adapter type: {self.kind}")
        if not 0.0 <= float(self.scale) <= 4.0:
            raise ValueError("adapter scale must be between 0 and 4")
        object.__setattr__(self, "kind", kind)


@dataclass(slots=True)
class LocalPrediction:
    tags: list[TagItem]
    classifiers: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)
    model_tags: dict[str, list[TagItem]] = field(default_factory=dict)


@dataclass(slots=True)
class LoadedModel:
    record: ModelRecord
    runtime: Any
    input_name: str | None
    adapter: AdapterConfig
    classifier: ClassifierHook | None
    device: str
    input_layout: str = "nchw"
    input_dtype: Any = np.float32
    amp_enabled: bool = True
    lock: threading.RLock = field(default_factory=threading.RLock)
    cutoff_cache: "OrderedDict[tuple[tuple[str, float], ...], np.ndarray]" = field(
        default_factory=OrderedDict
    )
    last_used: float = field(default_factory=time.monotonic)


def select_device(requested: str | None = None) -> str:
    try:
        import torch
    except ImportError:
        return "cpu"
    if requested and requested != "auto":
        if requested.startswith("cuda") and not torch.cuda.is_available():
            raise ModelLoadError("CUDA was requested but is not available")
        return requested
    return "cuda" if torch.cuda.is_available() else "cpu"


def threshold_snapshot(
    record: ModelRecord,
    *,
    threshold: float | None = None,
    category_thresholds: Mapping[str, Any] | None = None,
    use_category_thresholds: bool = True,
) -> dict[str, float]:
    """Copy request thresholds without mutating shared model state."""

    category_values: dict[str, Any] = dict(category_thresholds or {})
    # UI/job payloads may provide either category overrides or a per-model
    # map.  Resolve the latter at the request boundary so one model's setting
    # can never leak into another model's shared context.
    nested_models = category_values.pop("models", None)
    direct = category_values.pop(record.model_id, None)
    if direct is None and isinstance(nested_models, Mapping):
        direct = nested_models.get(record.model_id)
    if isinstance(direct, Mapping):
        nested = dict(direct)
        direct_threshold = nested.pop("threshold", nested.pop("default", None))
        if direct_threshold is not None:
            threshold = float(direct_threshold)
        category_values.update(nested)
    elif direct is not None:
        threshold = float(direct)
    numeric_categories: dict[str, float] = {}
    for key, value in category_values.items():
        if isinstance(value, Mapping):
            continue
        try:
            numeric_categories[key] = float(value)
        except (TypeError, ValueError):
            continue
    return record.threshold_snapshot(
        threshold=threshold,
        category_thresholds=numeric_categories,
        use_category_thresholds=use_category_thresholds,
    )


class LocalInferenceEngine:
    def __init__(
        self,
        registry: ModelRegistry,
        *,
        device: str | None = None,
        allow_unsafe_pickle: bool = False,
        max_loaded_models: int = 2,
        memory_budget_mb: int | None = None,
        model_factory: Callable[[ModelRecord], Any] | None = None,
        classifier_factory: Callable[[ModelRecord], ClassifierHook | None] | None = None,
        preprocess_workers: int | None = None,
    ):
        self.registry = registry
        self.device = select_device(device)
        self.allow_unsafe_pickle = bool(allow_unsafe_pickle)
        self.max_loaded_models = max(1, int(max_loaded_models))
        self.memory_budget_mb = (
            max(1, int(memory_budget_mb)) if memory_budget_mb is not None else None
        )
        self.model_factory = model_factory
        self.classifier_factory = classifier_factory
        worker_count = preprocess_workers or min(8, max(2, os.cpu_count() or 2))
        self._preprocess_executor = ThreadPoolExecutor(
            max_workers=max(1, int(worker_count)),
            thread_name_prefix="tagger2-preprocess",
        )
        self._prefetch_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tagger2-prefetch",
        )
        self._loaded: "OrderedDict[str, LoadedModel]" = OrderedDict()
        self._lock = threading.RLock()
        self._load_lock = threading.RLock()
        # GPU execution is serial by default. CPU and ONNX sessions retain
        # their per-model lock but do not block unrelated CPU models.
        self._device_lock = threading.RLock()

    def load(
        self,
        model_id: str,
        *,
        adapter_type: str = "none",
        adapter_path: str | os.PathLike[str] | None = None,
        adapter_scale: float = 1.0,
        allow_unsafe_pickle: bool | None = None,
        classifier: ClassifierHook | None = None,
    ) -> LoadedModel:
        # Model construction and adapter merging are serialized. This prevents
        # duplicate concurrent loads from briefly doubling VRAM usage.
        with self._load_lock:
            return self._load_serial(
                model_id,
                adapter_type=adapter_type,
                adapter_path=adapter_path,
                adapter_scale=adapter_scale,
                allow_unsafe_pickle=allow_unsafe_pickle,
                classifier=classifier,
            )

    def _load_serial(
        self,
        model_id: str,
        *,
        adapter_type: str = "none",
        adapter_path: str | os.PathLike[str] | None = None,
        adapter_scale: float = 1.0,
        allow_unsafe_pickle: bool | None = None,
        classifier: ClassifierHook | None = None,
    ) -> LoadedModel:
        record = self.registry.get(model_id)
        adapter = AdapterConfig(
            kind=adapter_type or "none",
            path=Path(adapter_path).expanduser().resolve(strict=False) if adapter_path else None,
            scale=float(adapter_scale),
        )
        if adapter.path is not None:
            if not adapter.path.exists():
                raise AdapterError("adapter file does not exist")
            if self.registry.allowlist is not None:
                self.registry.allowlist.assert_allowed(adapter.path, expect="file" if adapter.path.is_file() else "dir")

        with self._lock:
            current = self._loaded.get(model_id)
            if current is not None and current.adapter == adapter:
                if classifier is not None:
                    current.classifier = classifier
                elif current.classifier is None and self.classifier_factory is not None:
                    current.classifier = self.classifier_factory(record)
                current.last_used = time.monotonic()
                self._loaded.move_to_end(model_id)
                return current
            if current is not None:
                self._unload_locked(model_id)

        unsafe_allowed = (
            self.allow_unsafe_pickle
            if allow_unsafe_pickle is None
            else bool(allow_unsafe_pickle)
        ) or record.trusted
        if record.unsafe_weights and not unsafe_allowed:
            raise UnsafeModelError(
                "pickle-based .pt/.pth/.bin weights are disabled; explicitly trust this model first"
            )
        if adapter.path is not None and not unsafe_allowed:
            adapter_files = [adapter.path] if adapter.path.is_file() else list(adapter.path.rglob("*"))
            if any(
                path.is_file() and path.suffix.casefold() in {".pt", ".pth", ".bin", ".ckpt"}
                for path in adapter_files
            ):
                raise UnsafeModelError(
                    "pickle-based adapter weights are disabled; explicitly trust the adapter"
                )

        # Evict before constructing the new runtime. Evicting afterward would
        # still allow the transient load peak to exceed the VRAM budget.
        estimated = record.estimated_memory_mb
        with self._lock:
            while self._loaded and (
                len(self._loaded) >= self.max_loaded_models
                or (
                    self.memory_budget_mb is not None
                    and self.loaded_memory_mb + estimated > self.memory_budget_mb
                )
            ):
                self._unload_locked(next(iter(self._loaded)))

        try:
            input_name: str | None
            if record.backend is ModelBackend.ONNX:
                runtime, input_name, input_layout, input_dtype = self._load_onnx(record)
            else:
                runtime, input_name = self._load_pytorch(record, unsafe_allowed=unsafe_allowed)
                input_layout, input_dtype = "nchw", np.float32
            if adapter.kind != "none":
                if record.backend is not ModelBackend.PYTORCH:
                    raise AdapterError("adapters are supported only for PyTorch models")
                runtime = self._apply_adapter(runtime, adapter)
            active_classifier = classifier
            if active_classifier is None and self.classifier_factory is not None:
                active_classifier = self.classifier_factory(record)
            loaded = LoadedModel(
                record=record,
                runtime=runtime,
                input_name=input_name,
                adapter=adapter,
                classifier=active_classifier,
                device=self.device,
                input_layout=input_layout,
                input_dtype=input_dtype,
            )
        except Exception as exc:
            self.registry.mark_loaded(model_id, False, str(exc))
            if isinstance(exc, (InferenceError, ImportError)):
                raise
            raise ModelLoadError(f"failed to load model {model_id}: {exc}") from exc

        with self._lock:
            self._loaded[model_id] = loaded
            self.registry.mark_loaded(model_id, True)
        return loaded

    load_model = load

    def _load_onnx(self, record: ModelRecord) -> tuple[Any, str, str, Any]:
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise ModelLoadError("onnxruntime is not installed") from exc
        available = set(ort.get_available_providers())
        requested: list[str] = []
        if self.device.startswith("cuda") and "CUDAExecutionProvider" in available:
            requested.append("CUDAExecutionProvider")
        requested.append("CPUExecutionProvider")
        options = ort.SessionOptions()
        options.enable_mem_pattern = True
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        session = ort.InferenceSession(
            str(record.weight_path),
            sess_options=options,
            providers=requested,
        )
        inputs = session.get_inputs()
        if not inputs:
            raise ModelLoadError("ONNX model has no inputs")
        input_info = inputs[0]
        shape = list(input_info.shape or [])
        layout = "nchw"
        if len(shape) == 4 and shape[-1] in {1, 3, 4} and shape[1] not in {1, 3, 4}:
            layout = "nhwc"
        dtype = {
            "tensor(float16)": np.float16,
            "tensor(double)": np.float64,
            "tensor(uint8)": np.uint8,
        }.get(str(input_info.type), np.float32)
        return session, input_info.name, layout, dtype

    def _load_pytorch(self, record: ModelRecord, *, unsafe_allowed: bool) -> tuple[Any, None]:
        try:
            import torch
        except ImportError as exc:
            raise ModelLoadError("PyTorch is not installed") from exc

        if self.model_factory is not None:
            model = self.model_factory(record)
            # A factory may return an already-loaded model. When it exposes an
            # empty/uninitialised module, callers can load the state themselves.
            if model is None:
                raise ModelLoadError("model factory returned None")
            return self._prepare_torch_model(model), None

        suffix = record.weight_path.suffix.casefold()
        if suffix == ".safetensors":
            try:
                from safetensors.torch import load_file
            except ImportError as exc:
                raise ModelLoadError("safetensors is not installed") from exc
            state = load_file(str(record.weight_path), device="cpu")
        else:
            if not unsafe_allowed:
                raise UnsafeModelError("pickle-based weights require explicit trust")
            # Restricted weights-only loading is attempted first. Full pickle
            # deserialisation occurs only after the caller explicitly opts in,
            # and only when torch itself rejected the checkpoint contents.
            try:
                state = torch.load(record.weight_path, map_location="cpu", weights_only=True)
            except Exception as exc:
                if not _is_pickle_restriction_error(exc):
                    raise
                state = torch.load(record.weight_path, map_location="cpu", weights_only=False)

        if isinstance(state, torch.nn.Module):
            return self._prepare_torch_model(state), None
        state_dict = _unwrap_state_dict(state)
        if not state_dict:
            raise ModelLoadError("checkpoint does not contain a tensor state dictionary")
        if not record.architecture:
            raise ModelLoadError("model architecture is missing from config.json")
        try:
            import timm
        except ImportError as exc:
            raise ModelLoadError("timm is required to instantiate this architecture") from exc
        kwargs: dict[str, Any] = {"pretrained": False}
        if record.num_classes is not None:
            kwargs["num_classes"] = record.num_classes
        try:
            model = timm.create_model(record.architecture, **kwargs)
        except Exception as exc:
            raise ModelLoadError(f"unable to create architecture {record.architecture!r}") from exc
        clean = _clean_state_dict(state_dict)
        model_keys = set(model.state_dict())
        matched = model_keys.intersection(clean)
        if model_keys and len(matched) / len(model_keys) < 0.45:
            raise ModelLoadError("checkpoint does not match configured architecture")
        incompatible = model.load_state_dict(clean, strict=False)
        if len(incompatible.missing_keys) >= max(1, int(len(model_keys) * 0.55)):
            raise ModelLoadError("too many model parameters are missing from checkpoint")
        return self._prepare_torch_model(model), None

    def _prepare_torch_model(self, model: Any) -> Any:
        try:
            model.to(self.device)
            model.eval()
        except AttributeError as exc:
            raise ModelLoadError("model factory did not return a PyTorch-like module") from exc
        return model

    def _apply_adapter(self, model: Any, adapter: AdapterConfig) -> Any:
        if adapter.path is None:
            raise AdapterError("adapter path is required")
        if adapter.kind == "lora":
            try:
                from peft import PeftModel
            except ImportError as exc:
                raise AdapterError("peft is required for LoRA adapters") from exc
            wrapped = PeftModel.from_pretrained(model, str(adapter.path))
            if adapter.scale != 1.0:
                for module in wrapped.modules():
                    scaling = getattr(module, "scaling", None)
                    if isinstance(scaling, dict):
                        for key, value in scaling.items():
                            scaling[key] = float(value) * adapter.scale
            return wrapped.merge_and_unload()
        if adapter.kind == "lokr":
            try:
                from lycoris import create_lycoris_from_weights
            except ImportError as exc:
                raise AdapterError("lycoris-lora is required for LoKr adapters") from exc
            path = adapter.path
            if path.is_dir():
                candidates = list(path.glob("*lokr*.safetensors"))
                if not candidates:
                    raise AdapterError("LoKr adapter directory has no safetensors weights")
                path = candidates[0]
            network, _ = create_lycoris_from_weights(
                multiplier=adapter.scale,
                file=str(path),
                module=model,
            )
            network.apply_to()
            network.merge_to(adapter.scale)
            return model
        return model

    def get_loaded(self, model_id: str) -> LoadedModel:
        with self._lock:
            loaded = self._loaded.get(model_id)
            if loaded is None:
                raise InferenceError("model is not loaded")
            loaded.last_used = time.monotonic()
            self._loaded.move_to_end(model_id)
            return loaded

    def ensure_loaded(self, model_id: str) -> LoadedModel:
        try:
            return self.get_loaded(model_id)
        except InferenceError:
            return self.load(model_id)

    @property
    def loaded_model_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._loaded)

    @property
    def loaded_memory_mb(self) -> float:
        with self._lock:
            return sum(item.record.estimated_memory_mb for item in self._loaded.values())

    def predict(
        self,
        model_id: str,
        image: str | os.PathLike[str] | bytes | Image.Image,
        *,
        threshold: float | None = None,
        category_thresholds: Mapping[str, float] | None = None,
        use_category_thresholds: bool = True,
    ) -> list[TagItem]:
        return self.predict_result(
            model_id,
            image,
            threshold=threshold,
            category_thresholds=category_thresholds,
            use_category_thresholds=use_category_thresholds,
        ).tags

    def predict_result(
        self,
        model_id: str,
        image: str | os.PathLike[str] | bytes | Image.Image,
        *,
        threshold: float | None = None,
        category_thresholds: Mapping[str, float] | None = None,
        use_category_thresholds: bool = True,
        run_classifier: bool = True,
    ) -> LocalPrediction:
        loaded = self.ensure_loaded(model_id)
        thresholds = threshold_snapshot(
            loaded.record,
            threshold=threshold,
            category_thresholds=category_thresholds,
            use_category_thresholds=use_category_thresholds,
        )
        cutoff_values = _cached_tag_cutoffs(loaded, thresholds)
        start = time.perf_counter()
        shared_image = image if isinstance(image, Image.Image) else _load_pil(image)
        tensor = preprocess_image(shared_image, loaded.record.preprocess)
        preprocess_ms = (time.perf_counter() - start) * 1000.0
        run_start = time.perf_counter()
        logits = self._run(loaded, tensor[None, ...])
        probability = _probabilities(
            logits, loaded.record.metadata, backend=loaded.record.backend
        )
        tags = _select_tags(loaded.record, probability[0], thresholds, cutoff_values)
        inference_ms = (time.perf_counter() - run_start) * 1000.0
        classifier_values: dict[str, Any] = {}
        if run_classifier and loaded.classifier is not None:
            classifier_values = _run_classifier(loaded.classifier, shared_image, tags)
        return LocalPrediction(
            tags=tags,
            classifiers=classifier_values,
            timing={
                "preprocess_ms": preprocess_ms,
                "inference_ms": inference_ms,
                "total_ms": (time.perf_counter() - start) * 1000.0,
            },
        )

    def predict_raw(
        self,
        model_id: str,
        image: str | os.PathLike[str] | bytes | Image.Image,
    ) -> np.ndarray:
        """Return sigmoid probabilities without threshold filtering."""

        loaded = self.ensure_loaded(model_id)
        shared_image = image if isinstance(image, Image.Image) else _load_pil(image)
        tensor = preprocess_image(shared_image, loaded.record.preprocess)
        return _probabilities(
            self._run(loaded, tensor[None, ...]),
            loaded.record.metadata,
            backend=loaded.record.backend,
        )[0]

    def predict_batch(
        self,
        model_id: str,
        images: Sequence[str | os.PathLike[str] | bytes | Image.Image],
        *,
        threshold: float | None = None,
        category_thresholds: Mapping[str, float] | None = None,
        use_category_thresholds: bool = True,
        batch_size: int | None = None,
    ) -> list[list[TagItem]]:
        if not images:
            return []
        loaded = self.ensure_loaded(model_id)
        thresholds = threshold_snapshot(
            loaded.record,
            threshold=threshold,
            category_thresholds=category_thresholds,
            use_category_thresholds=use_category_thresholds,
        )
        cutoff_values = _cached_tag_cutoffs(loaded, thresholds)
        default_batch = 16 if loaded.device.startswith("cuda") else 32
        requested = max(1, min(int(batch_size or default_batch), len(images)))
        output: list[list[TagItem]] = []
        offset = 0
        current_batch = requested
        prefetched: Any = None
        while offset < len(images):
            chunk = images[offset : offset + current_batch]
            next_prefetched: Any = None
            try:
                if prefetched is None:
                    prefetched = self._submit_preprocess(chunk, loaded.record)
                tensors = prefetched.result()
                next_offset = offset + len(chunk)
                next_chunk = images[next_offset : next_offset + current_batch]
                next_prefetched = (
                    self._submit_preprocess(next_chunk, loaded.record)
                    if next_chunk
                    else None
                )
                logits = self._run(loaded, tensors)
                probabilities = _probabilities(
                    logits, loaded.record.metadata, backend=loaded.record.backend
                )
                output.extend(
                    _select_tags(loaded.record, row, thresholds, cutoff_values)
                    for row in probabilities
                )
                offset += len(chunk)
                prefetched = next_prefetched
            except Exception as exc:
                if not is_out_of_memory(exc) or current_batch <= 1:
                    raise
                if prefetched is not None:
                    prefetched.cancel()
                if next_prefetched is not None:
                    next_prefetched.cancel()
                prefetched = None
                current_batch = max(1, current_batch // 2)
                empty_cuda_cache()
        return output

    def predict_raw_batch(
        self,
        model_id: str,
        images: Sequence[str | os.PathLike[str] | bytes | Image.Image],
        *,
        batch_size: int | None = None,
    ) -> np.ndarray:
        loaded = self.ensure_loaded(model_id)
        if not images:
            return np.empty((0, len(loaded.record.tags)), dtype=np.float32)
        shared = [image if isinstance(image, Image.Image) else _load_pil(image) for image in images]
        default_batch = 16 if loaded.device.startswith("cuda") else 32
        current_batch = max(1, min(int(batch_size or default_batch), len(shared)))
        offset = 0
        results: list[np.ndarray] = []
        prefetched: Any = None
        while offset < len(shared):
            chunk = shared[offset : offset + current_batch]
            next_prefetched: Any = None
            try:
                if prefetched is None:
                    prefetched = self._submit_preprocess(chunk, loaded.record)
                tensors = prefetched.result()
                next_offset = offset + len(chunk)
                next_chunk = shared[next_offset : next_offset + current_batch]
                next_prefetched = (
                    self._submit_preprocess(next_chunk, loaded.record)
                    if next_chunk
                    else None
                )
                results.append(
                    _probabilities(
                        self._run(loaded, tensors),
                        loaded.record.metadata,
                        backend=loaded.record.backend,
                    )
                )
                offset += len(chunk)
                prefetched = next_prefetched
            except Exception as exc:
                if not is_out_of_memory(exc) or current_batch <= 1:
                    raise
                if prefetched is not None:
                    prefetched.cancel()
                if next_prefetched is not None:
                    next_prefetched.cancel()
                prefetched = None
                current_batch = max(1, current_batch // 2)
                empty_cuda_cache()
        return np.concatenate(results, axis=0)

    def _submit_preprocess(self, images: Sequence[Any], record: ModelRecord):
        return self._prefetch_executor.submit(
            preprocess_batch,
            images,
            record.preprocess,
            executor=self._preprocess_executor,
        )

    def predict_multi(
        self,
        model_ids: Sequence[str],
        image: str | os.PathLike[str] | bytes | Image.Image,
        **kwargs: Any,
    ) -> list[TagItem]:
        return self.predict_multi_result(model_ids, image, **kwargs).tags

    def predict_multi_result(
        self,
        model_ids: Sequence[str],
        image: str | os.PathLike[str] | bytes | Image.Image,
        *,
        include_model_tags: bool = False,
        **kwargs: Any,
    ) -> LocalPrediction:
        shared_image = image if isinstance(image, Image.Image) else _load_pil(image)
        predictions: dict[str, list[TagItem]] = {}
        classifiers: dict[str, Any] = {}
        timing: dict[str, float] = {}
        classifier_groups: dict[int, tuple[Any, list[str]]] = {}
        predict_kwargs = dict(kwargs)
        predict_kwargs.pop("run_classifier", None)
        for model_id in model_ids:
            result = self.predict_result(
                model_id, shared_image, run_classifier=False, **predict_kwargs
            )
            predictions[model_id] = result.tags
            hook = self.ensure_loaded(model_id).classifier
            if hook is not None:
                group = classifier_groups.setdefault(id(hook), (hook, []))
                group[1].append(model_id)
            for key, timing_value in result.timing.items():
                timing[key] = timing.get(key, 0.0) + float(timing_value)
        merged_tags = merge_predictions(predictions)
        for hook, attached_models in classifier_groups.values():
            classifier_value = _run_classifier(hook, shared_image, merged_tags)
            for model_id in attached_models:
                classifiers[model_id] = classifier_value
        return LocalPrediction(
            tags=merged_tags,
            classifiers=classifiers,
            timing=timing,
            model_tags=predictions if include_model_tags else {},
        )

    def predict_batch_results(
        self,
        model_id: str,
        images: Sequence[str | os.PathLike[str] | bytes | Image.Image],
        *,
        run_classifier: bool = True,
        **kwargs: Any,
    ) -> list[LocalPrediction]:
        shared = [image if isinstance(image, Image.Image) else _load_pil(image) for image in images]
        tag_batches = self.predict_batch(model_id, shared, **kwargs)
        loaded = self.ensure_loaded(model_id)
        # Classifier batches fall back to 4, matching main.py's classifier path
        # (``config.get("batch_size", 4)``); prediction batches keep their own
        # larger default.
        classifier_batch = max(1, int(kwargs.get("batch_size") or 4))
        classifier_batches = (
            _run_classifier_batch(
                loaded.classifier,
                shared,
                tag_batches,
                batch_size=classifier_batch,
            )
            if run_classifier and loaded.classifier is not None
            else [{} for _ in shared]
        )
        results: list[LocalPrediction] = []
        for tags, classifiers in zip(tag_batches, classifier_batches, strict=True):
            results.append(LocalPrediction(tags=tags, classifiers=classifiers))
        return results

    def predict_multi_batch(
        self,
        model_ids: Sequence[str],
        images: Sequence[str | os.PathLike[str] | bytes | Image.Image],
        **kwargs: Any,
    ) -> list[list[TagItem]]:
        return [result.tags for result in self.predict_multi_batch_results(model_ids, images, **kwargs)]

    def predict_multi_batch_results(
        self,
        model_ids: Sequence[str],
        images: Sequence[str | os.PathLike[str] | bytes | Image.Image],
        *,
        include_model_tags: bool = False,
        **kwargs: Any,
    ) -> list[LocalPrediction]:
        shared = [image if isinstance(image, Image.Image) else _load_pil(image) for image in images]
        predict_kwargs = dict(kwargs)
        predict_kwargs.pop("run_classifier", None)
        per_model: dict[str, list[LocalPrediction]] = {}
        classifier_groups: dict[int, tuple[Any, list[str]]] = {}
        for model_id in model_ids:
            per_model[model_id] = self.predict_batch_results(
                model_id,
                shared,
                run_classifier=False,
                **predict_kwargs,
            )
            hook = self.ensure_loaded(model_id).classifier
            if hook is not None:
                group = classifier_groups.setdefault(id(hook), (hook, []))
                group[1].append(model_id)

        merged_batches: list[list[TagItem]] = []
        for index in range(len(shared)):
            tag_groups = {
                model_id: results[index].tags for model_id, results in per_model.items()
            }
            merged_batches.append(merge_predictions(tag_groups))

        classifier_batches: list[dict[str, Any]] = [{} for _ in shared]
        for hook, attached_models in classifier_groups.values():
            # Same classifier fallback as predict_batch_results: 4, to match
            # main.py's ``config.get("batch_size", 4)`` classifier default.
            values = _run_classifier_batch(
                hook,
                shared,
                merged_batches,
                batch_size=max(1, int(predict_kwargs.get("batch_size") or 4)),
            )
            for index, value in enumerate(values):
                for model_id in attached_models:
                    classifier_batches[index][model_id] = value
        return [
            LocalPrediction(
                tags=tags,
                classifiers=classifiers,
                model_tags=(
                    {model_id: results[index].tags for model_id, results in per_model.items()}
                    if include_model_tags
                    else {}
                ),
            )
            for index, (tags, classifiers) in enumerate(
                zip(merged_batches, classifier_batches, strict=True)
            )
        ]

    def _run(self, loaded: LoadedModel, batch: Any) -> np.ndarray:
        execution_lock = self._device_lock if loaded.device.startswith("cuda") else nullcontext()
        with loaded.lock, execution_lock:
            loaded.last_used = time.monotonic()
            if loaded.record.backend is ModelBackend.ONNX:
                array = _input_numpy(batch)
                if loaded.input_layout == "nhwc":
                    array = np.transpose(array, (0, 2, 3, 1))
                array = np.ascontiguousarray(array, dtype=loaded.input_dtype)
                output = loaded.runtime.run(None, {loaded.input_name: array})
                return _as_numpy(output[0])
            try:
                import torch
            except ImportError as exc:  # pragma: no cover
                raise InferenceError("PyTorch is not installed") from exc
            tensor = batch.to(loaded.device, non_blocking=loaded.device.startswith("cuda"))
            with torch.inference_mode():
                if loaded.device.startswith("cuda") and loaded.amp_enabled:
                    try:
                        with torch.autocast(device_type="cuda", dtype=torch.float16):
                            output = loaded.runtime(tensor)
                        values = _as_numpy(_extract_output(output))
                        if np.isfinite(values).all():
                            return values
                    except (RuntimeError, FloatingPointError):
                        pass
                    # Some large vision backbones overflow under FP16 AMP.
                    # Disable it for this loaded context after the first bad
                    # batch and immediately retry in FP32.
                    loaded.amp_enabled = False
                output = loaded.runtime(tensor.float())
            return _as_numpy(_extract_output(output))

    def unload(self, model_id: str) -> bool:
        with self._lock:
            return self._unload_locked(model_id)

    unload_model = unload

    def _unload_locked(self, model_id: str) -> bool:
        loaded = self._loaded.pop(model_id, None)
        if loaded is None:
            self.registry.mark_loaded(model_id, False)
            return False
        with loaded.lock:
            runtime = loaded.runtime
            try:
                if loaded.record.backend is ModelBackend.PYTORCH and hasattr(runtime, "to"):
                    runtime.to("cpu")
            except Exception:
                pass
            loaded.runtime = None
            del runtime
        self.registry.mark_loaded(model_id, False)
        gc.collect()
        empty_cuda_cache()
        return True

    def unload_all(self) -> None:
        with self._lock:
            for model_id in list(self._loaded):
                self._unload_locked(model_id)

    def close(self) -> None:
        self.unload_all()
        self._prefetch_executor.shutdown(wait=True, cancel_futures=True)
        self._preprocess_executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> "LocalInferenceEngine":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def _unwrap_state_dict(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    for key in ("state_dict", "model_state_dict", "model", "module"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            return nested
    return value


def _clean_state_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, tensor in value.items():
        name = str(key)
        for prefix in ("_orig_mod.", "module."):
            if name.startswith(prefix):
                name = name[len(prefix) :]
        clean[name] = tensor
    return clean


def _extract_output(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key in ("logits", "output", "predictions"):
            if key in value:
                return value[key]
        if value:
            return next(iter(value.values()))
    if isinstance(value, (list, tuple)):
        if not value:
            raise InferenceError("model returned no outputs")
        return value[0]
    if hasattr(value, "logits"):
        return value.logits
    return value


def _as_numpy(value: Any) -> np.ndarray:
    if isinstance(value, np.ndarray):
        array = value
    elif hasattr(value, "detach"):
        array = value.detach().float().cpu().numpy()
    else:
        array = np.asarray(value)
    if array.ndim == 1:
        array = array[None, :]
    if array.ndim > 2:
        array = array.reshape(array.shape[0], -1)
    if array.ndim != 2:
        raise InferenceError(f"expected a 2D model output, got shape {array.shape}")
    return np.asarray(array, dtype=np.float32)


def _input_numpy(value: Any) -> np.ndarray:
    if isinstance(value, np.ndarray):
        return value
    if hasattr(value, "detach"):
        return value.detach().cpu().numpy()
    return np.asarray(value)


def _probabilities(
    logits: np.ndarray,
    metadata: Mapping[str, Any],
    *,
    backend: ModelBackend | None = None,
) -> np.ndarray:
    configured = metadata.get("output_activation") or metadata.get("activation")
    activation = str(
        configured
        or ("auto" if backend is ModelBackend.ONNX else "sigmoid")
    ).casefold()
    values = np.nan_to_num(
        np.asarray(logits, dtype=np.float32),
        nan=-100.0,
        posinf=100.0,
        neginf=-100.0,
    )
    if activation in {"none", "identity", "probabilities", "probability"}:
        return np.clip(values, 0.0, 1.0)
    if activation == "auto" and values.size:
        if float(values.min()) >= -1e-6 and float(values.max()) <= 1.000001:
            return np.clip(values, 0.0, 1.0)
    # Numerically stable sigmoid.
    positive = values >= 0
    result = np.empty_like(values)
    result[positive] = 1.0 / (1.0 + np.exp(-values[positive]))
    exp_values = np.exp(values[~positive])
    result[~positive] = exp_values / (1.0 + exp_values)
    return result


def _select_tags(
    record: ModelRecord,
    values: np.ndarray,
    thresholds: Mapping[str, float],
    cutoff_values: np.ndarray | None = None,
) -> list[TagItem]:
    scores = np.asarray(values, dtype=np.float32).reshape(-1)
    if cutoff_values is None or cutoff_values.size != scores.size:
        cutoff_values = _tag_cutoffs(record, thresholds, size=scores.size)
    indexes = np.flatnonzero(scores >= cutoff_values)
    if indexes.size:
        indexes = indexes[np.argsort(-scores[indexes], kind="stable")]
    tags: list[TagItem] = []
    for index_value in indexes.tolist():
        index = int(index_value)
        text = record.tags[index] if index < len(record.tags) else f"tag_{index}"
        category = record.categories.get(text, "general")
        tags.append(
            TagItem(
                text=text,
                category=category,
                score=float(scores[index]),
                source="local",
                model_id=record.model_id,
            )
        )
    return tags


def _tag_cutoffs(
    record: ModelRecord,
    thresholds: Mapping[str, float],
    *,
    size: int | None = None,
) -> np.ndarray:
    count = len(record.tags) if size is None else max(0, int(size))
    default = float(thresholds.get("default", 0.35))
    values = np.full(count, default, dtype=np.float32)
    for index, text in enumerate(record.tags[:count]):
        values[index] = float(thresholds.get(record.categories.get(text, "general"), default))
    return values


def _cached_tag_cutoffs(
    loaded: LoadedModel, thresholds: Mapping[str, float]
) -> np.ndarray:
    key = tuple(sorted((str(name), float(value)) for name, value in thresholds.items()))
    with loaded.lock:
        cached = loaded.cutoff_cache.get(key)
        if cached is not None:
            loaded.cutoff_cache.move_to_end(key)
            return cached
        values = _tag_cutoffs(loaded.record, thresholds)
        values.setflags(write=False)
        loaded.cutoff_cache[key] = values
        while len(loaded.cutoff_cache) > 16:
            loaded.cutoff_cache.popitem(last=False)
        return values


def normalize_tag_name(value: str) -> str:
    """Return the merge key for one local-model tag.

    Delegates to the shared :func:`tagger2.tag_text.canonical_tag_name` rule;
    the alias is kept for the long-standing public name.
    """

    return canonical_tag_name(value)


def merge_predictions(
    predictions: Mapping[str, Iterable[TagItem | Mapping[str, Any]]] | Iterable[Iterable[TagItem | Mapping[str, Any]]]
) -> list[TagItem]:
    """Merge local-model tags by normalised name and highest confidence."""

    groups = predictions.values() if isinstance(predictions, Mapping) else predictions
    best: dict[str, TagItem] = {}
    sources: dict[str, list[str]] = {}
    model_ids: dict[str, list[str]] = {}
    for group in groups:
        for raw in group:
            item = raw if isinstance(raw, TagItem) else TagItem.model_validate(raw)
            key = normalize_tag_name(item.text)
            if not key:
                continue
            if item.source and item.source not in sources.setdefault(key, []):
                sources[key].append(item.source)
            if item.model_id and item.model_id not in model_ids.setdefault(key, []):
                model_ids[key].append(item.model_id)
            current = best.get(key)
            current_score = current.score if current and current.score is not None else -1.0
            new_score = item.score if item.score is not None else -1.0
            if current is None or new_score > current_score:
                best[key] = item.model_copy(deep=True)
    result: list[TagItem] = []
    for key, item in best.items():
        item.source = "+".join(sources.get(key, [item.source]))
        item.model_id = ",".join(model_ids.get(key, [item.model_id]))
        result.append(item)
    result.sort(key=lambda item: item.score if item.score is not None else -1.0, reverse=True)
    return result


def _load_pil(source: str | os.PathLike[str] | bytes) -> Image.Image:
    from .security import open_image_secure

    return open_image_secure(source)


def _run_classifier(
    classifier: Any, image: Image.Image, tags: Sequence[TagItem]
) -> dict[str, Any]:
    """Bridge both the new ``classify`` hook and the legacy manager API."""

    try:
        if hasattr(classifier, "classify"):
            value = classifier.classify(image, tags)
        elif hasattr(classifier, "predict"):
            try:
                value = classifier.predict(image, use_aesthetic=True)
            except TypeError:
                value = classifier.predict(image)
        elif callable(classifier):
            value = classifier(image, tags)
        else:
            raise TypeError("unsupported classifier interface")
        if not isinstance(value, Mapping):
            raise TypeError("classifier result must be a mapping")
        return dict(value)
    except Exception:
        # Optional aesthetic classification must not discard otherwise
        # valid tagger output. Keep the failure structured and path-free.
        logger.warning("aesthetic classifier failed; output continues without scores", exc_info=True)
        return {"errors": ["classifier_failed"]}


def _run_classifier_batch(
    classifier: Any,
    images: Sequence[Image.Image],
    tags: Sequence[Sequence[TagItem]],
    *,
    batch_size: int,
) -> list[dict[str, Any]]:
    try:
        if hasattr(classifier, "classify_batch"):
            try:
                values = classifier.classify_batch(images, tags, batch_size=batch_size)
            except TypeError:
                values = classifier.classify_batch(images, batch_size=batch_size)
        elif hasattr(classifier, "predict_batch"):
            values = classifier.predict_batch(
                images,
                use_aesthetic=True,
                batch_size=batch_size,
            )
        else:
            return [
                _run_classifier(classifier, image, image_tags)
                for image, image_tags in zip(images, tags, strict=True)
            ]
        if not isinstance(values, Sequence) or len(values) != len(images):
            raise TypeError("classifier batch result length does not match input")
        return [dict(value) if isinstance(value, Mapping) else {"errors": ["classifier_failed"]} for value in values]
    except Exception:
        logger.warning("batch aesthetic classifier failed; output continues without scores", exc_info=True)
        return [{"errors": ["classifier_failed"]} for _ in images]


def _is_pickle_restriction_error(exc: Exception) -> bool:
    """Only a weights_only/pickle-restriction rejection may escalate to unsafe unpickling.

    Verified against torch 2.10: restricted loads raise ``pickle.UnpicklingError``
    (message prefixed with "Weights only load failed") or, for TorchScript/legacy
    archives, a ``RuntimeError`` naming ``weights_only=True``. Transient I/O
    failures (missing or truncated file, permissions) must not unlock full pickle
    deserialisation, so everything else is rejected here.
    """

    if isinstance(exc, pickle.UnpicklingError):
        return True
    if not isinstance(exc, (RuntimeError, TypeError, ValueError)):
        return False
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in ("weights only load failed", "weightsunpickler", "weights_only", "not an allowed global")
    )


__all__ = [
    "InferenceError",
    "UnsafeModelError",
    "ModelLoadError",
    "AdapterError",
    "ClassifierHook",
    "AdapterConfig",
    "LocalPrediction",
    "LoadedModel",
    "select_device",
    "threshold_snapshot",
    "LocalInferenceEngine",
    "InferenceEngine",
    "normalize_tag_name",
    "merge_predictions",
]


InferenceEngine = LocalInferenceEngine
