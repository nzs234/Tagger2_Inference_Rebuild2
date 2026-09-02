"""Incremental job counters and SSE poll-loop connection reuse."""

import asyncio
import random
import sqlite3

import pytest

from tagger2.jobs import JobManager
from tagger2.storage import (
    TERMINAL_ITEM_STATES,
    VALID_TRANSITIONS,
    JobRecord,
    SQLiteStorage,
    utc_now,
)

# Mirrors the transition map enforced by SQLiteStorage.update_item.
ITEM_TRANSITIONS = {
    "pending": {"running", "skipped", "cancelled"},
    "running": {"succeeded", "failed", "skipped", "cancelled", "pending"},
    "failed": {"pending", "running"},
    "cancelled": {"pending"},
    "succeeded": set(),
    "skipped": set(),
}


def _counters(record: JobRecord) -> tuple[int, int, int, int, int]:
    return (record.total, record.processed, record.succeeded, record.skipped, record.failed)


def _assert_counters_consistent(db: SQLiteStorage, job_id: str) -> None:
    """Incremental counters must equal a full _refresh_counters recomputation."""

    before = db.get_job(job_id)
    with db.connection() as connection:
        SQLiteStorage._refresh_counters(connection, job_id, utc_now())
    after = db.get_job(job_id)
    assert before is not None and after is not None
    assert _counters(before) == _counters(after)
    items = db.list_items(job_id, limit=1000)
    expected = (
        len(items),
        sum(1 for item in items if item.status in TERMINAL_ITEM_STATES),
        sum(1 for item in items if item.status == "succeeded"),
        sum(1 for item in items if item.status == "skipped"),
        sum(1 for item in items if item.status == "failed"),
    )
    assert _counters(after) == expected


def test_randomized_item_updates_match_full_counter_recomputation(tmp_path):
    db = SQLiteStorage(tmp_path / "counters.sqlite3")
    rng = random.Random(20260902)
    job = db.create_job(
        "online",
        {},
        [{"image_id": f"image-{index}", "relative_path": f"{index}.png"} for index in range(8)],
    )
    job_id = job.id
    statuses = {item.id: item.status for item in db.list_items(job_id)}
    job_state = "queued"

    for _ in range(600):
        roll = rng.random()
        if roll < 0.08:
            # Job-state churn, including requeues out of terminal states.
            options = sorted(VALID_TRANSITIONS[job_state])
            if options:
                job_state = rng.choice(options)
                db.transition_job(job_id, job_state)
        elif roll < 0.16 and job_state == "running":
            item = db.claim_next_item(job_id)
            if item is not None:
                statuses[item.id] = "running"
        elif roll < 0.22:
            if db.cancel_pending_items(job_id):
                statuses = {
                    item_id: ("cancelled" if status in {"pending", "running"} else status)
                    for item_id, status in statuses.items()
                }
        elif roll < 0.28:
            if db.reset_failed_items(job_id):
                statuses = {
                    item_id: ("pending" if status == "failed" else status)
                    for item_id, status in statuses.items()
                }
                job_state = "queued"
        else:
            movable = [item_id for item_id, status in statuses.items() if ITEM_TRANSITIONS[status]]
            if movable:
                item_id = rng.choice(movable)
                target = rng.choice(sorted(ITEM_TRANSITIONS[statuses[item_id]]))
                db.update_item(item_id, target, result={"ok": True} if target == "succeeded" else None)
                statuses[item_id] = target
        _assert_counters_consistent(db, job_id)

    # Whichever state the walk ended in, a forced terminal transition must
    # reconcile to the exact same values the incremental maintenance produced.
    db.transition_job(job_id, "cancelled", force=True)
    _assert_counters_consistent(db, job_id)
    db.close()


def test_retry_failed_and_cancel_reset_paths_keep_counters_exact(tmp_path):
    db = SQLiteStorage(tmp_path / "paths.sqlite3")
    job = db.create_job(
        "online",
        {},
        [{"image_id": f"image-{index}", "relative_path": f"{index}.png"} for index in range(4)],
    )
    job_id = job.id
    db.transition_job(job_id, "running")
    first = db.claim_next_item(job_id)
    assert first is not None
    db.update_item(first.id, "succeeded")
    second = db.claim_next_item(job_id)
    assert second is not None
    db.update_item(second.id, "failed", error="boom")
    assert _counters(db.get_job(job_id)) == (4, 2, 1, 0, 1)

    # retry-failed moves failed items back to pending.
    assert db.reset_failed_items(job_id) == 1
    record = db.get_job(job_id)
    assert _counters(record) == (4, 1, 1, 0, 0)
    assert record.state == "queued"
    _assert_counters_consistent(db, job_id)

    # The retried item runs and succeeds again.
    db.transition_job(job_id, "running")
    retried = db.claim_next_item(job_id)
    assert retried is not None and retried.id == second.id
    db.update_item(retried.id, "succeeded")
    assert _counters(db.get_job(job_id)) == (4, 2, 2, 0, 0)

    # An in-flight item reset to pending (interrupted worker) leaves the
    # processed counters untouched.
    third = db.claim_next_item(job_id)
    assert third is not None
    db.update_item(third.id, "pending", error="job interrupted")
    assert _counters(db.get_job(job_id)) == (4, 2, 2, 0, 0)
    _assert_counters_consistent(db, job_id)

    # Cancellation moves pending and in-flight items to cancelled, which
    # counts as processed but not as succeeded/failed/skipped.
    assert db.cancel_pending_items(job_id) == 2
    assert _counters(db.get_job(job_id)) == (4, 4, 2, 0, 0)
    _assert_counters_consistent(db, job_id)

    # Requeueing a cancelled item drops it out of processed again.
    cancelled_item = db.list_items(job_id, status="cancelled")[0]
    db.update_item(cancelled_item.id, "pending")
    assert _counters(db.get_job(job_id)) == (4, 3, 2, 0, 0)
    _assert_counters_consistent(db, job_id)
    db.close()


def test_refresh_job_counters_repairs_out_of_band_drift(tmp_path):
    db = SQLiteStorage(tmp_path / "drift.sqlite3")
    job = db.create_job(
        "online",
        {},
        [{"image_id": "one", "relative_path": "one.png"}, {"image_id": "two", "relative_path": "two.png"}],
    )
    with db.transaction() as connection:
        connection.execute("UPDATE jobs SET succeeded=5, processed=5 WHERE id=?", (job.id,))
    record = db.refresh_job_counters(job.id)
    assert _counters(record) == (2, 0, 0, 0, 0)
    with pytest.raises(KeyError):
        db.refresh_job_counters("missing-job")
    db.close()


def test_job_manager_retry_failed_reconciles_counters():
    async def run():
        db = SQLiteStorage(":memory:")
        attempts: dict[str, int] = {}

        async def processor(item, job):
            attempts[item.image_id] = attempts.get(item.image_id, 0) + 1
            if attempts[item.image_id] == 1:
                raise RuntimeError("temporary")
            return {"result": {"ok": True}}

        manager = JobManager(db, processors={"online": processor})
        job = manager.create_job(
            "online",
            {},
            [{"image_id": "one", "relative_path": "one"}, {"image_id": "two", "relative_path": "two"}],
        )
        await manager.run(job.id)
        record = db.get_job(job.id)
        assert (record.state, record.processed, record.failed, record.succeeded) == ("failed", 2, 2, 0)

        assert await manager.retry_failed(job.id, start=True) == 2
        record = db.get_job(job.id)
        assert (record.processed, record.failed, record.succeeded) == (0, 0, 0)

        await manager.run(job.id)
        record = db.get_job(job.id)
        assert (record.state, record.processed, record.succeeded, record.failed) == ("succeeded", 2, 2, 0)
        _assert_counters_consistent(db, job.id)
        await manager.shutdown()
        db.close()

    asyncio.run(run())


def test_job_manager_cancel_counts_cancelled_items():
    async def run():
        db = SQLiteStorage(":memory:")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def processor(item, job):
            if item.image_id == "one":
                entered.set()
                await release.wait()
            return {"result": {"image": item.image_id}}

        manager = JobManager(db, processors={"online": processor})
        job = manager.create_job(
            "online",
            {},
            [
                {"image_id": "one", "relative_path": "one"},
                {"image_id": "two", "relative_path": "two"},
                {"image_id": "three", "relative_path": "three"},
            ],
        )
        task = await manager.start(job.id)
        await entered.wait()
        await manager.cancel(job.id)
        release.set()
        await task
        record = db.get_job(job.id)
        assert record.state == "cancelled"
        statuses = {item.image_id: item.status for item in db.list_items(job.id)}
        assert statuses == {"one": "succeeded", "two": "cancelled", "three": "cancelled"}
        assert _counters(record) == (3, 3, 1, 0, 0)
        _assert_counters_consistent(db, job.id)
        await manager.shutdown()
        db.close()

    asyncio.run(run())


def test_event_stream_reuses_one_connection_and_recovers_from_errors():
    async def run():
        db = SQLiteStorage(":memory:")
        manager = JobManager(db)
        job = db.create_job("online", {}, [{"image_id": "one", "relative_path": "one.png"}])
        db.transition_job(job.id, "running")

        opens = {"count": 0}
        real_open = db._open

        def counting_open():
            opens["count"] += 1
            return real_open()

        db._open = counting_open

        stream = manager.event_stream(job.id, poll_seconds=0.05)
        first = await anext(stream)
        assert first["state"] == "queued"
        second = await anext(stream)
        assert second["state"] == "running"

        # Appended events arrive on later polls without opening any new
        # connection: the stream reuses the one it opened at start.
        for index in range(3):
            db.append_event(job.id, {"note": f"extra-{index}"})
            before_poll = opens["count"]
            event = await asyncio.wait_for(anext(stream), timeout=2.0)
            assert event["note"] == f"extra-{index}"
            assert opens["count"] == before_poll

        # A broken connection must not end the stream: the next poll backs
        # off, reopens exactly one fresh connection and keeps going.
        real_read = db.read_events_since
        flaky = {"armed": True}

        def flaky_read(*args, **kwargs):
            if flaky["armed"]:
                flaky["armed"] = False
                raise sqlite3.OperationalError("connection clobbered")
            return real_read(*args, **kwargs)

        db.read_events_since = flaky_read
        db.append_event(job.id, {"note": "after-error"})
        before_reconnect = opens["count"]
        event = await asyncio.wait_for(anext(stream), timeout=5.0)
        assert event["note"] == "after-error"
        assert opens["count"] == before_reconnect + 1

        db.transition_job(job.id, "interrupted", phase="interrupted")
        event = await asyncio.wait_for(anext(stream), timeout=5.0)
        assert event["state"] == "interrupted"
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1.0)

        await manager.shutdown()
        db.close()

    asyncio.run(run())


def test_list_profiles_use_single_query_per_call(tmp_path):
    db = SQLiteStorage(tmp_path / "profiles.sqlite3")
    for index in reversed(range(3)):
        db.upsert_provider_profile(
            f"provider-{index}",
            name=f"provider-{index}",
            kind="custom",
            base_url="https://example.invalid",
            config={"api_key": "secret"},
        )
        db.upsert_model_profile(f"model-{index}", name=f"model-{index}", config={"temperature": index})

    opens = {"count": 0}
    real_open = db._open

    def counting_open():
        opens["count"] += 1
        return real_open()

    db._open = counting_open

    providers = db.list_provider_profiles()
    assert [profile["name"] for profile in providers] == ["provider-0", "provider-1", "provider-2"]
    assert all(profile["config"]["api_key"] == "[configured]" for profile in providers)
    assert opens["count"] == 1

    opens["count"] = 0
    models = db.list_model_profiles()
    assert [profile["name"] for profile in models] == ["model-0", "model-1", "model-2"]
    assert models[1]["config"] == {"temperature": 1}
    assert opens["count"] == 1
    db.close()
