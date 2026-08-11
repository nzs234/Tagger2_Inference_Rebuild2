import asyncio
import json

import pytest

from tagger2.artifacts import (
    LOCAL_TAG_SCHEMA_VERSION,
    ArtifactManager,
    render_anima_txt,
    validate_artifact_file,
    validate_local_tags_file,
)
from tagger2.anima import parse_anima_response
from tagger2.jobs import JobManager
from tagger2.storage import SQLiteStorage


def _anima():
    return parse_anima_response(
        '{"quality":["highres"],"count":"solo","character":"","series":"","artist":"","appearance":["red fur"],"tags":["digital art"],"environment":["outdoors"],"nl":"caption"}'
    )


def test_sqlite_wal_job_lifecycle_and_secret_redaction(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    job = db.create_job("online", {"model": "vision", "api_key": "do-not-store"}, [{"image_id": "one", "relative_path": "one.png"}])
    assert db.get_job(job.id).config["api_key"] == "[configured]"
    db.transition_job(job.id, "running")
    item = db.claim_next_item(job.id)
    claim_event = db.get_latest_event(job.id)
    assert claim_event.data["current_item"] == "one"
    db.update_item(item.id, "succeeded", result={"ok": True})
    assert db.get_job(job.id).succeeded == 1
    events = db.get_events(job.id, after_seq=claim_event.seq)
    assert events and all(event.seq > claim_event.seq for event in events)
    db.close()


def test_atomic_artifacts_and_current_check(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    source = tmp_path / "image.png"
    source.write_bytes(b"source")
    job = db.create_job("online", {"prompt_version": "1"}, [{"image_id": "one", "relative_path": source.name}])
    item = db.list_items(job.id)[0]
    manager = ArtifactManager(db)
    output = tmp_path / "out"
    result = manager.write_anima(job_id=job.id, item_id=item.id, source_path=source, payload=_anima(), config_hash=job.config_hash, output_dir=output, write_txt=True)
    assert result.json_path.is_file() and result.txt_path.is_file()
    assert manager.should_skip(item_id=item.id, source_path=source, json_path=result.json_path, config_hash=job.config_hash)
    repeat_job = db.create_job("online", {"prompt_version": "1"}, [{"image_id": "two", "relative_path": source.name}])
    repeat_item = db.list_items(repeat_job.id)[0]
    assert manager.should_skip(item_id=repeat_item.id, source_path=source, json_path=result.json_path, config_hash=repeat_job.config_hash)
    assert not manager.should_skip(item_id=item.id, source_path=source, json_path=result.json_path, config_hash="changed-config")
    source.write_bytes(b"changed source")
    assert not manager.should_skip(item_id=item.id, source_path=source, json_path=result.json_path, config_hash=job.config_hash)
    assert "caption" not in result.txt_path.read_text(encoding="utf-8")
    assert render_anima_txt(_anima()).startswith("highres, solo")

    standalone = tmp_path / "standalone.txt"
    manager.write_txt(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        payload=_anima(),
        config_hash=job.config_hash,
        txt_path=standalone,
    )
    assert standalone.is_file()
    assert any(record.path == str(standalone.resolve()) for record in db.list_artifacts(item.id))


def test_local_artifacts_require_matching_source_config_schema_and_content(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    source = tmp_path / "image.png"
    source.write_bytes(b"stable source")
    config = {
        "model_ids": ["model-one"],
        "output": {"json": True, "txt": True, "conflict": "validate-skip"},
    }
    job = db.create_job("local", config, [{"image_id": "one", "relative_path": source.name}])
    item = db.list_items(job.id)[0]
    manager = ArtifactManager(db)
    json_path = tmp_path / "image.json"
    txt_path = tmp_path / "image.txt"
    tag = {
        "text": "highres",
        "category": "quality",
        "score": 0.9,
        "source": "local",
        "model_id": "model-one",
    }
    json_bytes = (json.dumps({"tags": [tag]}, indent=2) + "\n").encode()
    manager.write_bytes(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        artifact_path=json_path,
        kind="local_tags_json",
        data=json_bytes,
        config_hash=job.config_hash,
        schema_version=LOCAL_TAG_SCHEMA_VERSION,
    )
    manager.write_bytes(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        artifact_path=txt_path,
        kind="local_tags_txt",
        data=b"highres\n",
        config_hash=job.config_hash,
        schema_version=LOCAL_TAG_SCHEMA_VERSION,
    )

    def is_current(path, kind, validator, *, current_item=item, config_hash=job.config_hash, schema=LOCAL_TAG_SCHEMA_VERSION):
        return manager.should_skip_file(
            item_id=current_item.id,
            source_path=source,
            artifact_path=path,
            kind=kind,
            config_hash=config_hash,
            schema_version=schema,
            validator=validator,
        )

    assert is_current(json_path, "local_tags_json", validate_local_tags_file)
    assert is_current(txt_path, "local_tags_txt", validate_artifact_file)

    repeat = db.create_job("local", config, [{"image_id": "two", "relative_path": source.name}])
    repeat_item = db.list_items(repeat.id)[0]
    assert is_current(
        json_path,
        "local_tags_json",
        validate_local_tags_file,
        current_item=repeat_item,
        config_hash=repeat.config_hash,
    )
    assert not is_current(
        json_path,
        "local_tags_json",
        validate_local_tags_file,
        config_hash="changed-config",
    )
    assert not is_current(
        json_path,
        "local_tags_json",
        validate_local_tags_file,
        schema="local-tags-v0",
    )

    txt_path.write_text("tampered", encoding="utf-8")
    assert not is_current(txt_path, "local_tags_txt", validate_artifact_file)
    source.write_bytes(b"changed source")
    assert not is_current(json_path, "local_tags_json", validate_local_tags_file)

    source.write_bytes(b"stable source")
    manager.write_bytes(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        artifact_path=json_path,
        kind="local_tags_json",
        data=b'{"tags":[{"text":""}]}',
        config_hash=job.config_hash,
        schema_version=LOCAL_TAG_SCHEMA_VERSION,
    )
    assert not is_current(json_path, "local_tags_json", validate_local_tags_file)
    db.close()


def test_job_manager_processor_and_retry():
    async def run():
        db = SQLiteStorage(":memory:")
        attempts = {"one": 0}

        async def processor(item, job):
            attempts[item.image_id] += 1
            if attempts[item.image_id] == 1:
                raise RuntimeError("temporary")
            return {"result": {"ok": True}}

        manager = JobManager(db, processors={"online": processor})
        job = manager.create_job("online", {}, [{"image_id": "one", "relative_path": "one"}])
        await manager.run(job.id)
        assert db.get_job(job.id).state == "failed"
        assert await manager.retry_failed(job.id, start=True) == 1
        await manager.run(job.id)
        assert db.get_job(job.id).state == "succeeded"
        await manager.shutdown()

    asyncio.run(run())


def test_restart_recovery_and_queued_cancel(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = SQLiteStorage(path)
    job = first.create_job("online", {}, [{"image_id": "one", "relative_path": "one"}])
    first.transition_job(job.id, "running")
    assert first.claim_next_item(job.id).status == "running"
    first.close()

    second = SQLiteStorage(path)
    assert second.get_job(job.id).state == "interrupted"
    assert second.list_items(job.id)[0].status == "pending"

    async def cancel():
        manager = JobManager(second)
        await manager.cancel(job.id)
        assert second.get_job(job.id).state == "cancelled"

    asyncio.run(cancel())
    second.close()


def test_event_stream_replays_after_last_id_and_ends_for_interrupted_jobs():
    async def run():
        db = SQLiteStorage(":memory:")
        manager = JobManager(db)
        job = db.create_job(
            "online", {}, [{"image_id": "one", "relative_path": "one.png"}]
        )
        db.transition_job(job.id, "running")
        running_seq = db.get_latest_event(job.id).seq
        db.transition_job(job.id, "interrupted", phase="interrupted")
        interrupted_seq = db.get_latest_event(job.id).seq

        stream = manager.event_stream(
            job.id, last_event_id=running_seq, poll_seconds=0.01
        )
        event = await anext(stream)
        assert event["seq"] == interrupted_seq
        assert event["state"] == "interrupted"
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=0.2)

        caught_up = manager.event_stream(
            job.id, last_event_id=interrupted_seq, poll_seconds=0.01
        )
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(caught_up), timeout=0.1)

        paused = db.create_job(
            "online", {}, [{"image_id": "two", "relative_path": "two.png"}]
        )
        db.transition_job(paused.id, "running")
        db.transition_job(paused.id, "paused", phase="paused")
        paused_seq = db.get_latest_event(paused.id).seq
        paused_stream = manager.event_stream(
            paused.id, last_event_id=paused_seq, poll_seconds=0.01
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(anext(paused_stream), timeout=0.08)

        await manager.shutdown()
        db.close()

    asyncio.run(run())


def test_online_job_uses_bounded_parallel_workers():
    async def run():
        db = SQLiteStorage(":memory:")
        active = 0
        peak = 0
        lock = asyncio.Lock()

        async def processor(item, job):
            nonlocal active, peak
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0.03)
            async with lock:
                active -= 1
            return {"result": {"image_id": item.image_id}}

        manager = JobManager(db, processors={"online": processor})
        job = manager.create_job(
            "online",
            {"_worker_concurrency": 2},
            [
                {"image_id": f"image-{index}", "relative_path": f"{index}.png"}
                for index in range(5)
            ],
        )
        result = await manager.run(job.id)
        assert result.state == "succeeded"
        assert peak == 2
        assert len(db.list_items(job.id)) == 5
        await manager.shutdown()

    asyncio.run(run())


def test_local_job_uses_finite_batch_processor():
    async def run():
        db = SQLiteStorage(":memory:")
        batches = []

        async def batch_processor(items, job):
            batches.append([item.image_id for item in items])
            return [
                {"result": {"image_id": item.image_id, "ok": True}}
                for item in items
            ]

        manager = JobManager(db, batch_processors={"local": batch_processor})
        job = manager.create_job(
            "local",
            {"batch_size": 3},
            [
                {"image_id": f"image-{index}", "relative_path": f"{index}.png"}
                for index in range(5)
            ],
        )
        result = await manager.run(job.id)
        assert result.state == "succeeded"
        assert batches == [
            ["image-0", "image-1", "image-2"],
            ["image-3", "image-4"],
        ]
        assert all(item.result["ok"] for item in db.list_items(job.id))
        await manager.shutdown()

    asyncio.run(run())


def test_running_job_pauses_between_items_and_resumes():
    async def run():
        db = SQLiteStorage(":memory:")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def processor(item, _job):
            if item.image_id == "one":
                entered.set()
                await release.wait()
            return {"result": {"image": item.image_id}}

        manager = JobManager(db, processors={"online": processor})
        job = manager.create_job(
            "online",
            {},
            [{"image_id": "one", "relative_path": "one"}, {"image_id": "two", "relative_path": "two"}],
        )
        task = await manager.start(job.id)
        await entered.wait()
        await manager.pause(job.id)
        release.set()
        for _ in range(100):
            if db.get_job(job.id).processed == 1:
                break
            await asyncio.sleep(0)
        assert db.get_job(job.id).state == "paused"
        assert db.list_items(job.id)[1].status == "pending"
        await manager.resume(job.id)
        await task
        assert db.get_job(job.id).state == "succeeded"
        await manager.shutdown()
        db.close()

    asyncio.run(run())
