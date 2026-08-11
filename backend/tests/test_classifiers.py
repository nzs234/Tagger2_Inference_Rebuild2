from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from tagger2.classifiers import AestheticClassifier, ClassifierConfig


class FakeBackend:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0
        self.unloads = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def classify_batch(self, images, *, batch_size):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            self.calls += 1
            return [{"aesthetic": {"token": f"score_{index + 1}", "score": index + 1}} for index, _ in enumerate(images)]
        finally:
            with self.lock:
                self.active -= 1

    def unload(self):
        self.unloads += 1


def _config(tmp_path: Path) -> ClassifierConfig:
    return ClassifierConfig(project_dir=tmp_path, device="cpu")


def test_aesthetic_classifier_is_lazy_and_returns_lse14_result(tmp_path: Path) -> None:
    loads = 0
    backend = FakeBackend()

    def factory(_config):
        nonlocal loads
        loads += 1
        return backend

    service = AestheticClassifier(_config(tmp_path), backend_factory=factory)
    assert loads == 0
    result = service.classify(Image.new("RGB", (8, 8)), [])
    assert result["aesthetic"]["token"] == "score_1"
    assert loads == 1
    assert service.status()["aesthetic"]["backend"] == "lse14_fusion_1k"


def test_classifier_serializes_concurrent_calls_and_unloads(tmp_path: Path) -> None:
    backend = FakeBackend(delay=0.02)
    service = AestheticClassifier(
        _config(tmp_path), backend_factory=lambda _: backend
    )
    image = Image.new("RGB", (8, 8))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.classify(image, []), range(6)))
    assert all("aesthetic" in result for result in results)
    assert backend.max_active == 1
    service.unload()
    assert backend.unloads == 1
    assert service.status()["aesthetic"]["loaded"] is False


def test_classifier_failure_is_sanitized_and_cached(tmp_path: Path) -> None:
    calls = 0

    def failed(_config):
        nonlocal calls
        calls += 1
        raise RuntimeError("secret path C:/private/model.safetensors")

    service = AestheticClassifier(_config(tmp_path), backend_factory=failed)
    first = service.classify(Image.new("RGB", (8, 8)), [])
    second = service.classify(Image.new("RGB", (8, 8)), [])
    assert first["errors"][0]["classifier"] == "aesthetic"
    assert first["errors"][0]["code"] == "aesthetic_load_failed"
    assert "private" not in repr(first)
    assert second["errors"] == first["errors"]
    assert calls == 1


def test_classifier_config_rejects_invalid_device(tmp_path: Path) -> None:
    try:
        ClassifierConfig(project_dir=tmp_path, device="mps")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid device was accepted")
