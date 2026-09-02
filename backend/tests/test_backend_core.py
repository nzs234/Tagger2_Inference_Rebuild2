from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from tagger2.local_inference import (
    AdapterConfig,
    LoadedModel,
    LocalInferenceEngine,
    UnsafeModelError,
    _probabilities,
    merge_predictions,
    threshold_snapshot,
)
from tagger2.model_registry import ModelBackend, ModelRegistry, load_thresholds
from tagger2.preprocessing import load_preprocess_profile, preprocess_image
from tagger2.schemas import AnimaPayload, TagItem, parse_anima_response
from tagger2.secrets import CompositeSecretStore, EnvSecretStore
from tagger2.security import (
    PathAllowlist,
    PathNotAllowedError,
    SecurityError,
    atomic_write_json,
    validate_provider_url,
)


def _model_dir(tmp_path: Path, weight: str = "model.pt") -> Path:
    root = tmp_path / "models"
    model = root / "demo"
    model.mkdir(parents=True)
    (model / weight).write_bytes(b"placeholder")
    (model / "config.json").write_text(
        json.dumps({"architecture": "test", "input_size": [3, 8, 8]}),
        encoding="utf-8",
    )
    (model / "tags.json").write_text(json.dumps(["red", "blue"]), encoding="utf-8")
    return model


def test_allowlist_rejects_absolute_and_parent_paths(tmp_path: Path) -> None:
    allowlist = PathAllowlist()
    root = allowlist.register(tmp_path, kind="output", writable=True)

    assert allowlist.resolve(root.root_id, "nested/file.json", for_write=True).parent.name == "nested"
    with pytest.raises(PathNotAllowedError):
        allowlist.resolve(root.root_id, "../outside.json", for_write=True)
    with pytest.raises(PathNotAllowedError):
        allowlist.resolve(root.root_id, str(tmp_path / "absolute.json"), for_write=True)


def test_allowlist_rejects_symlink_escape_and_atomic_write(tmp_path: Path) -> None:
    root_path = tmp_path / "root"
    outside = tmp_path / "outside"
    root_path.mkdir()
    outside.mkdir()
    link = root_path / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    allowlist = PathAllowlist()
    root = allowlist.register(root_path, kind="output", writable=True)
    with pytest.raises(PathNotAllowedError):
        allowlist.resolve(root.root_id, "link/result.json", for_write=True)

    target = allowlist.resolve(root.root_id, "result.json", for_write=True)
    atomic_write_json(target, {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert not list(root_path.glob("*.tmp"))


def test_provider_url_requires_explicit_local_enablement(monkeypatch) -> None:
    with pytest.raises(SecurityError):
        validate_provider_url("http://127.0.0.1:1234/v1")
    assert validate_provider_url(
        "http://127.0.0.1:1234/v1", allow_local=True
    ) == "http://127.0.0.1:1234/v1"
    with pytest.raises(SecurityError):
        validate_provider_url("file:///tmp/model")
    with pytest.raises(SecurityError):
        validate_provider_url("http://localhost.:1234/v1")
    with pytest.raises(SecurityError):
        validate_provider_url("https://example.com/v1?api_key=do-not-store")
    with pytest.raises(SecurityError):
        validate_provider_url("http://0x7f000001/v1")

    monkeypatch.setattr(
        "tagger2.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(SecurityError):
        validate_provider_url("https://provider.example/v1", resolve_dns=True)
    assert validate_provider_url(
        "https://provider.example/v1",
        allow_local=True,
        resolve_dns=True,
    ) == "https://provider.example/v1"


def test_environment_secret_metadata_never_returns_secret() -> None:
    environment = EnvSecretStore(
        {"TAGGER2_SECRET_DEMO": "first-secret,second-secret"}
    )
    store = CompositeSecretStore(environment=environment)

    assert store.get_many("demo") == ["first-secret", "second-secret"]
    metadata = store.metadata("demo").as_dict()
    assert metadata == {
        "configured": True,
        "source": "environment",
        "key_suffix": "cret",
        "count": 2,
    }
    assert "first-secret" not in repr(metadata)


def test_anima_schema_is_shared_and_strict() -> None:
    raw = {
        "quality": ["best quality", "best quality"],
        "count": "solo",
        "character": "",
        "series": "",
        "artist": "",
        "appearance": ["white fur"],
        "tags": ["white fur", "standing"],
        "environment": ["outdoors"],
        "nl": "A character standing outdoors.",
    }
    parsed = parse_anima_response(json.dumps(raw), trigger_artist="test_artist")

    assert parsed["quality"] == ["best quality"]
    assert parsed["appearance"] == ["white fur"]
    assert parsed["tags"] == ["standing"]
    assert AnimaPayload.model_validate(parsed).artist == "test_artist"
    with pytest.raises(Exception):
        AnimaPayload.model_validate({**parsed, "metadata": {}})


def test_preprocess_profile_reads_steps_and_normalization(tmp_path: Path) -> None:
    model = tmp_path / "model"
    model.mkdir()
    (model / "preprocess.json").write_text(
        json.dumps(
            {
                "input_size": [3, 6, 8],
                "mean": [0.0, 0.0, 0.0],
                "std": [1.0, 1.0, 1.0],
                "transforms": [
                    {"type": "PadToSize", "size": [6, 8], "fill": [0, 0, 0]},
                    {"type": "Resize", "size": [6, 8]},
                ],
            }
        ),
        encoding="utf-8",
    )
    profile = load_preprocess_profile(model)
    tensor = preprocess_image(Image.new("RGB", (2, 4), (255, 0, 0)), profile, as_numpy=True)

    assert profile.input_size == (6, 8)
    assert [step.kind for step in profile.steps] == ["pad", "resize"]
    assert tensor.shape == (3, 6, 8)


def test_registry_has_opaque_id_and_refuses_untrusted_pickle(tmp_path: Path) -> None:
    model = _model_dir(tmp_path)
    registry = ModelRegistry([model.parent])
    record = registry.register(model)

    assert record.model_id.startswith("model_")
    assert str(model) not in record.model_id
    assert record.public().model_dump().get("path") is None
    assert record.unsafe_weights is True
    with pytest.raises(UnsafeModelError):
        LocalInferenceEngine(registry, device="cpu").load(record.model_id)


def test_unsafe_pickle_fallback_refuses_non_pickle_errors(tmp_path: Path, monkeypatch) -> None:
    torch = pytest.importorskip("torch")
    model = _model_dir(tmp_path)
    registry = ModelRegistry([model.parent])
    record = registry.register(model, trusted=True)
    engine = LocalInferenceEngine(registry, device="cpu")
    calls: list[bool] = []

    def fake_load(_path, *, map_location=None, weights_only=True, **_kwargs):
        del map_location
        calls.append(weights_only)
        if weights_only:
            raise OSError("disk full")
        raise AssertionError("weights_only=False must not be attempted for non-pickle errors")

    monkeypatch.setattr(torch, "load", fake_load)

    with pytest.raises(OSError, match="disk full"):
        engine._load_pytorch(record, unsafe_allowed=True)
    assert calls == [True]


def test_unsafe_pickle_fallback_follows_weights_only_rejection(tmp_path: Path, monkeypatch) -> None:
    torch = pytest.importorskip("torch")
    model = _model_dir(tmp_path)
    registry = ModelRegistry([model.parent])
    record = registry.register(model, trusted=True)
    engine = LocalInferenceEngine(registry, device="cpu")
    calls: list[bool] = []
    fallback_model = torch.nn.Module()

    def fake_load(_path, *, map_location=None, weights_only=True, **_kwargs):
        del map_location
        calls.append(weights_only)
        if weights_only:
            raise RuntimeError(
                "Weights only load failed. In PyTorch 2.6, we changed the default value of the "
                "`weights_only` argument in `torch.load` from `False` to `True`. "
                "WeightsUnpickler error: Unsupported global GLOBAL os.system"
            )
        return fallback_model

    monkeypatch.setattr(torch, "load", fake_load)

    loaded, _ = engine._load_pytorch(record, unsafe_allowed=True)
    assert calls == [True, False]
    assert loaded is fallback_model


def test_threshold_snapshot_and_merge_do_not_mutate_model(tmp_path: Path) -> None:
    record = ModelRegistry([_model_dir(tmp_path).parent]).discover()[0]
    original = dict(record.thresholds)

    snapshot = threshold_snapshot(record, threshold=0.6, category_thresholds={"character": 0.7})
    assert snapshot["default"] == 0.6
    assert snapshot["character"] == 0.7
    assert dict(record.thresholds) == original

    per_model = threshold_snapshot(
        record,
        category_thresholds={record.model_id: 0.42, "another_model": 0.9},
    )
    assert per_model["default"] == 0.42
    assert dict(record.thresholds) == original

    merged = merge_predictions(
        [
            [TagItem(text="white_fur", score=0.7, model_id="one")],
            [TagItem(text="White Fur", score=0.9, model_id="two")],
        ]
    )
    assert len(merged) == 1
    assert merged[0].score == 0.9
    assert merged[0].model_id == "one,two"


def test_category_threshold_csv_takes_priority_over_per_tag_average(tmp_path: Path) -> None:
    model = _model_dir(tmp_path)
    (model / "thresholds.csv").write_text(
        "category,name,threshold\n0,general,0.38\n4,character,0.51\n9,rating,0.24\n",
        encoding="utf-8",
    )

    thresholds = load_thresholds(model, {"general": [0.1, 0.2]})

    assert thresholds["general"] == pytest.approx(0.38)
    assert thresholds["character"] == pytest.approx(0.51)
    assert thresholds["rating"] == pytest.approx(0.24)
    assert thresholds["default"] == pytest.approx(0.38)


def test_cuda_amp_nonfinite_output_retries_and_stays_on_fp32(monkeypatch) -> None:
    amp_state = {"enabled": False}

    @contextmanager
    def inference_mode():
        yield

    @contextmanager
    def autocast(*, device_type, dtype):
        del device_type, dtype
        amp_state["enabled"] = True
        try:
            yield
        finally:
            amp_state["enabled"] = False

    fake_torch = SimpleNamespace(
        inference_mode=inference_mode,
        autocast=autocast,
        float16=object(),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    class Batch:
        def to(self, *_args, **_kwargs):
            return self

        def float(self):
            return self

    class Runtime:
        def __init__(self):
            self.calls = 0

        def __call__(self, _batch):
            self.calls += 1
            value = np.nan if amp_state["enabled"] else 2.0
            return np.asarray([[value]], dtype=np.float32)

    runtime = Runtime()
    loaded = SimpleNamespace(
        record=SimpleNamespace(backend=ModelBackend.PYTORCH),
        runtime=runtime,
        device="cuda",
        lock=threading.RLock(),
        last_used=0.0,
        amp_enabled=True,
    )
    engine = object.__new__(LocalInferenceEngine)
    engine._device_lock = threading.RLock()

    first = engine._run(loaded, Batch())
    second = engine._run(loaded, Batch())

    assert first.tolist() == [[2.0]]
    assert second.tolist() == [[2.0]]
    assert loaded.amp_enabled is False
    assert runtime.calls == 3


def test_injected_torch_model_predicts_without_loading_pickle(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    model = _model_dir(tmp_path)
    registry = ModelRegistry([model.parent])
    record = registry.register(model, trusted=True)

    class FixedModel(torch.nn.Module):  # type: ignore[name-defined,misc]
        def forward(self, inputs):
            row = torch.tensor([4.0, -4.0], device=inputs.device)
            return row.repeat(inputs.shape[0], 1)

    class Classifier:
        def classify(self, image, tags):
            return {"aesthetic": "flat", "tag_count": len(tags)}

    engine = LocalInferenceEngine(
        registry,
        device="cpu",
        model_factory=lambda _: FixedModel(),
    )
    engine.load(record.model_id, classifier=Classifier())
    result = engine.predict_multi_result(
        [record.model_id], Image.new("RGB", (8, 8)), threshold=0.5
    )
    batch = engine.predict_multi_batch(
        [record.model_id],
        [Image.new("RGB", (8, 8)), Image.new("RGB", (8, 8))],
        threshold=0.5,
        batch_size=2,
    )

    assert [tag.text for tag in result.tags] == ["red"]
    assert result.model_tags == {}
    assert result.classifiers[record.model_id] == {"aesthetic": "flat", "tag_count": 1}
    assert [[tag.text for tag in tags] for tags in batch] == [["red"], ["red"]]
    assert engine.unload(record.model_id) is True
    assert record.loaded is False


def test_onnx_input_layout_is_applied_before_session_run(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    model = _model_dir(tmp_path, weight="model.onnx")
    registry = ModelRegistry([model.parent])
    record = registry.register(model)
    seen = {}

    class Session:
        def run(self, outputs, inputs):
            seen["shape"] = inputs["image"].shape
            return [__import__("numpy").zeros((2, 2), dtype="float32")]

    loaded = LoadedModel(
        record=record,
        runtime=Session(),
        input_name="image",
        adapter=AdapterConfig(),
        classifier=None,
        device="cpu",
        input_layout="nhwc",
    )
    engine = LocalInferenceEngine(registry, device="cpu")

    assert engine._run(loaded, torch.zeros((2, 3, 8, 8))).shape == (2, 2)
    assert seen["shape"] == (2, 8, 8, 3)

    probability_output = _probabilities(
        __import__("numpy").array([[0.1, 0.9]], dtype="float32"),
        {},
        backend=record.backend,
    )
    assert probability_output[0].tolist() == pytest.approx([0.1, 0.9])


def test_safe_pytorch_safetensors_smoke(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    timm = pytest.importorskip("timm")
    safetensors = pytest.importorskip("safetensors.torch")
    model_dir = tmp_path / "models" / "safe"
    model_dir.mkdir(parents=True)
    model = timm.create_model("resnet18", pretrained=False, num_classes=2)
    safetensors.save_file(model.state_dict(), model_dir / "model.safetensors")
    (model_dir / "config.json").write_text(
        json.dumps(
            {
                "architecture": "resnet18",
                "num_classes": 2,
                "input_size": [3, 32, 32],
            }
        ),
        encoding="utf-8",
    )
    (model_dir / "tags.json").write_text(json.dumps(["one", "two"]), encoding="utf-8")
    registry = ModelRegistry([model_dir.parent])
    record = registry.register(model_dir)
    engine = LocalInferenceEngine(registry, device="cpu")

    probabilities = engine.predict_raw(record.model_id, Image.new("RGB", (32, 32)))

    assert probabilities.shape == (2,)
    assert ((0.0 <= probabilities) & (probabilities <= 1.0)).all()
    assert engine.unload(record.model_id)


def test_shared_classifier_runs_once_after_multi_model_merge(tmp_path: Path) -> None:
    torch = pytest.importorskip("torch")
    root = tmp_path / "models"
    for name in ("one", "two"):
        model_dir = root / name
        model_dir.mkdir(parents=True)
        (model_dir / "model.pt").write_bytes(b"placeholder")
        (model_dir / "config.json").write_text(
            json.dumps({"architecture": "test", "input_size": [3, 8, 8]}),
            encoding="utf-8",
        )
        (model_dir / "tags.json").write_text(json.dumps(["red", "blue"]), encoding="utf-8")
    registry = ModelRegistry([root])
    records = [registry.register(root / name, trusted=True) for name in ("one", "two")]

    class FixedModel(torch.nn.Module):  # type: ignore[name-defined,misc]
        def forward(self, inputs):
            return torch.tensor([[4.0, -4.0]], device=inputs.device).repeat(inputs.shape[0], 1)

    class BatchClassifier:
        def __init__(self):
            self.calls = 0

        def classify_batch(self, images, tags, *, batch_size):
            self.calls += 1
            return [{"aesthetic": "flat"} for _ in images]

    classifier = BatchClassifier()
    engine = LocalInferenceEngine(
        registry,
        device="cpu",
        model_factory=lambda _: FixedModel(),
        classifier_factory=lambda _: classifier,
        max_loaded_models=2,
    )
    results = engine.predict_multi_batch_results(
        [record.model_id for record in records],
        [Image.new("RGB", (8, 8)), Image.new("RGB", (8, 8))],
        threshold=0.5,
        batch_size=2,
        include_model_tags=True,
    )

    assert classifier.calls == 1
    assert [tag.text for tag in results[0].tags] == ["red"]
    assert set(results[0].classifiers) == {record.model_id for record in records}
    assert set(results[0].model_tags) == {record.model_id for record in records}
    for record in records:
        assert [tag.text for tag in results[0].model_tags[record.model_id]] == ["red"]
