"""Model-driven image preprocessing.

Profiles are resolved in this order: ``preprocess.json``, ``config.json``
(``preprocess``/``pretrained_cfg`` included), then ``normalize.json``.  The
resolved profile is immutable and can therefore be shared safely by concurrent
requests.
"""

from __future__ import annotations

import io
import json
import math
import os
from concurrent.futures import Executor
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence, cast

import numpy as np
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .security import UploadValidationError, open_image_secure


DEFAULT_MEAN = (0.485, 0.456, 0.406)
DEFAULT_STD = (0.229, 0.224, 0.225)


class PreprocessStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pad", "resize", "crop", "normalize", "to_tensor"]
    size: tuple[int, int] | None = None  # (height, width)
    interpolation: str = "bicubic"
    value: tuple[int, int, int] = (255, 255, 255)
    mean: tuple[float, float, float] | None = None
    std: tuple[float, float, float] | None = None
    mode: str = "stretch"

    @field_validator("size", mode="before")
    @classmethod
    def _parse_size(cls, value: Any) -> tuple[int, int] | None:
        return parse_image_size(value)


class PreprocessProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_size: tuple[int, int] = (448, 448)  # (height, width)
    interpolation: str = "bicubic"
    resize_mode: Literal["stretch", "fit", "shortest"] = "stretch"
    crop_mode: Literal["none", "center"] = "none"
    crop_pct: float = Field(default=1.0, gt=0.0, le=1.0)
    pad_to_size: bool = False
    pad_value: tuple[int, int, int] = (255, 255, 255)
    mean: tuple[float, float, float] = DEFAULT_MEAN
    std: tuple[float, float, float] = DEFAULT_STD
    channel_order: Literal["rgb", "bgr"] = "rgb"
    scale: float = Field(default=1.0 / 255.0, gt=0.0)
    steps: tuple[PreprocessStep, ...] = ()
    source: str = "defaults"

    @field_validator("input_size", mode="before")
    @classmethod
    def _parse_input_size(cls, value: Any) -> tuple[int, int]:
        return parse_image_size(value) or (448, 448)

    @field_validator("mean", "std", mode="before")
    @classmethod
    def _parse_stats(cls, value: Any) -> tuple[float, float, float]:
        if isinstance(value, np.ndarray):
            value = value.tolist()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("normalization values must contain three numbers")
        numbers = tuple(float(item) for item in value)
        if len(numbers) != 3:
            raise ValueError("normalization values must contain three numbers")
        return numbers

    @field_validator("std")
    @classmethod
    def _nonzero_std(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(item <= 0 for item in value):
            raise ValueError("normalization std values must be positive")
        return value

    @property
    def height(self) -> int:
        return self.input_size[0]

    @property
    def width(self) -> int:
        return self.input_size[1]


def parse_image_size(value: Any) -> tuple[int, int] | None:
    """Parse common timm/Hugging Face size encodings into ``(H, W)``."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        if "height" in value and "width" in value:
            return int(value["height"]), int(value["width"])
        for key in ("size", "shortest_edge", "image_size", "input_size"):
            if key in value:
                return parse_image_size(value[key])
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        size = int(value)
        return (size, size) if size > 0 else None
    if isinstance(value, str):
        clean = value.casefold().replace(" ", "")
        if clean.isdigit():
            return parse_image_size(int(clean))
        for separator in ("x", ","):
            if separator in clean:
                parts = clean.split(separator)
                try:
                    return parse_image_size([int(part) for part in parts])
                except ValueError:
                    return None
        return None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = [int(item) for item in value]
        if len(values) >= 3 and values[-3] in {1, 3, 4}:
            values = values[-2:]
        elif len(values) > 2:
            values = values[-2:]
        if len(values) == 1 and values[0] > 0:
            return values[0], values[0]
        if len(values) == 2 and all(item > 0 for item in values):
            return values[0], values[1]
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _first(mapping: Mapping[str, Any], *paths: str) -> Any:
    for dotted in paths:
        value: Any = mapping
        for part in dotted.split("."):
            if not isinstance(value, Mapping) or part not in value:
                value = None
                break
            value = value[part]
        if value is not None:
            return value
    return None


def _parse_color(value: Any, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if isinstance(value, str):
        named = {
            "black": (0, 0, 0),
            "white": (255, 255, 255),
            "gray": (128, 128, 128),
            "grey": (128, 128, 128),
        }
        return named.get(value.strip().casefold(), default)
    if isinstance(value, (int, float)):
        number = max(0, min(255, int(value)))
        return number, number, number
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        numbers = tuple(max(0, min(255, int(item))) for item in value)
        if len(numbers) == 3:
            return numbers
    return default


def _normalise_interpolation(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {
            0: "nearest",
            1: "lanczos",
            2: "bilinear",
            3: "bicubic",
            4: "box",
            5: "hamming",
        }.get(int(value), "bicubic")
    text = str(value or "bicubic").casefold().replace("interpolationmode.", "")
    aliases = {"linear": "bilinear", "cubic": "bicubic", "lanczos3": "lanczos"}
    return aliases.get(text, text) if text in {"nearest", "bilinear", "bicubic", "lanczos", "box", "hamming"} or text in aliases else "bicubic"


def _parse_steps(value: Any) -> tuple[PreprocessStep, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    steps: list[PreprocessStep] = []
    for raw in value:
        if isinstance(raw, str):
            raw = {"type": raw}
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("type") or raw.get("name") or raw.get("kind") or "").casefold()
        compact = recompact(name)
        if compact in {"padtosize", "pad", "letterbox"}:
            kind = "pad"
        elif compact in {"resize", "resizeshortest", "shortestedge"}:
            kind = "resize"
        elif compact in {"centercrop", "crop"}:
            kind = "crop"
        elif compact in {"normalize", "normalise"}:
            kind = "normalize"
        elif compact in {"totensor", "tensor"}:
            kind = "to_tensor"
        else:
            continue
        size = parse_image_size(raw.get("size") or raw.get("target_size") or raw.get("image_size"))
        mode = str(raw.get("mode") or ("shortest" if "shortest" in compact else "stretch")).casefold()
        kwargs: dict[str, Any] = {
            "kind": kind,
            "size": size,
            "interpolation": _normalise_interpolation(raw.get("interpolation")),
            "value": _parse_color(
                raw.get("value") or raw.get("fill") or raw.get("background_color"),
                (255, 255, 255),
            ),
            "mode": mode,
        }
        if kind == "normalize":
            kwargs["mean"] = raw.get("mean", DEFAULT_MEAN)
            kwargs["std"] = raw.get("std", DEFAULT_STD)
        steps.append(PreprocessStep.model_validate(kwargs))
    return tuple(steps)


def recompact(value: str) -> str:
    return "".join(character for character in value if character.isalnum())


def load_preprocess_profile(model_dir: str | os.PathLike[str]) -> PreprocessProfile:
    root = Path(model_dir)
    preprocess_path = root / "preprocess.json"
    config_path = root / "config.json"
    processor_path = root / "preprocessor_config.json"
    normalize_path = root / "normalize.json"
    if not normalize_path.is_file() and root.parent != root:
        normalize_path = root.parent / "normalize.json"

    explicit = _read_json(preprocess_path)
    config = _read_json(config_path)
    processor = _read_json(processor_path)
    normalise = _read_json(normalize_path)
    nested: Mapping[str, Any] = (
        config["preprocess"] if isinstance(config.get("preprocess"), Mapping) else {}
    )
    pretrained: Mapping[str, Any] = (
        config["pretrained_cfg"]
        if isinstance(config.get("pretrained_cfg"), Mapping)
        else {}
    )
    # Highest-precedence values go first in the merged view.
    merged: dict[str, Any] = {}
    for mapping in (config, processor, pretrained, nested, explicit):
        if isinstance(mapping, Mapping):
            merged.update(mapping)

    explicit_size = parse_image_size(
        _first(
            explicit,
            "input_size",
            "image_size",
            "size",
            "crop_size",
        )
        or _first(nested, "input_size", "image_size", "size", "crop_size")
        or _first(pretrained, "input_size", "image_size", "size", "crop_size")
        or _first(processor, "input_size", "image_size", "size", "crop_size")
        or _first(config, "input_size", "image_size", "resolution", "model.image_size")
    )
    size = explicit_size or (448, 448)

    mean = _first(explicit, "mean", "normalize.mean")
    mean = mean if mean is not None else _first(nested, "mean", "normalize.mean")
    mean = mean if mean is not None else _first(pretrained, "mean")
    mean = mean if mean is not None else _first(processor, "mean", "image_mean")
    mean = mean if mean is not None else normalise.get("mean", DEFAULT_MEAN)
    std = _first(explicit, "std", "normalize.std")
    std = std if std is not None else _first(nested, "std", "normalize.std")
    std = std if std is not None else _first(pretrained, "std")
    std = std if std is not None else _first(processor, "std", "image_std")
    std = std if std is not None else normalise.get("std", DEFAULT_STD)

    raw_steps = explicit.get("steps") or explicit.get("transforms") or explicit.get("operations")
    if raw_steps is None:
        # timm exporters commonly namespace the evaluation pipeline under
        # ``test``/``val`` instead of using a top-level transforms key.
        for key in ("test", "validation", "val", "eval", "inference"):
            if isinstance(explicit.get(key), Sequence) and not isinstance(explicit.get(key), (str, bytes)):
                raw_steps = explicit[key]
                break
    steps = _parse_steps(raw_steps)
    if explicit_size is None:
        for step in reversed(steps):
            if step.size is not None and step.kind in {"resize", "crop", "pad"}:
                size = step.size
                break
    # Some exporters store normalization only inside the operation list.
    # Promote it to the immutable profile used by the tensor conversion stage.
    for step in steps:
        if step.kind == "normalize":
            if step.mean is not None:
                mean = step.mean
            if step.std is not None:
                std = step.std
            break
    resize_value = str(merged.get("resize_mode") or "stretch").casefold()
    if resize_value not in {"stretch", "fit", "shortest"}:
        resize_value = "stretch"
    crop_value = str(merged.get("crop_mode") or "none").casefold()
    if bool(merged.get("center_crop")):
        crop_value = "center"
    if crop_value not in {"none", "center"}:
        crop_value = "none"
    if crop_value == "center" and "resize_mode" not in merged:
        resize_value = "shortest"
    resize_mode = cast(Literal["stretch", "fit", "shortest"], resize_value)
    crop_mode = cast(Literal["none", "center"], crop_value)
    channel_value = str(merged.get("channel_order") or "rgb").casefold()
    if channel_value not in {"rgb", "bgr"}:
        channel_value = "rgb"
    channel_order = cast(Literal["rgb", "bgr"], channel_value)
    pad_value = _parse_color(
        merged.get("pad_value", merged.get("fill", (255, 255, 255))),
        (255, 255, 255),
    )
    source_parts = [
        path.name
        for path in (preprocess_path, config_path, processor_path, normalize_path)
        if path.is_file()
    ]
    return PreprocessProfile(
        input_size=size,
        interpolation=_normalise_interpolation(
            merged.get("interpolation", processor.get("resample"))
        ),
        resize_mode=resize_mode,
        crop_mode=crop_mode,
        crop_pct=float(merged.get("crop_pct") or 1.0),
        pad_to_size=bool(
            merged.get("pad_to_size")
            or merged.get("pad_to_square")
            or merged.get("letterbox")
        ),
        pad_value=pad_value,
        mean=mean,
        std=std,
        channel_order=channel_order,
        scale=float(
            merged.get("scale")
            or processor.get("rescale_factor")
            or (1.0 / 255.0)
        ),
        steps=steps,
        source="+".join(source_parts) or "defaults",
    )


def _resample(name: str) -> Image.Resampling:
    return {
        "nearest": Image.Resampling.NEAREST,
        "bilinear": Image.Resampling.BILINEAR,
        "bicubic": Image.Resampling.BICUBIC,
        "lanczos": Image.Resampling.LANCZOS,
        "box": Image.Resampling.BOX,
        "hamming": Image.Resampling.HAMMING,
    }.get(name.casefold(), Image.Resampling.BICUBIC)


def _pad(image: Image.Image, size: tuple[int, int], value: tuple[int, int, int], interpolation: str) -> Image.Image:
    height, width = size
    if image.width > width or image.height > height:
        ratio = min(width / image.width, height / image.height)
        resized = (
            max(1, round(image.width * ratio)),
            max(1, round(image.height * ratio)),
        )
        image = image.resize(resized, _resample(interpolation))
    left = max(0, (width - image.width) // 2)
    top = max(0, (height - image.height) // 2)
    right = max(0, width - image.width - left)
    bottom = max(0, height - image.height - top)
    return ImageOps.expand(image, border=(left, top, right, bottom), fill=value)


def _resize(image: Image.Image, size: tuple[int, int], mode: str, interpolation: str, fill: tuple[int, int, int]) -> Image.Image:
    height, width = size
    sample = _resample(interpolation)
    if mode == "fit":
        result = ImageOps.contain(image, (width, height), method=sample)
        return _pad(result, size, fill, interpolation)
    if mode == "shortest":
        scale = max(width / image.width, height / image.height)
        resized = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            sample,
        )
        return ImageOps.fit(resized, (width, height), method=sample, centering=(0.5, 0.5))
    return image.resize((width, height), sample)


def _crop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    height, width = size
    if image.width < width or image.height < height:
        image = _pad(image, size, (255, 255, 255), "bicubic")
    left = max(0, (image.width - width) // 2)
    top = max(0, (image.height - height) // 2)
    return image.crop((left, top, left + width, top + height))


def transform_pil(image: Image.Image, profile: PreprocessProfile) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    if profile.steps:
        for step in profile.steps:
            size = step.size or profile.input_size
            if step.kind == "pad":
                image = _pad(image, size, step.value, step.interpolation)
            elif step.kind == "resize":
                image = _resize(image, size, step.mode, step.interpolation, step.value)
            elif step.kind == "crop":
                image = _crop(image, size)
        if image.size != (profile.width, profile.height):
            image = _resize(
                image,
                profile.input_size,
                profile.resize_mode,
                profile.interpolation,
                profile.pad_value,
            )
        return image

    target = profile.input_size
    if profile.pad_to_size:
        image = _pad(image, target, profile.pad_value, profile.interpolation)
    if profile.crop_mode == "center" and profile.crop_pct < 1.0:
        resize_size = (
            max(target[0], round(target[0] / profile.crop_pct)),
            max(target[1], round(target[1] / profile.crop_pct)),
        )
        image = _resize(image, resize_size, "shortest", profile.interpolation, profile.pad_value)
        image = _crop(image, target)
    elif image.size != (target[1], target[0]):
        image = _resize(
            image,
            target,
            profile.resize_mode,
            profile.interpolation,
            profile.pad_value,
        )
    if profile.crop_mode == "center" and image.size != (target[1], target[0]):
        image = _crop(image, target)
    return image


def _normalise_numpy(
    transformed: Image.Image, profile: PreprocessProfile
) -> np.ndarray:
    array = np.asarray(transformed, dtype=np.float32) * np.float32(profile.scale)
    if profile.channel_order == "bgr":
        array = array[..., ::-1].copy()
    mean = np.asarray(profile.mean, dtype=np.float32)
    std = np.asarray(profile.std, dtype=np.float32)
    array = (array - mean) / std
    return np.ascontiguousarray(array.transpose(2, 0, 1), dtype=np.float32)


def preprocess_to_numpy(image: Image.Image, profile: PreprocessProfile) -> np.ndarray:
    return _normalise_numpy(transform_pil(image, profile), profile)


@lru_cache(maxsize=128)
def _torch_stats(
    mean: tuple[float, float, float], std: tuple[float, float, float]
):
    import torch

    return (
        torch.tensor(mean, dtype=torch.float32).view(3, 1, 1),
        torch.tensor(std, dtype=torch.float32).view(3, 1, 1),
    )


def _normalise_tensor(transformed: Image.Image, profile: PreprocessProfile):
    import torch

    try:
        from torchvision.transforms.functional import pil_to_tensor

        tensor = pil_to_tensor(transformed)
    except ImportError:  # pragma: no cover - torchvision is a runtime dependency
        raw = np.array(transformed, dtype=np.uint8, copy=True)
        tensor = torch.from_numpy(raw).permute(2, 0, 1)
    tensor = tensor.to(dtype=torch.float32)
    if profile.channel_order == "bgr":
        tensor = tensor[[2, 1, 0], ...]
    mean, std = _torch_stats(profile.mean, profile.std)
    return tensor.mul_(profile.scale).sub_(mean).div_(std)


def preprocess_image(
    source: str | os.PathLike[str] | bytes | Image.Image,
    profile: PreprocessProfile,
    *,
    as_numpy: bool = False,
    tensor_fast_path: bool = True,
    max_bytes: int = 32 * 1024 * 1024,
    max_pixels: int = 80_000_000,
    max_edge: int = 16_384,
):
    if isinstance(source, Image.Image):
        image = source.copy()
        if image.width * image.height > max_pixels or max(image.size) > max_edge:
            raise UploadValidationError("image dimensions exceed limit")
    else:
        image = open_image_secure(
            source,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            max_edge=max_edge,
        )
    transformed = transform_pil(image, profile)
    if as_numpy and not tensor_fast_path:
        return _normalise_numpy(transformed, profile)
    try:
        tensor = _normalise_tensor(transformed, profile)
    except ImportError as exc:  # pragma: no cover - runtime dependency
        if as_numpy:
            return _normalise_numpy(transformed, profile)
        raise RuntimeError("PyTorch is required for tensor preprocessing") from exc
    return tensor.numpy() if as_numpy else tensor


def preprocess_batch(
    images: Iterable[str | os.PathLike[str] | bytes | Image.Image],
    profile: PreprocessProfile,
    *,
    as_numpy: bool = False,
    executor: Executor | None = None,
):
    # NumPy scales better across the bounded preprocessing pool. Torch's CPU
    # elementwise kernels are faster for a single image but contend with one
    # another when several workers normalise batches concurrently.
    worker = partial(
        preprocess_image,
        profile=profile,
        as_numpy=True,
        tensor_fast_path=False,
    )
    arrays = (
        list(executor.map(worker, images))
        if executor is not None
        else [worker(image) for image in images]
    )
    if not arrays:
        shape = (0, 3, profile.height, profile.width)
        return np.empty(shape, dtype=np.float32) if as_numpy else _torch_empty(shape)
    batch = np.stack(arrays, axis=0)
    if as_numpy:
        return batch
    try:
        import torch
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyTorch is required for tensor preprocessing") from exc
    return torch.from_numpy(batch)


def _torch_empty(shape: tuple[int, ...]):
    try:
        import torch
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyTorch is required for tensor preprocessing") from exc
    return torch.empty(shape, dtype=torch.float32)


def prepare_online_image(
    source: str | os.PathLike[str] | bytes | Image.Image,
    *,
    max_edge: int = 2048,
    max_bytes: int = 8 * 1024 * 1024,
    quality: int = 92,
) -> tuple[bytes, str]:
    """Orient, RGB-normalise and size an image for a vision provider."""

    if isinstance(source, Image.Image):
        image = ImageOps.exif_transpose(source).convert("RGB")
    else:
        image = open_image_secure(source)
    if max(image.size) > max_edge:
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    current_quality = max(55, min(95, quality))
    while True:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=current_quality, optimize=True)
        data = output.getvalue()
        if len(data) <= max_bytes:
            return data, "image/jpeg"
        if current_quality > 60:
            current_quality -= 8
            continue
        if max(image.size) <= 512:
            raise UploadValidationError("image cannot be compressed below provider limit")
        scale = math.sqrt(max_bytes / len(data)) * 0.92
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.Resampling.LANCZOS,
        )


# Common name used by older integrations.
load_profile = load_preprocess_profile
resolve_preprocess_profile = load_preprocess_profile
preprocess = preprocess_image


__all__ = [
    "DEFAULT_MEAN",
    "DEFAULT_STD",
    "PreprocessStep",
    "PreprocessProfile",
    "parse_image_size",
    "load_preprocess_profile",
    "load_profile",
    "resolve_preprocess_profile",
    "transform_pil",
    "preprocess_to_numpy",
    "preprocess_image",
    "preprocess",
    "preprocess_batch",
    "prepare_online_image",
]
