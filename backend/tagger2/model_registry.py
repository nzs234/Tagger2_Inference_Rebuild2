"""Opaque local-model discovery and metadata registry."""

from __future__ import annotations

import csv
import json
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Iterator, List, Mapping, Sequence

from .preprocessing import PreprocessProfile, load_preprocess_profile
from .schemas import ModelPublic
from .security import PathAllowlist, PathNotAllowedError, opaque_id


DEFAULT_THRESHOLDS: dict[str, float] = {
    "general": 0.35,
    "character": 0.85,
    "species": 0.35,
    "rating": 0.50,
    "default": 0.35,
}


class ModelRegistryError(RuntimeError):
    pass


class UnknownModelError(ModelRegistryError, KeyError):
    pass


class ModelBackend(str, Enum):
    ONNX = "onnx"
    PYTORCH = "pytorch"


@dataclass(slots=True)
class ModelRecord:
    """Internal model metadata.  Use :meth:`public` at the API boundary."""

    model_id: str
    name: str
    path: Path
    weight_path: Path
    backend: ModelBackend
    architecture: str
    input_size: tuple[int, int]
    num_classes: int | None
    tags: tuple[str, ...]
    categories: Mapping[str, str]
    thresholds: Mapping[str, float]
    preset_thresholds: Mapping[str, float]
    preprocess: PreprocessProfile
    unsafe_weights: bool
    trusted: bool = False
    adapter_types: tuple[str, ...] = ()
    classifier: Mapping[str, Any] | None = None
    loaded: bool = False
    load_error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def estimated_memory_mb(self) -> float:
        try:
            size = self.weight_path.stat().st_size
            # Large ONNX models may keep tensors in an adjacent external-data
            # file, conventionally ``model.onnx.data``.
            external = self.weight_path.with_name(self.weight_path.name + ".data")
            if external.is_file():
                size += external.stat().st_size
            return size / (1024.0 * 1024.0)
        except OSError:
            return 0.0

    def public(self) -> ModelPublic:
        return ModelPublic(
            model_id=self.model_id,
            name=self.name,
            backend=self.backend.value,
            architecture=self.architecture,
            input_size=self.input_size,
            num_classes=self.num_classes,
            loaded=self.loaded,
            unsafe_weights=self.unsafe_weights,
            adapter_types=list(self.adapter_types),
            thresholds=dict(self.thresholds),
        )

    to_public = public

    def threshold_snapshot(
        self,
        *,
        threshold: float | None = None,
        category_thresholds: Mapping[str, float] | None = None,
        use_category_thresholds: bool = True,
    ) -> dict[str, float]:
        values = dict(self.thresholds if use_category_thresholds else {})
        default = float(values.get("default", DEFAULT_THRESHOLDS["default"]))
        if threshold is not None:
            default = _threshold(threshold)
            values = {key: default for key in values}
        if category_thresholds:
            values.update({str(key): _threshold(value) for key, value in category_thresholds.items()})
        values["default"] = default
        return values

    def set_thresholds(self, values: Mapping[str, Any] | None = None, *, reset: bool = False) -> None:
        selected = dict(self.preset_thresholds)
        if not reset and values is not None:
            for key, value in values.items():
                selected[str(key)] = _threshold(value)
        if "default" not in selected:
            selected["default"] = selected.get("general", DEFAULT_THRESHOLDS["default"])
        self.thresholds = MappingProxyType(selected)


def _threshold(value: Any) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    return number


def normalize_category(value: Any, tag: str = "") -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return "rating" if tag.casefold() in {"general", "sensitive", "questionable", "explicit", "safe"} else "general"
    categories = {
        "0": "general",
        "general": "general",
        "gen": "general",
        "g": "general",
        "4": "character",
        "character": "character",
        "char": "character",
        "c": "character",
        "9": "rating",
        "rating": "rating",
        "rate": "rating",
        "r": "rating",
        "species": "species",
        "spec": "species",
        "s": "species",
    }
    if text in categories:
        return categories[text]
    if "char" in text:
        return "character"
    if "spec" in text:
        return "species"
    if "rat" in text:
        return "rating"
    return "other"


def _read_json(path: Path, *, limit: int = 16 * 1024 * 1024) -> Any:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {}
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _architecture(config: Mapping[str, Any]) -> str:
    candidates: list[Any] = []
    for key in ("architecture", "arch", "backbone", "model_name", "model_type"):
        candidates.append(config.get(key))
    architectures = config.get("architectures")
    if isinstance(architectures, Sequence) and not isinstance(architectures, (str, bytes)):
        candidates.extend(architectures)
    for container_name in ("model", "model_args", "pretrained_cfg"):
        container = config.get(container_name)
        if isinstance(container, Mapping):
            for key in ("architecture", "arch", "backbone", "model_name", "model_type"):
                candidates.append(container.get(key))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _num_classes(config: Mapping[str, Any], tag_count: int) -> int | None:
    for container in (config, config.get("model"), config.get("model_args")):
        if not isinstance(container, Mapping):
            continue
        for key in ("num_classes", "num_labels", "class_count", "n_classes"):
            raw_value = container.get(key)
            if raw_value is None:
                continue
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
    return tag_count or None


def _tag_from_item(item: Any, index: int) -> tuple[str, str] | None:
    if isinstance(item, str):
        text = item.strip()
        return (text, normalize_category("", text)) if text else None
    if isinstance(item, Mapping):
        text = str(
            item.get("name")
            or item.get("tag")
            or item.get("label")
            or item.get("text")
            or ""
        ).strip()
        if not text:
            return None
        return text, normalize_category(item.get("category") or item.get("type"), text)
    return None


def _load_tags_json(path: Path) -> tuple[list[str], dict[str, str]]:
    value = _read_json(path)
    if isinstance(value, Mapping):
        for key in ("tags", "labels", "classes", "tag_names"):
            if isinstance(value.get(key), (list, tuple)):
                value = value[key]
                break
        else:
            # ``{"tag": "category"}`` and ``{"0": "tag"}`` are both
            # common. Numeric keys indicate index-to-label.
            if all(str(key).isdigit() for key in value):
                value = [entry for _, entry in sorted(value.items(), key=lambda item: int(item[0]))]
            else:
                mapping_tags = [str(key).strip() for key in value if str(key).strip()]
                mapping_categories = {
                    tag: normalize_category(value.get(tag), tag)
                    for tag in mapping_tags
                }
                return mapping_tags, mapping_categories
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return [], {}
    tags: list[str] = []
    categories: dict[str, str] = {}
    for index, item in enumerate(value):
        parsed = _tag_from_item(item, index)
        if parsed is None:
            continue
        tag, category = parsed
        tags.append(tag)
        categories[tag] = category
    return tags, categories


def _load_tags_csv(path: Path) -> tuple[list[str], dict[str, str], dict[str, list[float]]]:
    tags: list[str] = []
    categories: dict[str, str] = {}
    threshold_values: dict[str, list[float]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = {str(field).casefold(): field for field in reader.fieldnames or []}
            name_key = next((fields[key] for key in ("name", "tag", "label", "tag_name") if key in fields), None)
            category_key = next((fields[key] for key in ("category", "type", "category_id") if key in fields), None)
            threshold_key = next((fields[key] for key in ("best_threshold", "threshold") if key in fields), None)
            if not name_key:
                return [], {}, {}
            for row in reader:
                tag = str(row.get(name_key) or "").strip()
                if not tag:
                    continue
                category = normalize_category(row.get(category_key) if category_key else "", tag)
                tags.append(tag)
                categories[tag] = category
                if threshold_key:
                    try:
                        value = _threshold(row.get(threshold_key))
                    except (TypeError, ValueError):
                        continue
                    threshold_values.setdefault(category, []).append(value)
    except (OSError, UnicodeError, csv.Error):
        return [], {}, {}
    return tags, categories, threshold_values


def _load_tags_text(path: Path) -> tuple[list[str], dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return [], {}
    tags = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return tags, {tag: normalize_category("", tag) for tag in tags}


def load_tag_metadata(model_dir: str | os.PathLike[str]) -> tuple[tuple[str, ...], Mapping[str, str], dict[str, list[float]]]:
    root = Path(model_dir)
    threshold_values: dict[str, list[float]] = {}
    for directory in (root, root.parent):
        for filename in (
            "selected_tags.csv",
            "tags.csv",
            "wd14_tags.csv",
            "tags.json",
            "labels.txt",
            "classes.txt",
        ):
            path = directory / filename
            if not path.is_file():
                continue
            if path.suffix.casefold() == ".json":
                tags, categories = _load_tags_json(path)
            elif path.suffix.casefold() == ".csv":
                tags, categories, threshold_values = _load_tags_csv(path)
            else:
                tags, categories = _load_tags_text(path)
            if tags:
                # Keep the first occurrence's category while removing duplicate
                # labels that can otherwise shift output-index alignment.
                unique: list[str] = []
                seen: set[str] = set()
                for tag in tags:
                    marker = tag.casefold()
                    if marker in seen:
                        continue
                    seen.add(marker)
                    unique.append(tag)
                return tuple(unique), MappingProxyType({tag: categories.get(tag, "general") for tag in unique}), threshold_values
    return (), MappingProxyType({}), threshold_values


def load_thresholds(model_dir: str | os.PathLike[str], inferred: Mapping[str, Sequence[float]] | None = None) -> Mapping[str, float]:
    values = dict(DEFAULT_THRESHOLDS)
    root = Path(model_dir)
    raw = _read_json(root / "thresholds.json")
    if isinstance(raw, Mapping) and raw:
        for key, value in raw.items():
            try:
                values[str(key)] = _threshold(value)
            except (TypeError, ValueError):
                continue
        if not any(str(key).casefold() == "default" for key in raw):
            values["default"] = values.get("general", DEFAULT_THRESHOLDS["default"])
    elif csv_values := _load_category_thresholds_csv(root / "thresholds.csv"):
        values.update(csv_values)
        values["default"] = values.get("general", DEFAULT_THRESHOLDS["default"])
    elif inferred:
        for category, numbers in inferred.items():
            valid = [float(value) for value in numbers if 0 <= float(value) <= 1]
            if valid:
                values[str(category)] = sum(valid) / len(valid)
        values["default"] = values.get("general", DEFAULT_THRESHOLDS["default"])
    return MappingProxyType(values)


def _load_category_thresholds_csv(path: Path) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = {str(field).casefold(): field for field in reader.fieldnames or []}
            threshold_key = next(
                (fields[key] for key in ("threshold", "best_threshold") if key in fields),
                None,
            )
            name_key = next(
                (fields[key] for key in ("name", "category_name", "category") if key in fields),
                None,
            )
            category_key = fields.get("category")
            if threshold_key is None or name_key is None:
                return {}
            for row in reader:
                name = str(row.get(name_key) or "").strip().casefold()
                category = (
                    name
                    if name in DEFAULT_THRESHOLDS
                    else normalize_category(row.get(category_key) if category_key else name, name)
                )
                try:
                    value = _threshold(row.get(threshold_key))
                except (TypeError, ValueError):
                    continue
                grouped.setdefault(category, []).append(value)
    except (OSError, UnicodeError, csv.Error):
        return {}
    return {
        category: sum(category_values) / len(category_values)
        for category, category_values in grouped.items()
        if category_values
    }


_WEIGHT_PRIORITY = (
    ("model.onnx", ModelBackend.ONNX, False),
    ("model.safetensors", ModelBackend.PYTORCH, False),
    ("pytorch_model.safetensors", ModelBackend.PYTORCH, False),
    ("model.pt", ModelBackend.PYTORCH, True),
    ("pytorch_model.bin", ModelBackend.PYTORCH, True),
    ("checkpoint.pt", ModelBackend.PYTORCH, True),
    ("checkpoint.pth", ModelBackend.PYTORCH, True),
)


def detect_weight(
    model_dir: str | os.PathLike[str], backend: ModelBackend | str | None = None
) -> tuple[Path, ModelBackend, bool]:
    root = Path(model_dir)
    requested = ModelBackend(backend) if backend and str(backend) != "auto" else None
    for filename, candidate_backend, unsafe in _WEIGHT_PRIORITY:
        if requested is not None and candidate_backend is not requested:
            continue
        candidate = root / filename
        if candidate.is_file():
            return candidate, candidate_backend, unsafe
    # Permit explicitly named standalone model files but never confuse adapter
    # safetensors with a base model during directory discovery.
    if root.is_file():
        suffix = root.suffix.casefold()
        if suffix == ".onnx":
            return root, ModelBackend.ONNX, False
        if suffix == ".safetensors":
            return root, ModelBackend.PYTORCH, False
        if suffix in {".pt", ".pth", ".bin", ".ckpt"}:
            return root, ModelBackend.PYTORCH, True
    if root.is_dir():
        # Hugging Face checkpoints are often sharded or use a project-specific
        # filename.  Select the largest plausible base weight as a fallback,
        # while excluding adapter/classifier-only artifacts.
        safe = [
            path
            for path in root.glob("*.safetensors")
            if not any(token in path.name.casefold() for token in ("adapter", "lora", "lokr", "head"))
        ]
        if safe:
            return max(safe, key=lambda path: path.stat().st_size), ModelBackend.PYTORCH, False
        unsafe_candidates = [
            path
            for path in root.glob("*")
            if path.is_file()
            and path.suffix.casefold() in {".pt", ".pth", ".bin", ".ckpt"}
            and not any(token in path.name.casefold() for token in ("adapter", "lora", "lokr", "classifier"))
        ]
        if unsafe_candidates:
            return max(unsafe_candidates, key=lambda path: path.stat().st_size), ModelBackend.PYTORCH, True
    raise ModelRegistryError(f"no supported model weights found in {root}")


class ModelRegistry:
    def __init__(
        self,
        model_roots: Iterable[str | os.PathLike[str]] | None = None,
        *,
        allowlist: PathAllowlist | None = None,
    ):
        self.allowlist = allowlist
        self._roots: list[Path] = []
        self._models: dict[str, ModelRecord] = {}
        self._path_index: dict[tuple[Path, ModelBackend], str] = {}
        self._lock = threading.RLock()
        for path in model_roots or ():
            self.add_root(path)

    def add_root(self, path: str | os.PathLike[str]) -> Path:
        root = Path(path).expanduser().resolve(strict=False)
        if self.allowlist is not None:
            root = self.allowlist.assert_allowed(root, expect="dir")
        if not root.is_dir():
            raise ModelRegistryError(f"model root does not exist: {root}")
        with self._lock:
            if root not in self._roots:
                self._roots.append(root)
        return root

    def _assert_model_path(self, path: Path) -> Path:
        canonical = path.resolve(strict=False)
        if self.allowlist is not None:
            return self.allowlist.assert_allowed(canonical)
        if self._roots and not any(_is_relative_to(canonical, root) for root in self._roots):
            raise PathNotAllowedError("model path is outside registered model roots")
        return canonical

    def register(
        self,
        path: str | os.PathLike[str],
        *,
        backend: ModelBackend | str | None = None,
        name: str | None = None,
        trusted: bool = False,
    ) -> ModelRecord:
        supplied = self._assert_model_path(Path(path).expanduser())
        model_dir = supplied.parent if supplied.is_file() else supplied
        weight, detected_backend, unsafe = detect_weight(supplied if supplied.is_file() else model_dir, backend)
        model_dir = weight.parent
        key = (model_dir.resolve(strict=False), detected_backend)
        with self._lock:
            existing_id = self._path_index.get(key)
            if existing_id:
                existing = self._models[existing_id]
                if trusted and not existing.trusted:
                    existing.trusted = True
                return existing

        config = _read_json(model_dir / "config.json")
        if not isinstance(config, Mapping):
            config = {}
        tags, categories, inferred = load_tag_metadata(model_dir)
        thresholds = load_thresholds(model_dir, inferred)
        preprocess = load_preprocess_profile(model_dir)
        architecture = _architecture(config)
        identifier = opaque_id(f"{model_dir.resolve(strict=False)}::{detected_backend.value}", prefix="model")
        adapters: list[str] = []
        if (model_dir / "adapter_config.json").is_file() or any(model_dir.glob("*lora*.safetensors")):
            adapters.append("lora")
        if any(model_dir.glob("*lokr*.safetensors")):
            adapters.append("lokr")
        classifier = config.get("classifier") if isinstance(config.get("classifier"), Mapping) else None
        record = ModelRecord(
            model_id=identifier,
            name=str(name or config.get("display_name") or config.get("name") or model_dir.name).strip(),
            path=model_dir,
            weight_path=weight,
            backend=detected_backend,
            architecture=architecture,
            input_size=preprocess.input_size,
            num_classes=_num_classes(config, len(tags)),
            tags=tags,
            categories=categories,
            thresholds=thresholds,
            preset_thresholds=thresholds,
            preprocess=preprocess,
            unsafe_weights=unsafe,
            trusted=bool(trusted),
            adapter_types=tuple(adapters),
            classifier=MappingProxyType(dict(classifier)) if classifier else None,
            metadata=MappingProxyType(dict(config)),
        )
        with self._lock:
            self._models[identifier] = record
            self._path_index[key] = identifier
        return record

    register_model = register
    register_directory = register

    def discover(self, *, recursive: bool = True) -> list[ModelRecord]:
        found: List[ModelRecord] = []
        visited: set[Path] = set()
        with self._lock:
            roots = list(self._roots)
        for root in roots:
            candidates: Iterator[Path]
            if recursive:
                names = {item[0] for item in _WEIGHT_PRIORITY}
                directories = {
                    path.parent
                    for path in root.rglob("*")
                    if path.is_file() and path.name in names
                }
                candidates = iter(sorted(directories, key=lambda path: str(path).casefold()))
            else:
                candidates = iter([root, *(path for path in root.iterdir() if path.is_dir())])
            for directory in candidates:
                canonical = directory.resolve(strict=False)
                if canonical in visited:
                    continue
                visited.add(canonical)
                try:
                    found.append(self.register(canonical))
                except ModelRegistryError:
                    continue
        return found

    scan = discover
    discover_models = discover

    def get(self, model_id: str) -> ModelRecord:
        with self._lock:
            record = self._models.get(model_id)
        if record is None:
            raise UnknownModelError(model_id)
        return record

    resolve = get
    get_model = get

    def list(self) -> List[ModelRecord]:
        with self._lock:
            return list(self._models.values())

    def list_public(self) -> List[ModelPublic]:
        return [record.public() for record in self.list()]

    @property
    def models(self) -> List[ModelRecord]:
        return self.list()

    def mark_loaded(self, model_id: str, loaded: bool, error: str | None = None) -> None:
        record = self.get(model_id)
        with self._lock:
            record.loaded = loaded
            record.load_error = error

    def trust(self, model_id: str, trusted: bool = True) -> None:
        with self._lock:
            self.get(model_id).trusted = trusted

    def remove(self, model_id: str) -> None:
        with self._lock:
            record = self._models.pop(model_id, None)
            if record is not None:
                self._path_index.pop((record.path.resolve(strict=False), record.backend), None)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "DEFAULT_THRESHOLDS",
    "ModelRegistryError",
    "UnknownModelError",
    "ModelBackend",
    "ModelRecord",
    "normalize_category",
    "load_tag_metadata",
    "load_thresholds",
    "detect_weight",
    "ModelRegistry",
]
