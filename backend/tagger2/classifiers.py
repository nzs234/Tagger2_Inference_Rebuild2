"""Lazy local aesthetic scoring with the LSE14 fusion model."""

from __future__ import annotations

import gc
import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast

from PIL import Image, ImageOps

from .schemas import TagItem


ClassifierName = Literal["aesthetic"]


class ClassifierIssueDict(TypedDict):
    classifier: str
    code: str
    message: str
    retryable: bool


class ClassifierOutput(TypedDict, total=False):
    aesthetic: dict[str, Any]
    errors: list[ClassifierIssueDict]


class ClassifierBackend(Protocol):
    def classify_batch(
        self, images: Sequence[Image.Image], *, batch_size: int
    ) -> list[Mapping[str, Any]]: ...

    def unload(self) -> None: ...


BackendFactory = Callable[["ClassifierConfig"], ClassifierBackend]


class ClassifierError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def public(self) -> ClassifierIssueDict:
        return {
            "classifier": "aesthetic",
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def _default_project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class ClassifierConfig:
    project_dir: Path = field(default_factory=_default_project_dir)
    models_dir: Path | None = None
    cache_dir: Path | None = None
    device: str = "auto"
    batch_size: int = 4
    auto_download: bool = True

    def __post_init__(self) -> None:
        project_dir = Path(self.project_dir).expanduser().resolve(strict=False)
        models_dir = Path(self.models_dir or project_dir / "models").expanduser().resolve(
            strict=False
        )
        cache_dir = Path(
            self.cache_dir or project_dir / "data_cache" / "huggingface"
        ).expanduser().resolve(strict=False)
        if int(self.batch_size) < 1:
            raise ValueError("classifier batch_size must be at least 1")
        device = str(self.device or "auto").strip().casefold()
        if not (device == "auto" or device == "cpu" or device.startswith("cuda")):
            raise ValueError("classifier device must be auto, cpu, or cuda")
        object.__setattr__(self, "project_dir", project_dir)
        object.__setattr__(self, "models_dir", models_dir)
        object.__setattr__(self, "cache_dir", cache_dir)
        object.__setattr__(self, "device", device)
        object.__setattr__(self, "batch_size", int(self.batch_size))


class AestheticClassifier:
    """Thread-safe, lazy LSE14 scorer used by local inference jobs."""

    def __init__(
        self,
        config: ClassifierConfig | None = None,
        *,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self.config = config or ClassifierConfig()
        self._factory = backend_factory or _load_lse14_backend
        self._backend: ClassifierBackend | None = None
        self._failure: ClassifierError | None = None
        self._state_lock = threading.RLock()
        self._run_lock = threading.RLock()

    def classify(self, image: Image.Image, tags: Sequence[TagItem]) -> Mapping[str, Any]:
        return self.classify_batch([image], [tags], batch_size=1)[0]

    def classify_batch(
        self,
        images: Sequence[Image.Image],
        tags: Sequence[Sequence[TagItem]] | None = None,
        *,
        batch_size: int | None = None,
    ) -> list[ClassifierOutput]:
        if not images:
            return []
        if tags is not None and len(tags) != len(images):
            raise ValueError("tags and images must have the same length")
        if any(not isinstance(image, Image.Image) for image in images):
            raise TypeError("classifier images must be PIL images")
        outputs: list[ClassifierOutput] = [{"errors": []} for _ in images]
        with self._run_lock:
            backend = self._get_backend()
            if backend is None:
                issue = cast(ClassifierError, self._failure).public()
                for output in outputs:
                    output["errors"].append(issue)
                return outputs
            try:
                values = backend.classify_batch(
                    images,
                    batch_size=max(1, int(batch_size or self.config.batch_size)),
                )
                if len(values) != len(images):
                    raise ValueError("classifier backend returned the wrong result count")
                for output, value in zip(outputs, values, strict=True):
                    aesthetic = value.get("aesthetic") if isinstance(value, Mapping) else None
                    if isinstance(aesthetic, Mapping):
                        output["aesthetic"] = dict(aesthetic)
            except Exception as exc:
                issue = _inference_issue(exc).public()
                for output in outputs:
                    output["errors"].append(issue)
        return outputs

    def load(self, classifier: ClassifierName | None = None) -> dict[str, Any]:
        if classifier not in {None, "aesthetic"}:
            raise ValueError("unknown classifier")
        with self._run_lock:
            self._get_backend()
        return self.status()

    def unload(self, classifier: ClassifierName | None = None) -> None:
        if classifier not in {None, "aesthetic"}:
            raise ValueError("unknown classifier")
        with self._run_lock, self._state_lock:
            backend = self._backend
            self._backend = None
            self._failure = None
        if backend is not None:
            try:
                backend.unload()
            except Exception:
                pass
        _collect_model_memory()

    close = unload

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "aesthetic": {
                    "enabled": True,
                    "loaded": self._backend is not None,
                    "error": self._failure.public() if self._failure is not None else None,
                    "backend": "lse14_fusion_1k",
                    "scale": "1-5",
                }
            }

    def _get_backend(self) -> ClassifierBackend | None:
        with self._state_lock:
            if self._backend is not None:
                return self._backend
            if self._failure is not None:
                return None
            try:
                backend = self._factory(self.config)
                if not hasattr(backend, "classify_batch") or not hasattr(backend, "unload"):
                    raise TypeError("invalid classifier backend")
            except ClassifierError as exc:
                self._failure = exc
                return None
            except ImportError:
                self._failure = ClassifierError(
                    "classifier_dependency_missing",
                    "LSE14 scoring dependencies are not installed.",
                )
                return None
            except Exception:
                self._failure = ClassifierError(
                    "aesthetic_load_failed",
                    "The LSE14 aesthetic scorer could not be loaded.",
                )
                return None
            self._backend = backend
            return backend


class _Lse14Backend:
    def __init__(
        self,
        siglip: Any,
        siglip_processor: Any,
        clip: Any,
        clip_processor: Any,
        head: Any,
        *,
        torch_module: Any,
        device: str,
        dtype: Any,
        input_dim: int,
    ) -> None:
        self.siglip = siglip
        self.siglip_processor = siglip_processor
        self.clip = clip
        self.clip_processor = clip_processor
        self.head = head
        self.torch = torch_module
        self.device = device
        self.dtype = dtype
        self.input_dim = input_dim

    def classify_batch(
        self, images: Sequence[Image.Image], *, batch_size: int
    ) -> list[Mapping[str, Any]]:
        output: list[Mapping[str, Any]] = []
        offset = 0
        current_batch = min(max(1, batch_size), len(images))
        while offset < len(images):
            chunk = [
                ImageOps.exif_transpose(image).convert("RGB")
                for image in images[offset : offset + current_batch]
            ]
            try:
                with self.torch.inference_mode():
                    siglip_features = self._siglip_features(chunk)
                    clip_features = self._clip_features(chunk)
                    fused = self.torch.cat([siglip_features, clip_features], dim=-1)
                    if int(fused.shape[-1]) != self.input_dim:
                        raise ValueError("LSE14 feature dimensions do not match the checkpoint")
                    scores, domain_logits = self.head(fused)
                    score_rows = scores.float().detach().cpu().tolist()
                    domain_values = self.torch.sigmoid(domain_logits).float().detach().cpu().tolist()
                for values, domain in zip(score_rows, domain_values, strict=True):
                    score = float(values[0])
                    bucket = max(1, min(5, int(round(score))))
                    output.append(
                        {
                            "aesthetic": {
                                "token": f"score_{bucket}",
                                "score": round(score, 4),
                                "bucket": bucket,
                                "composition": round(float(values[1]), 4),
                                "color": round(float(values[2]), 4),
                                "sexual": round(float(values[3]), 4),
                                "in_domain_probability": round(float(domain), 6),
                                "backend": "lse14_fusion_1k",
                            }
                        }
                    )
                offset += len(chunk)
            except Exception as exc:
                if not _is_out_of_memory(exc) or current_batch <= 1:
                    raise
                current_batch = max(1, current_batch // 2)
                _empty_cuda_cache()
        return output

    def _siglip_features(self, images: Sequence[Image.Image]) -> Any:
        inputs = self.siglip_processor(images=list(images), return_tensors="pt")
        inputs = {
            key: value.to(self.device, non_blocking=self.device.startswith("cuda"))
            for key, value in inputs.items()
            if hasattr(value, "to")
        }
        if "pixel_values" in inputs:
            inputs["pixel_values"] = inputs["pixel_values"].to(dtype=self.dtype)
        vision = getattr(self.siglip, "vision_model", self.siglip)
        kwargs: dict[str, Any] = {"pixel_values": inputs["pixel_values"]}
        if "pixel_attention_mask" in inputs:
            kwargs["attention_mask"] = inputs["pixel_attention_mask"]
        if "spatial_shapes" in inputs:
            kwargs["spatial_shapes"] = inputs["spatial_shapes"]
        result = vision(**kwargs, output_hidden_states=True, return_dict=True)
        hidden_states = getattr(result, "hidden_states", None)
        if hidden_states:
            features = hidden_states[-1].mean(dim=1)
        else:
            features = _extract_features(result)
        return self.torch.nn.functional.normalize(features.float(), dim=-1).to(self.dtype)

    def _clip_features(self, images: Sequence[Image.Image]) -> Any:
        inputs = self.clip_processor(images=list(images), return_tensors="pt")
        pixel_values = inputs["pixel_values"].to(
            self.device,
            dtype=self.dtype,
            non_blocking=self.device.startswith("cuda"),
        )
        result = self.clip.get_image_features(pixel_values=pixel_values)
        features = result if self.torch.is_tensor(result) else _extract_features(result)
        return self.torch.nn.functional.normalize(features.float(), dim=-1).to(self.dtype)

    def unload(self) -> None:
        self.siglip = None
        self.clip = None
        self.head = None
        self.siglip_processor = None
        self.clip_processor = None


def _load_lse14_backend(config: ClassifierConfig) -> ClassifierBackend:
    try:
        import torch
        from huggingface_hub import snapshot_download
        from safetensors import safe_open
        from safetensors.torch import load_file
        from transformers import AutoImageProcessor, AutoModel, CLIPModel
    except ImportError as exc:  # pragma: no cover - optional runtime
        raise ClassifierError(
            "classifier_dependency_missing",
            "Transformers, safetensors, huggingface_hub, and PyTorch are required for LSE14 scoring.",
        ) from exc

    models_dir = cast(Path, config.models_dir)
    cache_dir = cast(Path, config.cache_dir)
    scorer_dir = models_dir / "lse14__lse14-scorer"
    scorer_path = scorer_dir / "1k.safetensors"
    token = os.getenv("HF_TOKEN") or None
    if not scorer_path.is_file():
        if not config.auto_download:
            raise ClassifierError(
                "aesthetic_assets_missing",
                "The LSE14 scorer checkpoint is missing.",
            )
        scorer_dir.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_download(
                repo_id="lse14/lse14-scorer",
                local_dir=scorer_dir,
                allow_patterns=["1k.safetensors", "README.md"],
                token=token,
            )
        except Exception as exc:
            raise ClassifierError(
                "aesthetic_download_failed",
                "The LSE14 scorer checkpoint could not be downloaded.",
                retryable=True,
            ) from exc

    local_siglip = models_dir / "local_aesthetic_bundle" / "backbone"
    try:
        if (local_siglip / "model.safetensors").is_file():
            siglip_dir = local_siglip
        else:
            if not config.auto_download:
                raise FileNotFoundError
            siglip_dir = Path(
                snapshot_download(
                    repo_id="google/siglip2-so400m-patch16-naflex",
                    cache_dir=cache_dir,
                    allow_patterns=["config.json", "model.safetensors", "preprocessor_config.json"],
                    token=token,
                )
            )
        clip_dir = Path(
            snapshot_download(
                repo_id="openai/clip-vit-large-patch14",
                cache_dir=cache_dir,
                allow_patterns=["config.json", "model.safetensors", "preprocessor_config.json"],
                token=token,
                local_files_only=not config.auto_download,
            )
        )
    except Exception as exc:
        raise ClassifierError(
            "aesthetic_backbone_missing",
            "The LSE14 feature extractors are unavailable.",
            retryable=config.auto_download,
        ) from exc

    try:
        with safe_open(str(scorer_path), framework="pt", device="cpu") as stream:
            metadata = stream.metadata() or {}
        if metadata.get("format") != "fusion_multitask_v1":
            raise ValueError("unsupported checkpoint format")
        input_dim = int(metadata.get("input_dim", 0))
        hidden_dims = json.loads(metadata.get("hidden_dims_json", "[]"))
        dropout = float(metadata.get("dropout", 0.3))
        if input_dim != 1920 or not isinstance(hidden_dims, list) or not hidden_dims:
            raise ValueError("unsupported LSE14 checkpoint shape")
        head = _build_lse14_head(torch, input_dim, [int(value) for value in hidden_dims], dropout)
        incompatible = head.load_state_dict(load_file(str(scorer_path), device="cpu"), strict=False)
        missing = [key for key in incompatible.missing_keys if not key.startswith("reg_heads.background")]
        if missing:
            raise ValueError("checkpoint is missing required tensors")
        siglip_processor = AutoImageProcessor.from_pretrained(
            str(siglip_dir), local_files_only=True, trust_remote_code=False
        )
        siglip = AutoModel.from_pretrained(
            str(siglip_dir), local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
        clip_processor = AutoImageProcessor.from_pretrained(
            str(clip_dir), local_files_only=True, trust_remote_code=False
        )
        clip = CLIPModel.from_pretrained(
            str(clip_dir), local_files_only=True, trust_remote_code=False, use_safetensors=True
        )
    except Exception as exc:
        raise ClassifierError(
            "aesthetic_assets_invalid",
            "The LSE14 scorer assets are incompatible.",
        ) from exc

    device = _resolve_device(config.device, torch)
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    for model in (siglip, clip, head):
        runtime_model = cast(Any, model)
        runtime_model.to(device=device, dtype=dtype).eval()
        for parameter in runtime_model.parameters():
            parameter.requires_grad_(False)
    return _Lse14Backend(
        siglip,
        siglip_processor,
        clip,
        clip_processor,
        head,
        torch_module=torch,
        device=device,
        dtype=dtype,
        input_dim=input_dim,
    )


def _build_lse14_head(torch: Any, input_dim: int, hidden_dims: list[int], dropout: float) -> Any:
    nn = torch.nn

    class FusionHead(nn.Module):  # type: ignore[name-defined,misc]
        def __init__(self) -> None:
            super().__init__()
            layers: list[Any] = []
            previous = input_dim
            for hidden in hidden_dims:
                layers.extend(
                    [nn.LayerNorm(previous), nn.Linear(previous, hidden), nn.GELU(), nn.Dropout(dropout)]
                )
                previous = hidden
            self.trunk = nn.Sequential(*layers)
            self.reg_heads = nn.ModuleDict(
                {name: nn.Linear(previous, 1) for name in ("aesthetic", "composition", "color", "sexual")}
            )
            self.cls_head = nn.Linear(previous, 1)

        def forward(self, value: Any) -> tuple[Any, Any]:
            value = self.trunk(value)
            raw = torch.cat(
                [self.reg_heads[name](value) for name in ("aesthetic", "composition", "color", "sexual")],
                dim=-1,
            )
            return torch.sigmoid(raw) * 4.0 + 1.0, self.cls_head(value).squeeze(-1)

    return FusionHead()


def _extract_features(output: Any) -> Any:
    for name in ("image_embeds", "pooler_output"):
        value = getattr(output, name, None)
        if value is not None:
            return value
    hidden = getattr(output, "last_hidden_state", None)
    if hidden is not None and getattr(hidden, "ndim", 0) == 3:
        return hidden.mean(dim=1)
    raise ValueError("unsupported vision model output")


def _resolve_device(requested: str, torch: Any) -> str:
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested.startswith("cuda") and not torch.cuda.is_available():
        raise ClassifierError(
            "classifier_device_unavailable",
            "CUDA was requested but is not available.",
        )
    return requested


def _inference_issue(exc: Exception) -> ClassifierError:
    if isinstance(exc, ClassifierError):
        return exc
    if _is_out_of_memory(exc):
        return ClassifierError(
            "classifier_out_of_memory",
            "The LSE14 aesthetic scorer ran out of device memory.",
            retryable=True,
        )
    return ClassifierError(
        "aesthetic_inference_failed",
        "The LSE14 aesthetic scorer could not process this batch.",
    )


def _is_out_of_memory(exc: BaseException) -> bool:
    return "out of memory" in str(exc).casefold()


def _empty_cuda_cache() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _collect_model_memory() -> None:
    gc.collect()
    _empty_cuda_cache()


__all__ = [
    "AestheticClassifier",
    "ClassifierBackend",
    "ClassifierConfig",
    "ClassifierError",
    "ClassifierIssueDict",
    "ClassifierOutput",
]
