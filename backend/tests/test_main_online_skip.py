import asyncio

from tagger2.anima import parse_anima_response
from tagger2.artifacts import ArtifactManager
from tagger2.main import Runtime
from tagger2.storage import SQLiteStorage


def test_online_processor_uses_current_artifact_before_provider(tmp_path):
    async def run():
        source = tmp_path / "image.bin"
        source.write_bytes(b"stable source")
        output = tmp_path / "artifacts"
        db = SQLiteStorage(tmp_path / "jobs.sqlite3")
        config = {"output": {"json": True, "txt": True, "txt_include_tags": True, "conflict": "validate-skip"}, "trigger_artist": ""}
        job = db.create_job("online", config, [{"image_id": "image", "relative_path": source.name}])
        item = db.list_items(job.id)[0]
        artifact_manager = ArtifactManager(db)
        payload = parse_anima_response(
            '{"quality":["highres"],"count":"solo","character":"","series":"","artist":"","appearance":["red fur"],"tags":["digital art"],"environment":["outdoors"],"nl":"caption"}'
        )
        json_path = output / "image.json"
        artifact_manager.write_anima(
            job_id=job.id,
            item_id=item.id,
            source_path=source,
            payload=payload,
            config_hash=job.config_hash,
            output_dir=output,
            relative_path=json_path.name,
        )

        class FailingProvider:
            model = "vision"
            calls = 0

            async def generate_anima(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("online provider must not run for a current artifact")

        provider = FailingProvider()
        runtime = Runtime.__new__(Runtime)
        runtime.artifacts = artifact_manager
        runtime.provider = lambda _provider_id, **_kwargs: provider
        runtime.resolve_item_path = lambda _item: source
        runtime._output_path = lambda _item, _job, suffix: output / f"image{suffix}"

        result = await Runtime.online_processor(runtime, item, job)
        assert result.status == "succeeded"
        assert provider.calls == 0
        assert (output / "image.txt").is_file()

        json_mtime = json_path.stat().st_mtime_ns
        txt_path = output / "image.txt"
        txt_mtime = txt_path.stat().st_mtime_ns
        repeated = await Runtime.online_processor(runtime, item, job)
        assert repeated.status == "skipped"
        assert provider.calls == 0
        assert json_path.stat().st_mtime_ns == json_mtime
        assert txt_path.stat().st_mtime_ns == txt_mtime

        txt_path.write_text("tampered", encoding="utf-8")
        repaired = await Runtime.online_processor(runtime, item, job)
        assert repaired.status == "succeeded"
        assert provider.calls == 0
        assert "highres" in txt_path.read_text(encoding="utf-8")
        assert json_path.stat().st_mtime_ns == json_mtime
        db.close()

    asyncio.run(run())


def test_online_txt_only_uses_editable_nl_and_tag_prompts(tmp_path):
    async def run():
        source = tmp_path / "image.bin"
        source.write_bytes(b"source")
        output = tmp_path / "artifacts"
        db = SQLiteStorage(tmp_path / "jobs.sqlite3")
        config = {
            "provider_id": "vision",
            "provider_model": "model",
            "nl_prompt": "CUSTOM NL PROMPT",
            "tag_prompt": "CUSTOM TAG PROMPT",
            "json_prompt": "CUSTOM JSON PROMPT",
            "output": {
                "json": False,
                "txt": True,
                "txt_include_tags": True,
                "conflict": "overwrite",
            },
        }
        job = db.create_job(
            "online", config, [{"image_id": "image", "relative_path": source.name}]
        )
        item = db.list_items(job.id)[0]

        class Provider:
            model = "model"
            prompts = []

            async def generate(self, _image, prompt, **_kwargs):
                self.prompts.append(prompt)
                return "<NL start>A detailed caption.<NL end>\n<TAG start>red_hair, portrait<TAG end>"

        provider = Provider()
        runtime = Runtime.__new__(Runtime)
        runtime.artifacts = ArtifactManager(db)
        runtime.provider = lambda _provider_id, **_kwargs: provider
        runtime.resolve_item_path = lambda _item: source
        runtime._output_path = lambda _item, _job, suffix: output / f"image{suffix}"

        result = await Runtime.online_processor(runtime, item, job)

        assert result.status == "succeeded"
        assert len(provider.prompts) == 1
        assert "CUSTOM NL PROMPT" in provider.prompts[0]
        assert "CUSTOM TAG PROMPT" in provider.prompts[0]
        assert "CUSTOM JSON PROMPT" not in provider.prompts[0]
        assert result.result is not None
        assert result.result["anima"] is None
        assert result.result["caption"] == "A detailed caption."
        assert [tag["text"] for tag in result.result["tags"]] == ["red_hair", "portrait"]
        assert not (output / "image.json").exists()
        assert (output / "image.txt").read_text(encoding="utf-8") == (
            "A detailed caption.\n\nred_hair, portrait\n"
        )
        db.close()

    asyncio.run(run())


def test_workbench_online_nl_uses_plain_caption_without_artifacts(tmp_path):
    async def run():
        source = tmp_path / "image.bin"
        source.write_bytes(b"source")
        db = SQLiteStorage(tmp_path / "jobs.sqlite3")
        config = {
            "provider_id": "vision",
            "provider_model": "vision-nl",
            "online_response": "nl",
            "nl_prompt": "CUSTOM WORKBENCH NL PROMPT",
            "json_prompt": "MUST NOT BE USED",
            "output": {
                "json": False,
                "txt": False,
                "txt_include_tags": False,
                "conflict": "validate-skip",
            },
        }
        job = db.create_job(
            "online",
            config,
            [{"image_id": "image", "relative_path": source.name}],
        )
        item = db.list_items(job.id)[0]

        class Provider:
            model = "provider-default"
            prompts: list[str] = []

            async def generate(self, _image, prompt, *, model=None):
                self.prompts.append(prompt)
                assert model == "vision-nl"
                return "A detailed natural-language caption."

            async def generate_anima(self, *_args, **_kwargs):
                raise AssertionError("workbench NL mode must not call JSON generation")

        provider = Provider()
        runtime = Runtime.__new__(Runtime)
        runtime.artifacts = ArtifactManager(db)
        runtime.provider = lambda _provider_id, **_kwargs: provider
        runtime.resolve_item_path = lambda _item: source
        runtime._output_path = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("web-only NL results must not resolve artifact paths")
        )

        processed = await Runtime.online_processor(runtime, item, job)

        assert processed.status == "succeeded"
        assert len(provider.prompts) == 1
        assert "CUSTOM WORKBENCH NL PROMPT" in provider.prompts[0]
        assert "MUST NOT BE USED" not in provider.prompts[0]
        assert processed.result is not None
        assert processed.result["model_id"] == "vision-nl"
        assert processed.result["caption"] == "A detailed natural-language caption."
        assert processed.result["tags"] == []
        assert processed.result["anima"] is None
        assert processed.result["artifacts"] == []
        assert db.list_artifacts(item.id) == []
        db.close()

    asyncio.run(run())
