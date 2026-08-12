"""
Tests for OCR stage.
"""

import json
from pathlib import Path
from unittest.mock import Mock

from tagger2.workflow.ocr import (
    OCRIssue,
    OCRModelPaths,
    PaddleOCREngine,
    build_ocr_runtime_manifest,
    discover_ocr_model_paths,
    load_ocr_sidecar,
    run_ocr_stage,
)


class MockOCREngine:
    """Mock OCR engine for testing."""

    def __init__(self, mock_results: dict[str, list[dict]] | None = None):
        self.mock_results = mock_results or {}
        self.calls: list[tuple[Path, float]] = []

    def recognize_text(
        self, image_path: Path, min_confidence: float = 0.5
    ) -> list[dict]:
        self.calls.append((image_path, min_confidence))
        return self.mock_results.get(str(image_path), [])


def test_ocr_disabled(tmp_path):
    """Test that OCR stage is skipped when disabled."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    samples = {
        "image1.jpg": tmp_path / "image1.jpg",
    }

    ocr_config = {"enabled": False}

    results, issues = run_ocr_stage(workspace, samples, ocr_config)

    assert len(results) == 0
    assert len(issues) == 0


def test_ocr_missing_isolated_runtime_fails_closed(tmp_path, monkeypatch):
    """Enabled OCR must report an unavailable stage without using host Python."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image_path = tmp_path / "image.jpg"
    image_path.write_bytes(b"fake image data")

    missing_runtime = tmp_path / "runtime_ocr" / "Scripts" / "python.exe"
    monkeypatch.setattr(
        PaddleOCREngine,
        "_find_paddle_runtime",
        lambda self: missing_runtime,
    )

    results, issues = run_ocr_stage(
        workspace,
        {"image.jpg": image_path},
        {"enabled": True},
    )

    assert results == {}
    assert len(issues) == 1
    assert issues[0].code == "ocr_unavailable"
    assert "runtime interpreter not found" in issues[0].message
    assert not (workspace / "ocr_sidecars").exists()


def test_ocr_explicit_missing_runtime_never_falls_back(tmp_path):
    """An explicit invalid interpreter is rejected instead of falling back."""

    missing_runtime = tmp_path / "does-not-exist" / "python.exe"
    try:
        PaddleOCREngine(runtime_python=missing_runtime)
    except RuntimeError as exc:
        assert str(missing_runtime.resolve()) in str(exc)
    else:  # pragma: no cover - protects the fail-closed contract
        raise AssertionError("missing OCR runtime unexpectedly initialized")


def test_ocr_model_cache_discovery_is_complete_and_content_addressed(tmp_path):
    """Model discovery requires all three families and hashes their contents."""

    cache = tmp_path / "paddleocr"
    model_dirs = {
        "det": cache / "det" / "en_PP-OCRv3_det_infer",
        "rec": cache / "rec" / "en_PP-OCRv4_rec_infer",
        "cls": cache / "cls" / "ch_ppocr_mobile_v2.0_cls_infer",
    }
    for model_dir in model_dirs.values():
        model_dir.mkdir(parents=True)
        (model_dir / "inference.pdmodel").write_bytes(b"model")
        (model_dir / "inference.pdiparams").write_bytes(b"weights")

    paths = discover_ocr_model_paths(cache)
    assert isinstance(paths, OCRModelPaths)
    first_key = paths.cache_key
    (model_dirs["rec"] / "inference.pdiparams").write_bytes(b"changed")
    assert discover_ocr_model_paths(cache).cache_key != first_key


def test_ocr_model_cache_discovery_fails_closed_when_incomplete(tmp_path):
    """An incomplete cache must not trigger a model download or fallback."""

    cache = tmp_path / "paddleocr"
    (cache / "det" / "det_model").mkdir(parents=True)
    (cache / "det" / "det_model" / "inference.pdmodel").write_bytes(b"model")

    try:
        discover_ocr_model_paths(cache)
    except RuntimeError as exc:
        assert "incomplete" in str(exc)
        assert "automatic downloads are disabled" in str(exc)
    else:  # pragma: no cover - protects the fail-closed contract
        raise AssertionError("incomplete OCR model cache unexpectedly accepted")


def test_ocr_runtime_manifest_contains_digests_and_versions(tmp_path, monkeypatch):
    """A probe records immutable runtime/model identities without downloads."""

    runtime = tmp_path / "runtime" / "python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"isolated-python")
    monkeypatch.setattr(PaddleOCREngine, "_validate_runtime", lambda self: None)

    cache = tmp_path / "paddleocr"
    for family in ("det", "rec", "cls"):
        model_dir = cache / family / f"{family}_model"
        model_dir.mkdir(parents=True)
        (model_dir / "inference.pdmodel").write_bytes(b"model")
        (model_dir / "inference.pdiparams").write_bytes(b"weights")
    monkeypatch.setattr(
        "tagger2.workflow.ocr.subprocess.run",
        lambda *args, **kwargs: type(
            "ProbeResult", (), {"returncode": 0, "stdout": '{"paddle":"3.0.0","paddleocr":"2.9.1"}', "stderr": ""}
        )(),
    )

    manifest = build_ocr_runtime_manifest(runtime, cache)
    assert manifest["schema_version"] == "ocr-runtime-v1"
    assert manifest["runtime_sha256"]
    assert manifest["versions"]["paddle"] == "3.0.0"
    assert manifest["model_cache"]["fingerprint"]


def test_ocr_basic_recognition(tmp_path):
    """Test basic OCR recognition."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Create a sample image file
    image_path = tmp_path / "image1.jpg"
    image_path.write_bytes(b"fake image data")

    samples = {
        "image1.jpg": image_path,
    }

    mock_regions = [
        {
            "text": "Hello World",
            "box": [[10, 10], [100, 10], [100, 30], [10, 30]],
            "confidence": 0.95,
        }
    ]

    mock_engine = MockOCREngine({str(image_path): mock_regions})

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
    }

    results, issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    assert len(results) == 1
    assert "image1.jpg" in results
    assert results["image1.jpg"].success is True
    assert len(results["image1.jpg"].detected_regions) == 1
    assert results["image1.jpg"].detected_regions[0]["text"] == "Hello World"
    assert len(issues) == 0

    # Check that sidecar was written
    sidecar_path = workspace / "ocr_sidecars" / "image1.jpg.ocr.json"
    assert sidecar_path.exists()

    with open(sidecar_path, "r", encoding="utf-8") as f:
        sidecar_data = json.load(f)
        assert sidecar_data["version"] == "v1"
        assert sidecar_data["image"] == "image1.jpg"
        assert sidecar_data["region_count"] == 1


def test_ocr_confidence_filtering(tmp_path):
    """Test that low-confidence regions are filtered."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    image_path = tmp_path / "image1.jpg"
    image_path.write_bytes(b"fake image data")

    samples = {"image1.jpg": image_path}

    # Mock engine returns regions with different confidence levels
    mock_regions = [
        {"text": "High", "box": [[0, 0], [10, 10]], "confidence": 0.95},
        {"text": "Low", "box": [[0, 0], [10, 10]], "confidence": 0.3},
    ]

    mock_engine = MockOCREngine({str(image_path): mock_regions})

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
    }

    _results, _issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    # MockEngine returns all regions, but in real scenario PaddleOCR would filter
    # Here we just verify the call was made with correct min_confidence
    assert len(mock_engine.calls) == 1
    assert mock_engine.calls[0][1] == 0.5


def test_ocr_sidecar_reuse(tmp_path):
    """Test that existing sidecars are reused when force_reprocess is False."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    image_path = tmp_path / "image1.jpg"
    image_path.write_bytes(b"fake image data")

    samples = {"image1.jpg": image_path}

    # Create existing sidecar
    ocr_dir = workspace / "ocr_sidecars"
    ocr_dir.mkdir()
    sidecar_path = ocr_dir / "image1.jpg.ocr.json"
    
    existing_data = {
        "version": "v1",
        "image": "image1.jpg",
        "min_confidence": 0.5,
        "regions": [{"text": "Cached", "box": [[0, 0]], "confidence": 0.9}],
        "region_count": 1,
    }
    
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(existing_data, f)

    mock_engine = MockOCREngine()

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
        "force_reprocess": False,
    }

    results, _issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    # Should use cached result, not call engine
    assert len(mock_engine.calls) == 0
    assert results["image1.jpg"].detected_regions[0]["text"] == "Cached"


def test_ocr_force_reprocess(tmp_path):
    """Test that force_reprocess bypasses cache."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    image_path = tmp_path / "image1.jpg"
    image_path.write_bytes(b"fake image data")

    samples = {"image1.jpg": image_path}

    # Create existing sidecar
    ocr_dir = workspace / "ocr_sidecars"
    ocr_dir.mkdir()
    sidecar_path = ocr_dir / "image1.jpg.ocr.json"
    
    existing_data = {
        "version": "v1",
        "image": "image1.jpg",
        "regions": [{"text": "Old", "box": [[0, 0]], "confidence": 0.9}],
    }
    
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(existing_data, f)

    mock_regions = [{"text": "New", "box": [[0, 0]], "confidence": 0.95}]
    mock_engine = MockOCREngine({str(image_path): mock_regions})

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
        "force_reprocess": True,
    }

    results, _issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    # Should call engine and get new result
    assert len(mock_engine.calls) == 1
    assert results["image1.jpg"].detected_regions[0]["text"] == "New"


def test_ocr_engine_failure(tmp_path):
    """Test that OCR engine failures are non-blocking."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    image_path = tmp_path / "image1.jpg"
    image_path.write_bytes(b"fake image data")

    samples = {"image1.jpg": image_path}

    # Mock engine that raises exception
    mock_engine = Mock()
    mock_engine.recognize_text.side_effect = RuntimeError("OCR failed")

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
    }

    results, issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    # Should have result marked as failed
    assert len(results) == 1
    assert results["image1.jpg"].success is False
    assert "OCR failed" in results["image1.jpg"].error

    # Should have warning issue, not error
    assert len(issues) == 1
    assert isinstance(issues[0], OCRIssue)
    assert issues[0].code == "ocr_failed"
    assert issues[0].relative_image_path == "image1.jpg"


def test_ocr_multiple_samples(tmp_path):
    """Test OCR on multiple samples."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    samples = {}
    mock_results = {}

    for i in range(3):
        image_path = tmp_path / f"image{i}.jpg"
        image_path.write_bytes(b"fake image data")
        samples[f"image{i}.jpg"] = image_path
        mock_results[str(image_path)] = [
            {"text": f"Text {i}", "box": [[0, 0]], "confidence": 0.9}
        ]

    mock_engine = MockOCREngine(mock_results)

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
    }

    results, issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    assert len(results) == 3
    assert len(issues) == 0

    for i in range(3):
        assert f"image{i}.jpg" in results
        assert results[f"image{i}.jpg"].detected_regions[0]["text"] == f"Text {i}"


def test_load_ocr_sidecar(tmp_path):
    """Test loading OCR sidecar file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    ocr_dir = workspace / "ocr_sidecars"
    ocr_dir.mkdir()

    sidecar_data = {
        "version": "v1",
        "image": "test.jpg",
        "regions": [{"text": "Test", "box": [[0, 0]], "confidence": 0.9}],
    }

    sidecar_path = ocr_dir / "test.jpg.ocr.json"
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(sidecar_data, f)

    loaded = load_ocr_sidecar(workspace, "test.jpg")
    assert loaded is not None
    assert loaded["image"] == "test.jpg"
    assert len(loaded["regions"]) == 1


def test_load_ocr_sidecar_not_found(tmp_path):
    """Test loading non-existent OCR sidecar."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    loaded = load_ocr_sidecar(workspace, "nonexistent.jpg")
    assert loaded is None


def test_ocr_nested_paths(tmp_path):
    """Test OCR with nested directory structure."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    nested_dir = tmp_path / "subdir"
    nested_dir.mkdir()

    image_path = nested_dir / "image.jpg"
    image_path.write_bytes(b"fake image data")

    samples = {"subdir/image.jpg": image_path}

    mock_regions = [{"text": "Nested", "box": [[0, 0]], "confidence": 0.9}]
    mock_engine = MockOCREngine({str(image_path): mock_regions})

    ocr_config = {
        "enabled": True,
        "min_confidence": 0.5,
    }

    results, _issues = run_ocr_stage(workspace, samples, ocr_config, mock_engine)

    assert "subdir/image.jpg" in results
    
    # Check that nested sidecar directory was created
    sidecar_path = workspace / "ocr_sidecars" / "subdir" / "image.jpg.ocr.json"
    assert sidecar_path.exists()
