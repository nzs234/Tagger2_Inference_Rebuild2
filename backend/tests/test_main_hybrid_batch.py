import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from tagger2.anima import parse_anima_response
from tagger2.artifacts import ArtifactManager, render_hybrid_nl_tags
from tagger2.jobs import ProcessResult
from tagger2.main import Runtime
from tagger2.storage import SQLiteStorage


def _local_result(*tags: str) -> ProcessResult:
    return ProcessResult(
        result={
            "tags": [
                {
                    "text": tag,
                    "category": "general",
                    "score": 0.9,
                    "source": "local",
                    "model_id": "merged",
                }
                for tag in tags
            ],
            "warnings": [],
            "timing": {"local_ms": 3.0},
        }
    )


def _runtime(tmp_path: Path, db: SQLiteStorage, provider):
    output = tmp_path / "out"
    runtime = Runtime.__new__(Runtime)
    runtime.artifacts = ArtifactManager(db)
    runtime.provider = lambda _provider_id, **_kwargs: provider
    runtime._output_path = (
        lambda item, _job, suffix: output / Path(item.relative_path).with_suffix(suffix).name
    )
    return runtime, output


def test_hybrid_nl_writes_one_caption_then_delimiter_then_merged_tags(tmp_path):
    async def run():
        source = tmp_path / "image.png"
        source.write_bytes(b"source")
        db = SQLiteStorage(tmp_path / "jobs.sqlite3")
        job = db.create_job(
            "local",
            {
                "hybrid": True,
                "provider_id": "caption",
                "provider_model": "caption-model",
                "online_response": "nl",
                "nl_prompt": "CUSTOM NL",
                "output": {"txt": True, "json": False, "conflict": "validate-skip"},
            },
            [{"image_id": "image", "relative_path": source.name}],
        )
        item = db.list_items(job.id)[0]

        class Provider:
            model = "caption-model"
            calls = 0

            async def generate(self, image, prompt, *, model=None):
                self.calls += 1
                assert image == source
                assert model == "caption-model"
                assert "CUSTOM NL" in prompt
                return "A detailed caption."

            async def generate_anima(self, *_args, **_kwargs):
                raise AssertionError("NL hybrid output must not generate Anima JSON")

        provider = Provider()
        runtime, output = _runtime(tmp_path, db, provider)
        result = await Runtime._write_hybrid_result(
            runtime,
            item,
            job,
            source,
            _local_result("shared_tag", "shared_tag", "second_tag"),
        )

        assert result.status == "succeeded"
        assert result.result is not None
        assert result.result["caption"] == "A detailed caption."
        assert (output / "image.txt").read_text(encoding="utf-8") == (
            "A detailed caption.\n|||\nshared_tag, second_tag\n"
        )
        assert not (output / "image.json").exists()
        assert Runtime._hybrid_outputs_current(runtime, item, job, source)
        skipped = Runtime._hybrid_skipped_result(runtime, item, job, source)
        assert skipped.status == "skipped"
        assert provider.calls == 1
        db.close()

    asyncio.run(run())


def test_hybrid_json_writes_local_tag_txt_and_online_anima_json(tmp_path):
    async def run():
        source = tmp_path / "image.png"
        source.write_bytes(b"source")
        db = SQLiteStorage(tmp_path / "jobs.sqlite3")
        job = db.create_job(
            "local",
            {
                "hybrid": True,
                "provider_id": "anima",
                "online_response": "json",
                "output": {
                    "txt": True,
                    "json": True,
                    "replace_underscores": True,
                    "conflict": "overwrite",
                },
            },
            [{"image_id": "image", "relative_path": source.name}],
        )
        item = db.list_items(job.id)[0]

        class Provider:
            model = "anima-model"

            async def generate(self, *_args, **_kwargs):
                raise AssertionError("JSON hybrid output must not call text generation")

            async def generate_anima(self, image, _prompt, *, trigger_artist, model=None):
                assert image == source
                assert trigger_artist == ""
                assert model is None
                return parse_anima_response(
                    '{"quality":["highres"],"count":"solo","character":"",'
                    '"series":"","artist":"","appearance":["red_fur"],'
                    '"tags":["digital_art"],"environment":["indoors"],'
                    '"nl":"A red subject."}'
                )

        runtime, output = _runtime(tmp_path, db, Provider())
        result = await Runtime._write_hybrid_result(
            runtime,
            item,
            job,
            source,
            _local_result("local_tag", "other_tag"),
        )

        assert result.status == "succeeded"
        assert (output / "image.txt").read_text(encoding="utf-8") == "local_tag, other_tag\n"
        anima = json.loads((output / "image.json").read_text(encoding="utf-8"))
        assert set(anima) == {
            "quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl"
        }
        assert anima["appearance"] == ["red fur"]
        assert anima["tags"] == ["digital art"]
        assert result.result is not None
        assert result.result["anima"] == anima
        assert {entry.kind for entry in db.list_artifacts(item.id)} == {
            "hybrid_local_tags_txt",
            "hybrid_anima_json",
        }
        db.close()

    asyncio.run(run())


def test_hybrid_batch_scopes_online_failure_to_the_failed_item(tmp_path):
    async def run():
        db = SQLiteStorage(tmp_path / "jobs.sqlite3")
        sources = {}
        for name in ("one.png", "two.png"):
            source = tmp_path / name
            source.write_bytes(name.encode())
            sources[name] = source
        job = db.create_job(
            "local",
            {
                "hybrid": True,
                "provider_id": "caption",
                "online_response": "nl",
                "online_concurrency": 2,
                "output": {"txt": True, "json": False, "conflict": "overwrite"},
            },
            [
                {"image_id": "one", "relative_path": "one.png"},
                {"image_id": "two", "relative_path": "two.png"},
            ],
        )
        items = db.list_items(job.id)

        class Provider:
            model = "caption-model"

            async def generate(self, image, _prompt, **_kwargs):
                if image.name == "two.png":
                    raise RuntimeError("provider unavailable")
                return "first caption"

        runtime, output = _runtime(tmp_path, db, Provider())
        runtime.settings = SimpleNamespace(max_online_concurrency=8)
        runtime.gpu_lock = asyncio.Lock()
        runtime.resolve_item_path = lambda item: sources[item.relative_path]
        runtime._local_batch_processor_sync = lambda batch, _job: [
            _local_result(f"{item.image_id}_tag") for item in batch
        ]

        results = await Runtime._hybrid_batch_processor(runtime, items, job)

        assert [result.status for result in results] == ["succeeded", "failed"]
        assert (output / "one.txt").read_text(encoding="utf-8") == "first caption\n|||\none_tag\n"
        assert not (output / "two.txt").exists()
        db.close()

    asyncio.run(run())


def test_hybrid_renderer_uses_a_stable_single_line_delimiter():
    assert render_hybrid_nl_tags("caption\n", ["one", "ONE", "two"]) == (
        "caption\n|||\none, two\n"
    )
