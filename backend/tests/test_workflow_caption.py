"""Tests for the Caption stage adapter over the host inference engine."""

import tempfile
from pathlib import Path

import pytest
from PIL import Image


def _image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path)
    return path


class FakePredictor:
    """Test double implementing the TagPredictor protocol."""

    def __init__(self, tags_by_name=None, fail_on=()):
        self.tags_by_name = tags_by_name or {}
        self.fail_on = set(fail_on)
        self.calls: list[str] = []

    def predict_tags(self, image_path):
        from backend.tagger2.workflow.stages.caption import CaptionTag

        name = Path(image_path).name
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError("model exploded")
        raw = self.tags_by_name.get(name, ("male", "anthro"))
        return [CaptionTag(raw_tag=tag, score=0.9) for tag in raw]


class FakeBatchPredictor:
    """Test double implementing both the per-image and the batch protocol."""

    def __init__(self, tags_by_name=None, fail_on=(), batch_fail_on=()):
        self.tags_by_name = tags_by_name or {}
        self.fail_on = set(fail_on)
        self.batch_fail_on = set(batch_fail_on)
        self.calls: list[str] = []
        self.batch_calls: list[list[str]] = []

    def _tags_for(self, name):
        from backend.tagger2.workflow.stages.caption import CaptionTag

        raw = self.tags_by_name.get(name, ("male", "anthro"))
        return [CaptionTag(raw_tag=tag, score=0.9) for tag in raw]

    def predict_tags(self, image_path):
        name = Path(image_path).name
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError("model exploded")
        return self._tags_for(name)

    def predict_tags_batch(self, image_paths):
        names = [Path(image_path).name for image_path in image_paths]
        self.batch_calls.append(names)
        if self.batch_fail_on.intersection(names):
            raise RuntimeError("batch exploded")
        return [self._tags_for(name) for name in names]


def test_display_tag_applies_frozen_transform():
    """Underscores become spaces and parentheses are escaped."""
    from backend.tagger2.workflow.stages.caption import CaptionDisplaySettings, display_tag

    settings = CaptionDisplaySettings()
    assert display_tag("blue_fur", settings) == "blue fur"
    assert display_tag("holding_cup_(object)", settings) == "holding cup \\(object\\)"

    raw = CaptionDisplaySettings(replace_underscores_with_spaces=False, preserve_escapes=False)
    assert display_tag("blue_fur", raw) == "blue_fur"
    assert display_tag("cup_(object)", raw) == "cup_(object)"


def test_display_tag_rejects_unrepresentable_tag():
    """A tag containing a comma or newline is an error, not silently dropped."""
    from backend.tagger2.workflow.stages.caption import (
        CaptionDisplaySettings,
        CaptionError,
        display_tag,
    )

    settings = CaptionDisplaySettings()
    for bad in ("a,b", "a\nb", "a\rb", " padded", ""):
        with pytest.raises(CaptionError):
            display_tag(bad, settings)


def test_format_caption_includes_triggers_first():
    """Trigger terms are prefixed when enabled and omitted when not."""
    from backend.tagger2.workflow.stages.caption import (
        CaptionDisplaySettings,
        CaptionTag,
        format_caption,
    )

    tags = [CaptionTag("male"), CaptionTag("blue_fur")]

    plain = format_caption(tags, CaptionDisplaySettings())
    assert plain == "male, blue fur"

    triggered = format_caption(
        tags,
        CaptionDisplaySettings(triggers_enabled=True, trigger_terms=("my_style",)),
    )
    assert triggered == "my style, male, blue fur"


def test_format_caption_requires_at_least_one_tag():
    from backend.tagger2.workflow.stages.caption import (
        CaptionDisplaySettings,
        CaptionError,
        format_caption,
    )

    with pytest.raises(CaptionError):
        format_caption([], CaptionDisplaySettings())


def test_caption_stage_skips_samples_with_existing_annotations():
    """Raw e621 JSON and tag TXT are authoritative and are never regenerated."""
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import (
        CaptionDisplaySettings,
        run_caption_stage,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _image(root / "bare.png")
        _image(root / "tagged.png")
        (root / "tagged.txt").write_text("male, anthro", encoding="utf-8")

        imported = import_dataset(root, recursive=False)
        predictor = FakePredictor()
        report = run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=predictor,
            settings=CaptionDisplaySettings(),
            model_id="tagger-test",
        )

        assert report.captioned == 1
        assert report.skipped == 1
        assert report.failed == 0
        # Only the un-annotated image reached the model.
        assert predictor.calls == ["bare.png"]

        by_path = report.by_path()
        assert by_path["bare.png"].txt == "male, anthro"
        assert by_path["tagged.png"].skipped is True
        assert "existing annotation" in by_path["tagged.png"].skip_reason


def test_caption_stage_records_per_sample_failure():
    """One failing image does not abort the stage."""
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import (
        CaptionDisplaySettings,
        run_caption_stage,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _image(root / "good.png")
        _image(root / "bad.png")

        imported = import_dataset(root, recursive=False)
        report = run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=FakePredictor(fail_on={"bad.png"}),
            settings=CaptionDisplaySettings(),
        )

        assert report.captioned == 1
        assert report.failed == 1
        by_path = report.by_path()
        assert by_path["good.png"].ok
        assert "model exploded" in by_path["bad.png"].error


def test_caption_stage_treats_empty_prediction_as_failure():
    """A model returning nothing above threshold is a recorded failure."""
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import (
        CaptionDisplaySettings,
        run_caption_stage,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _image(root / "a.png")
        imported = import_dataset(root, recursive=False)

        report = run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=FakePredictor(tags_by_name={"a.png": ()}),
            settings=CaptionDisplaySettings(),
        )
        assert report.failed == 1
        assert "no tags" in report.results[0].error


def test_engine_predictor_delegates_to_host_engine():
    """The adapter calls the host engine and preserves model tag spelling."""
    from backend.tagger2.workflow.stages.caption import EngineTagPredictor, TagPredictor

    class Item:
        def __init__(self, text, score, category):
            self.text = text
            self.score = score
            self.category = category

    class FakeEngine:
        def __init__(self):
            self.seen: list[dict] = []

        def predict(self, model_id, image, **kwargs):
            self.seen.append({"model_id": model_id, "image": image, **kwargs})
            return [Item("blue_fur", 0.8, "general"), Item("rex", 0.95, "character")]

    engine = FakeEngine()
    predictor = EngineTagPredictor(engine, "eva02-large", threshold=None)
    assert isinstance(predictor, TagPredictor)

    tags = predictor.predict_tags(Path("a.png"))
    assert [tag.raw_tag for tag in tags] == ["blue_fur", "rex"]
    assert tags[1].category == "character"
    # `threshold=None` preserves the model default (model_default mode).
    assert engine.seen[0]["model_id"] == "eva02-large"
    assert engine.seen[0]["threshold"] is None


def test_caption_stage_batches_pending_samples_in_order():
    """A batch-capable predictor receives chunks and results keep sample order."""
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import (
        DEFAULT_CAPTION_BATCH_SIZE,
        CaptionDisplaySettings,
        run_caption_stage,
    )

    assert DEFAULT_CAPTION_BATCH_SIZE == 8

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        names = [f"img{index:02d}.png" for index in range(10)]
        for name in names:
            _image(root / name)

        imported = import_dataset(root, recursive=False)
        predictor = FakeBatchPredictor()
        report = run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=predictor,
            settings=CaptionDisplaySettings(),
            model_id="tagger-test",
        )

        assert report.captioned == 10
        assert report.failed == 0
        # Default chunk size: one full chunk of 8 plus the 2-sample remainder.
        assert predictor.batch_calls == [names[:8], names[8:]]
        assert predictor.calls == []
        # One tag list per input path, in input order.
        assert [result.relative_image_path for result in report.results] == names
        assert all(result.ok for result in report.results)


def test_caption_stage_honors_batch_size_override():
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import CaptionDisplaySettings, run_caption_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        names = [f"img{index:02d}.png" for index in range(7)]
        for name in names:
            _image(root / name)

        imported = import_dataset(root, recursive=False)
        predictor = FakeBatchPredictor()
        run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=predictor,
            settings=CaptionDisplaySettings(),
            batch_size=3,
        )

        assert predictor.batch_calls == [names[0:3], names[3:6], names[6:]]


def test_caption_stage_batch_failure_falls_back_to_per_image():
    """A chunk-level batch failure retries images individually, keeping isolation."""
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import CaptionDisplaySettings, run_caption_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for name in ("good1.png", "bad.png", "good2.png"):
            _image(root / name)

        imported = import_dataset(root, recursive=False)
        predictor = FakeBatchPredictor(batch_fail_on={"bad.png"}, fail_on={"bad.png"})
        report = run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=predictor,
            settings=CaptionDisplaySettings(),
        )

        assert report.captioned == 2
        assert report.failed == 1
        # The failing chunk was retried image by image, in chunk order.
        assert len(predictor.batch_calls) == 1
        assert predictor.calls == ["bad.png", "good1.png", "good2.png"]
        by_path = report.by_path()
        assert by_path["good1.png"].ok
        assert by_path["good2.png"].ok
        assert "model exploded" in by_path["bad.png"].error
        # Results keep input (sorted import) order despite the chunk fallback.
        assert [result.relative_image_path for result in report.results] == [
            "bad.png",
            "good1.png",
            "good2.png",
        ]


def test_caption_stage_keeps_skip_positions_between_chunks():
    """Skipped samples stay at their input position around batched results."""
    from backend.tagger2.workflow.dataset_import import import_dataset
    from backend.tagger2.workflow.stages.caption import CaptionDisplaySettings, run_caption_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _image(root / "a.png")
        _image(root / "b.png")
        (root / "b.txt").write_text("male, anthro", encoding="utf-8")
        _image(root / "c.png")

        imported = import_dataset(root, recursive=False)
        predictor = FakeBatchPredictor()
        report = run_caption_stage(
            imported.samples,
            source_root=root,
            predictor=predictor,
            settings=CaptionDisplaySettings(),
        )

        assert predictor.batch_calls == [["a.png", "c.png"]]
        assert [result.relative_image_path for result in report.results] == [
            "a.png",
            "b.png",
            "c.png",
        ]
        assert report.results[0].ok
        assert report.results[1].skipped is True
        assert report.results[2].ok


def test_engine_predictor_batch_delegates_to_host_engine():
    """The batch adapter calls predict_multi_batch_results and maps results back."""
    from backend.tagger2.workflow.stages.caption import (
        BatchTagPredictor,
        EngineTagPredictor,
        TagPredictor,
    )

    class Item:
        def __init__(self, text, score, category):
            self.text = text
            self.score = score
            self.category = category

    class Prediction:
        def __init__(self, tags):
            self.tags = tags

    class FakeEngine:
        def __init__(self):
            self.calls: list[dict] = []

        def predict_multi_batch_results(self, model_ids, images, **kwargs):
            self.calls.append({"model_ids": list(model_ids), "images": list(images), **kwargs})
            return [
                Prediction([Item("blue_fur", 0.8, "general"), Item("rex", 0.95, "character")]),
                Prediction([Item("male", 0.9, "general")]),
            ]

    engine = FakeEngine()
    predictor = EngineTagPredictor(engine, "eva02-large", threshold=None)
    # The adapter satisfies both protocols, so the stage may drive it in batches.
    assert isinstance(predictor, TagPredictor)
    assert isinstance(predictor, BatchTagPredictor)

    batch = predictor.predict_tags_batch([Path("a.png"), Path("b.png")])
    assert [tag.raw_tag for tag in batch[0]] == ["blue_fur", "rex"]
    assert batch[0][1].category == "character"
    assert [tag.raw_tag for tag in batch[1]] == ["male"]

    assert len(engine.calls) == 1
    call = engine.calls[0]
    assert call["model_ids"] == ["eva02-large"]
    assert call["images"] == [Path("a.png"), Path("b.png")]
    assert call["threshold"] is None
    assert call["batch_size"] == 2

    # An empty chunk never reaches the engine.
    assert predictor.predict_tags_batch([]) == ()
    assert len(engine.calls) == 1


def test_pipeline_uses_caption_tags_and_requires_a_predictor():
    """Caption output flows into the exported nine-field payload."""
    import json

    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1
    from backend.tagger2.workflow.pipeline import PipelineError, run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output, workspace = root / "src", root / "out", root / "ws"
        source.mkdir()
        output.mkdir()
        _image(source / "a.png")

        index = root / "index.csv"
        index.write_text(
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "anthro,anthro,replace,furry\n",
            encoding="utf-8",
        )

        config = WorkflowJobConfigV1.from_payload(
            {
                "profile": "e621",
                "work_mode": "full_copy",
                "overwrite_mode": "incremental",
                "source_root": {"root_id": "in", "relative_path": ""},
                "output_root": {"root_id": "out", "relative_path": ""},
                "caption": {"enabled": True, "resource_id": "tagger-test"},
                "classify": {"enabled": False},
                "replace": {"enabled": True},
                "ocr": {"enabled": False},
                "nl": {"enabled": False},
                "token_budget": {"enabled": False},
                "export": {"format": "json"},
            }
        )

        # Enabling caption without a predictor is a hard configuration error.
        with pytest.raises(PipelineError):
            run_offline_pipeline(
                config,
                source_root=source,
                output_root=output,
                workspace=workspace,
                replacement_index_path=index,
                tag_predictor=None,
            )

        report = run_offline_pipeline(
            config,
            source_root=source,
            output_root=output,
            workspace=root / "ws2",
            replacement_index_path=index,
            tag_predictor=FakePredictor(tags_by_name={"a.png": ("male", "anthro")}),
        )

        assert report.caption == {"captioned": 1, "skipped": 0, "failed": 0}
        assert report.exported_samples == 1
        payload = json.loads((output / "a.json").read_text(encoding="utf-8"))
        # Caption produced male+anthro; replace rewrote anthro -> furry.
        assert payload["tags"] == ["male", "furry"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
