"""Integration tests for the OCR stage inside the offline pipeline."""

import json
from pathlib import Path

from PIL import Image


def _config(**overrides):
    from tagger2.workflow.contracts import WorkflowJobConfigV1

    payload = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": True, "min_confidence": 0.5},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    }
    payload.update(overrides)
    return WorkflowJobConfigV1.from_payload(payload)


def _dataset(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (64, 64), color="white").save(source / "test.png")
    (source / "test.txt").write_text("solo, long_hair", encoding="utf-8")

    output = tmp_path / "output"
    output.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return source, output, workspace


class _StubEngine:
    """OCR engine double so the stage is testable without PaddleOCR."""

    def __init__(self, regions=None, error: Exception | None = None):
        self.regions = regions if regions is not None else []
        self.error = error
        self.calls: list[tuple[Path, float]] = []

    def recognize_text(self, image_path: Path, min_confidence: float = 0.5):
        self.calls.append((image_path, min_confidence))
        if self.error is not None:
            raise self.error
        return self.regions


def test_ocr_stage_writes_sidecar_and_leaves_annotation_untouched(tmp_path: Path):
    """OCR output goes to a sidecar; the nine-field payload is unaffected."""
    from tagger2.workflow.pipeline import run_offline_pipeline

    source, output, workspace = _dataset(tmp_path)
    engine = _StubEngine(
        [{"text": "SAMPLE", "box": [[0, 0], [10, 0], [10, 5], [0, 5]], "confidence": 0.91}]
    )

    report = run_offline_pipeline(
        _config(),
        source_root=source,
        output_root=output,
        workspace=workspace,
        ocr_engine=engine,
    )

    assert report.failed_samples == 0
    assert report.committed_files == 2  # JSON + image
    assert report.ocr == {"processed": 1, "failed": 0, "regions": 1}
    assert len(engine.calls) == 1
    assert engine.calls[0][1] == 0.5

    sidecar = workspace / "ocr_sidecars" / "test.png.ocr.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["version"] == "v1"
    assert payload["region_count"] == 1

    # The exported annotation must not gain an OCR field.
    exported = json.loads((output / "test.json").read_text(encoding="utf-8"))
    assert set(exported) == {
        "quality",
        "count",
        "character",
        "series",
        "artist",
        "appearance",
        "tags",
        "environment",
        "nl",
    }
    assert exported["tags"] == ["solo", "long_hair"]


def test_ocr_stage_is_skipped_when_disabled(tmp_path: Path):
    """A disabled OCR stage neither calls the engine nor creates sidecars."""
    from tagger2.workflow.pipeline import run_offline_pipeline

    source, output, workspace = _dataset(tmp_path)
    engine = _StubEngine()

    report = run_offline_pipeline(
        _config(ocr={"enabled": False}),
        source_root=source,
        output_root=output,
        workspace=workspace,
        ocr_engine=engine,
    )

    assert engine.calls == []
    assert report.ocr == {}
    assert not (workspace / "ocr_sidecars").exists()


def test_ocr_failure_is_a_warning_and_does_not_block_commit(tmp_path: Path):
    """An OCR failure must not fail the sample or prevent the commit."""
    from tagger2.workflow.pipeline import run_offline_pipeline

    source, output, workspace = _dataset(tmp_path)
    engine = _StubEngine(error=RuntimeError("engine exploded"))

    report = run_offline_pipeline(
        _config(),
        source_root=source,
        output_root=output,
        workspace=workspace,
        ocr_engine=engine,
    )

    ocr_issues = [issue for issue in report.issues if issue.module_id == "ocr"]
    assert len(ocr_issues) == 1
    assert ocr_issues[0].code == "ocr_failed"
    assert ocr_issues[0].severity == "warning"
    assert ocr_issues[0].blocking is False

    # Failing OCR is not a failed sample, and the dataset is still committed.
    assert report.failed_samples == 0
    assert report.committed_files == 2  # JSON + image
    assert (output / "test.json").exists()
    assert report.ocr == {"processed": 0, "failed": 1, "regions": 0}
