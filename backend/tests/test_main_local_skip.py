import json
from pathlib import Path
from types import SimpleNamespace

from tagger2.artifacts import ArtifactManager
from tagger2.local_inference import LocalPrediction
from tagger2.main import Runtime
from tagger2.schemas import TagItem
from tagger2.storage import SQLiteStorage


def test_local_processors_skip_current_json_before_inference(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    config = {
        "model_ids": ["model-one"],
        "output": {
            "json": True,
            "txt": True,
            "replace_underscores": True,
            "conflict": "validate-skip",
        },
    }
    job = db.create_job(
        "local",
        config,
        [
            {"image_id": "one", "relative_path": "one.png"},
            {"image_id": "two", "relative_path": "two.png"},
        ],
    )
    items = db.list_items(job.id)
    sources = {}
    for item in items:
        source = tmp_path / item.relative_path
        source.write_bytes(f"source:{item.image_id}".encode())
        sources[item.id] = source

    output = tmp_path / "artifacts"
    output.mkdir()
    runtime = Runtime.__new__(Runtime)
    runtime.artifacts = ArtifactManager(db)
    runtime.resolve_item_path = lambda item: sources[item.id]
    runtime._output_path = (
        lambda item, _job, suffix: output / Path(item.relative_path).with_suffix(suffix).name
    )
    prediction = LocalPrediction(
        tags=[
            TagItem(
                text="very_highres",
                category="quality",
                score=0.9,
                source="local",
                model_id="model-one",
            )
        ]
    )
    for item in items:
        written = Runtime._write_local_result(
            runtime, item, job, sources[item.id], prediction
        )
        assert written.status == "succeeded"
        assert written.result is not None
        assert written.result["tags"][0]["text"] == "very highres"

    assert "very highres" in (output / "one.txt").read_text(encoding="utf-8")
    assert json.loads((output / "one.json").read_text(encoding="utf-8"))["tags"][0]["text"] == "very highres"

    mtimes = {path.name: path.stat().st_mtime_ns for path in output.iterdir()}

    def fail_if_inference_is_configured(_config):
        raise AssertionError("current local JSON must bypass model discovery and inference")

    runtime._local_model_ids = fail_if_inference_is_configured
    single = Runtime._local_processor_sync(runtime, items[0], job)
    batch = Runtime._local_batch_processor_sync(runtime, items, job)

    assert single.status == "skipped"
    assert [result.status for result in batch] == ["skipped", "skipped"]
    assert {path.name: path.stat().st_mtime_ns for path in output.iterdir()} == mtimes
    db.close()


def test_workbench_result_keeps_models_separate_without_writing_artifacts(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    config = {
        "model_ids": ["model-one", "model-two"],
        "separate_models": True,
        "output": {
            "json": False,
            "txt": False,
            "replace_underscores": True,
            "conflict": "validate-skip",
        },
    }
    job = db.create_job(
        "local",
        config,
        [{"image_id": "one", "relative_path": "one.png"}],
    )
    item = db.list_items(job.id)[0]
    source = tmp_path / "one.png"
    source.write_bytes(b"source")
    runtime = Runtime.__new__(Runtime)
    runtime.registry = SimpleNamespace(
        get=lambda model_id: SimpleNamespace(name={
            "model-one": "Model One",
            "model-two": "Model Two",
        }[model_id])
    )
    runtime._output_path = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("web-only workbench results must not resolve artifact paths")
    )
    prediction = LocalPrediction(
        tags=[
            TagItem(text="shared_tag", model_id="model-one,model-two"),
        ],
        model_tags={
            "model-one": [TagItem(text="first_tag", model_id="model-one")],
            "model-two": [TagItem(text="second_tag", model_id="model-two")],
        },
    )

    written = Runtime._write_local_result(runtime, item, job, source, prediction)

    assert written.status == "succeeded"
    assert written.result is not None
    assert written.result["artifacts"] == []
    assert [group["model_name"] for group in written.result["model_results"]] == [
        "Model One",
        "Model Two",
    ]
    assert [group["tags"][0]["text"] for group in written.result["model_results"]] == [
        "first tag",
        "second tag",
    ]
    assert db.list_artifacts(item.id) == []
    db.close()


def test_workbench_single_model_keeps_one_named_result_without_artifacts(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    config = {
        "model_ids": ["model-one"],
        "separate_models": True,
        "output": {"json": False, "txt": False, "conflict": "validate-skip"},
    }
    job = db.create_job(
        "local",
        config,
        [{"image_id": "one", "relative_path": "one.png"}],
    )
    item = db.list_items(job.id)[0]
    source = tmp_path / "one.png"
    source.write_bytes(b"source")
    runtime = Runtime.__new__(Runtime)
    runtime.registry = SimpleNamespace(
        get=lambda _model_id: SimpleNamespace(name="Model One")
    )
    runtime._output_path = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("web-only workbench results must not resolve artifact paths")
    )
    prediction = LocalPrediction(
        tags=[TagItem(text="single_tag", model_id="model-one")],
        model_tags={
            "model-one": [TagItem(text="single_tag", model_id="model-one")]
        },
    )

    written = Runtime._write_local_result(runtime, item, job, source, prediction)

    assert written.status == "succeeded"
    assert written.result is not None
    assert written.result["artifacts"] == []
    assert written.result["model_results"] == [
        {
            "model_id": "model-one",
            "model_name": "Model One",
            "tags": [
                {
                    "text": "single_tag",
                    "category": "general",
                    "score": None,
                    "source": "local",
                    "model_id": "model-one",
                }
            ],
        }
    ]
    assert db.list_artifacts(item.id) == []
    db.close()


def test_local_txt_uses_default_rating_filter_and_parenthesis_escape(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    config = {
        "model_ids": ["model-one"],
        "output": {
            "json": False,
            "txt": True,
            "replace_underscores": True,
            "conflict": "overwrite",
        },
    }
    job = db.create_job(
        "local",
        config,
        [{"image_id": "one", "relative_path": "one.png"}],
    )
    item = db.list_items(job.id)[0]
    source = tmp_path / "one.png"
    source.write_bytes(b"source")
    output = tmp_path / "artifacts"
    runtime = Runtime.__new__(Runtime)
    runtime.artifacts = ArtifactManager(db)
    runtime._output_path = lambda *_args, **_kwargs: output / "one.txt"
    prediction = LocalPrediction(
        tags=[
            TagItem(text="fennix_(fortnite)", category="character", model_id="model-one"),
            TagItem(text="questionable", category="rating", model_id="model-one"),
        ]
    )

    written = Runtime._write_local_result(runtime, item, job, source, prediction)

    assert written.status == "succeeded"
    assert written.result is not None
    assert [tag["text"] for tag in written.result["tags"]] == [r"fennix \(fortnite\)"]
    assert (output / "one.txt").read_text(encoding="utf-8") == "fennix \\(fortnite\\)\n"
    db.close()
