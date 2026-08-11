"""Cooperative async job runner backed by :mod:`tagger2.storage`."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from .storage import JobItemRecord, JobRecord, SQLiteStorage


class ItemProcessor(Protocol):
    def __call__(self, item: JobItemRecord, job: JobRecord) -> Any: ...


class BatchItemProcessor(Protocol):
    def __call__(
        self, items: Sequence[JobItemRecord], job: JobRecord
    ) -> Sequence[Any] | Awaitable[Sequence[Any]]: ...


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: str = "succeeded"
    result: Mapping[str, Any] | None = None
    error: str | None = None
    duration_ms: float | None = None


class JobManager:
    """Run one cooperative worker per job and persist every state change.

    The manager intentionally does not know how local inference or online
    providers work.  A mode-specific processor receives an immutable item and
    job snapshot, and may return a mapping, :class:`ProcessResult`, or an
    awaitable of either.  This keeps GPU serialization and provider limits in
    their respective subsystems.
    """

    def __init__(
        self,
        storage: SQLiteStorage,
        *,
        processors: Mapping[str, ItemProcessor] | None = None,
        batch_processors: Mapping[str, BatchItemProcessor] | None = None,
        on_event: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self.storage = storage
        self.processors: dict[str, ItemProcessor] = dict(processors or {})
        self.batch_processors: dict[str, BatchItemProcessor] = dict(batch_processors or {})
        self.on_event = on_event
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pause_events: dict[str, asyncio.Event] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    def register_processor(self, mode: str, processor: ItemProcessor) -> None:
        mode = str(getattr(mode, "value", mode)).lower()
        if mode not in {"local", "online"}:
            raise ValueError("processor mode must be local or online")
        self.processors[mode] = processor

    def register_batch_processor(self, mode: str, processor: BatchItemProcessor) -> None:
        mode = str(getattr(mode, "value", mode)).lower()
        if mode not in {"local", "online"}:
            raise ValueError("processor mode must be local or online")
        self.batch_processors[mode] = processor

    def create_job(
        self,
        mode: str,
        config: Mapping[str, Any],
        items: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        *,
        source_root_id: str | None = None,
        output_root_id: str | None = None,
        start: bool = False,
    ) -> JobRecord | Awaitable[JobRecord]:
        """Create a persisted job; ``start=True`` returns an awaitable start."""

        record = self.storage.create_job(
            mode,
            config,
            items,
            source_root_id=source_root_id,
            output_root_id=output_root_id,
        )
        if not start:
            return record
        return self._create_and_start(record.id)

    async def _create_and_start(self, job_id: str) -> JobRecord:
        await self.start(job_id)
        record = self.storage.get_job(job_id)
        assert record is not None
        return record

    async def start(self, job_id: str) -> asyncio.Task[None]:
        """Start or resume a job and return its worker task."""

        async with self._lock:
            existing = self._tasks.get(job_id)
            if existing is not None and not existing.done():
                return existing
            record = self.storage.get_job(job_id)
            if record is None:
                raise KeyError(job_id)
            if record.state in {"queued", "paused", "interrupted"}:
                self.storage.transition_job(job_id, "running", phase="running")
            elif record.state != "running":
                raise ValueError(f"job {job_id} is not startable from {record.state}")
            pause = self._pause_events.setdefault(job_id, asyncio.Event())
            pause.set()
            cancel = self._cancel_events.setdefault(job_id, asyncio.Event())
            cancel.clear()
            task = asyncio.create_task(self._run(job_id), name=f"tagger2-job-{job_id}")
            self._tasks[job_id] = task

            def cleanup(done: asyncio.Task[None]) -> None:
                if self._tasks.get(job_id) is done:
                    self._tasks.pop(job_id, None)

            task.add_done_callback(cleanup)
            return task

    async def run(self, job_id: str) -> JobRecord:
        task = await self.start(job_id)
        try:
            await task
        except asyncio.CancelledError:
            raise
        record = self.storage.get_job(job_id)
        assert record is not None
        return record

    async def pause(self, job_id: str) -> JobRecord:
        record = self.storage.get_job(job_id)
        if record is None:
            raise KeyError(job_id)
        if record.state == "running":
            self._pause_events.setdefault(job_id, asyncio.Event()).clear()
            self.storage.transition_job(job_id, "paused", phase="paused")
        elif record.state != "paused":
            raise ValueError(f"job {job_id} is not running")
        return self.storage.get_job(job_id)  # type: ignore[return-value]

    async def resume(self, job_id: str) -> JobRecord:
        self._pause_events.setdefault(job_id, asyncio.Event()).set()
        record = self.storage.get_job(job_id)
        if record is None:
            raise KeyError(job_id)
        if record.state == "paused":
            self.storage.transition_job(job_id, "running", phase="running")
            await self.start(job_id)
        elif record.state in {"queued", "interrupted"}:
            await self.start(job_id)
        elif record.state != "running":
            raise ValueError(f"job {job_id} cannot resume from {record.state}")
        return self.storage.get_job(job_id)  # type: ignore[return-value]

    async def cancel(self, job_id: str) -> JobRecord:
        record = self.storage.get_job(job_id)
        if record is None:
            raise KeyError(job_id)
        self._cancel_events.setdefault(job_id, asyncio.Event()).set()
        # Wake a paused worker so it can observe cancellation.
        self._pause_events.setdefault(job_id, asyncio.Event()).set()
        if record.state in {"queued", "paused", "running", "interrupted"}:
            self.storage.transition_job(job_id, "cancelling", phase="cancelling")
            task = self._tasks.get(job_id)
            if task is None or task.done():
                self.storage.cancel_pending_items(job_id)
                self.storage.transition_job(job_id, "cancelled", phase="cancelled")
        elif record.state == "cancelling":
            pass
        else:
            raise ValueError(f"job {job_id} is already terminal")
        return self.storage.get_job(job_id)  # type: ignore[return-value]

    async def retry_failed(self, job_id: str, *, start: bool = False) -> int:
        count = self.storage.reset_failed_items(job_id)
        if count and start:
            await self.start(job_id)
        return count

    # Names used by the HTTP layer are explicit and easy to discover.
    pause_job = pause
    resume_job = resume
    cancel_job = cancel
    retry_failed_items = retry_failed

    async def _run(self, job_id: str) -> None:
        processor: ItemProcessor | None = None
        try:
            job = self.storage.get_job(job_id)
            if job is None:
                return
            processor = self.processors.get(job.mode)
            batch_processor = self.batch_processors.get(job.mode)
            if batch_processor is not None:
                await self._run_batched(
                    job_id,
                    batch_processor,
                    max(1, min(512, int(job.config.get("batch_size", 1) or 1))),
                )
                return
            requested_workers = int(job.config.get("_worker_concurrency", 1) or 1)
            if processor is not None and job.mode == "online" and requested_workers > 1:
                await self._run_parallel(job_id, processor, min(128, requested_workers))
                return
            while True:
                if self._cancel_events.setdefault(job_id, asyncio.Event()).is_set():
                    self.storage.cancel_pending_items(job_id)
                    current = self.storage.get_job(job_id)
                    if current and current.state == "cancelling":
                        self.storage.transition_job(job_id, "cancelled", phase="cancelled")
                    return

                current = self.storage.get_job(job_id)
                if current is None:
                    return
                if current.state == "paused":
                    await self._pause_events.setdefault(job_id, asyncio.Event()).wait()
                    continue
                if current.state != "running":
                    return

                item = self.storage.claim_next_item(job_id)
                if item is None:
                    current = self.storage.get_job(job_id)
                    if current is None:
                        return
                    if current.processed >= current.total:
                        final = "failed" if current.failed else "succeeded"
                        self.storage.transition_job(job_id, final, phase=final)
                        return
                    # A concurrent controller may have paused/cancelled between
                    # the state read and claim; let the next loop observe it.
                    await asyncio.sleep(0)
                    continue

                started = time.perf_counter()
                try:
                    if processor is None:
                        raise RuntimeError(f"no processor registered for mode '{job.mode}'")
                    is_async = inspect.iscoroutinefunction(processor) or inspect.iscoroutinefunction(
                        getattr(processor, "__call__", None)
                    )
                    output = processor(item, current) if is_async else await asyncio.to_thread(processor, item, current)
                    if inspect.isawaitable(output):
                        output = await output
                    result = self._coerce_result(output, time.perf_counter() - started)
                    self.storage.update_item(
                        item.id,
                        result.status,
                        result=result.result,
                        error=result.error,
                        duration_ms=result.duration_ms,
                    )
                except asyncio.CancelledError:
                    # External shutdown leaves work resumable and marks the job
                    # interrupted; normal user cancellation is cooperative.
                    try:
                        self.storage.update_item(item.id, "pending", error="job interrupted")
                    except Exception:
                        pass
                    try:
                        current = self.storage.get_job(job_id)
                        if current and current.state in {"running", "cancelling"}:
                            self.storage.transition_job(job_id, "interrupted", phase="interrupted", error="worker cancelled")
                    finally:
                        raise
                except Exception as exc:
                    elapsed = (time.perf_counter() - started) * 1000
                    self.storage.update_item(item.id, "failed", error=str(exc), duration_ms=elapsed)
                await self._emit_latest(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            current = self.storage.get_job(job_id)
            if current and current.state in {"running", "cancelling"}:
                self.storage.transition_job(job_id, "failed", phase="failed", error=str(exc), force=True)
        finally:
            self._pause_events.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    async def _run_parallel(self, job_id: str, processor: ItemProcessor, workers: int) -> None:
        """Drain an online queue with bounded concurrent item workers."""

        is_async = inspect.iscoroutinefunction(processor) or inspect.iscoroutinefunction(
            getattr(processor, "__call__", None)
        )

        async def worker() -> None:
            while True:
                if self._cancel_events.setdefault(job_id, asyncio.Event()).is_set():
                    return
                current = self.storage.get_job(job_id)
                if current is None or current.state != "running":
                    if current is not None and current.state == "paused":
                        await self._pause_events.setdefault(job_id, asyncio.Event()).wait()
                        continue
                    return
                item = self.storage.claim_next_item(job_id)
                if item is None:
                    return
                started = time.perf_counter()
                try:
                    output = (
                        processor(item, current)
                        if is_async
                        else await asyncio.to_thread(processor, item, current)
                    )
                    if inspect.isawaitable(output):
                        output = await output
                    result = self._coerce_result(output, time.perf_counter() - started)
                    self.storage.update_item(
                        item.id,
                        result.status,
                        result=result.result,
                        error=result.error,
                        duration_ms=result.duration_ms,
                    )
                except asyncio.CancelledError:
                    try:
                        self.storage.update_item(item.id, "pending", error="job interrupted")
                    except Exception:
                        pass
                    raise
                except Exception as exc:
                    self.storage.update_item(
                        item.id,
                        "failed",
                        error=str(exc),
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                await self._emit_latest(job_id)

        await asyncio.gather(*(worker() for _ in range(max(1, workers))))
        current = self.storage.get_job(job_id)
        if current is None:
            return
        if current.state == "cancelling" or self._cancel_events.setdefault(
            job_id, asyncio.Event()
        ).is_set():
            self.storage.cancel_pending_items(job_id)
            if (current := self.storage.get_job(job_id)) and current.state == "cancelling":
                self.storage.transition_job(job_id, "cancelled", phase="cancelled")
        elif current.state == "running" and current.processed >= current.total:
            final = "failed" if current.failed else "succeeded"
            self.storage.transition_job(job_id, final, phase=final)

    async def _run_batched(
        self,
        job_id: str,
        processor: BatchItemProcessor,
        batch_size: int,
    ) -> None:
        """Process a finite local batch while retaining per-item persistence."""

        is_async = inspect.iscoroutinefunction(processor) or inspect.iscoroutinefunction(
            getattr(processor, "__call__", None)
        )
        while True:
            if self._cancel_events.setdefault(job_id, asyncio.Event()).is_set():
                self.storage.cancel_pending_items(job_id)
                current = self.storage.get_job(job_id)
                if current and current.state == "cancelling":
                    self.storage.transition_job(job_id, "cancelled", phase="cancelled")
                return
            current = self.storage.get_job(job_id)
            if current is None:
                return
            if current.state == "paused":
                await self._pause_events.setdefault(job_id, asyncio.Event()).wait()
                continue
            if current.state != "running":
                return

            items: list[JobItemRecord] = []
            for _ in range(batch_size):
                item = self.storage.claim_next_item(job_id)
                if item is None:
                    break
                items.append(item)
            if not items:
                current = self.storage.get_job(job_id)
                if current and current.processed >= current.total:
                    final = "failed" if current.failed else "succeeded"
                    self.storage.transition_job(job_id, final, phase=final)
                return

            started = time.perf_counter()
            try:
                outputs = (
                    processor(items, current)
                    if is_async
                    else await asyncio.to_thread(processor, items, current)
                )
                if inspect.isawaitable(outputs):
                    outputs = await outputs
                results = list(outputs)
                if len(results) != len(items):
                    raise ValueError("batch processor returned the wrong number of results")
                elapsed = time.perf_counter() - started
                for item, output in zip(items, results, strict=True):
                    result = self._coerce_result(output, elapsed / len(items))
                    self.storage.update_item(
                        item.id,
                        result.status,
                        result=result.result,
                        error=result.error,
                        duration_ms=result.duration_ms,
                    )
            except asyncio.CancelledError:
                for item in items:
                    try:
                        self.storage.update_item(item.id, "pending", error="job interrupted")
                    except Exception:
                        pass
                raise
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000 / len(items)
                for item in items:
                    self.storage.update_item(
                        item.id,
                        "failed",
                        error=str(exc),
                        duration_ms=elapsed_ms,
                    )
            await self._emit_latest(job_id)

    @staticmethod
    def _coerce_result(output: Any, elapsed: float) -> ProcessResult:
        if isinstance(output, ProcessResult):
            duration = output.duration_ms if output.duration_ms is not None else elapsed * 1000
            return ProcessResult(output.status, output.result, output.error, duration)
        if output is None:
            return ProcessResult(duration_ms=elapsed * 1000)
        if isinstance(output, Mapping):
            status = str(getattr(output.get("status", "succeeded"), "value", output.get("status", "succeeded"))).lower()
            if status not in {"succeeded", "skipped", "failed", "cancelled"}:
                raise ValueError(f"invalid processor result status: {status}")
            result = output.get("result")
            if result is None:
                result = {key: value for key, value in output.items() if key not in {"status", "error", "duration_ms"}}
            return ProcessResult(status, result if isinstance(result, Mapping) else {"value": result}, output.get("error"), float(output.get("duration_ms", elapsed * 1000)))
        return ProcessResult(result={"value": output}, duration_ms=elapsed * 1000)

    async def _emit_latest(self, job_id: str) -> None:
        if self.on_event is None:
            return
        event = self.storage.get_latest_event(job_id)
        if event is None:
            return
        data = {"seq": event.seq, **event.data}
        callback_result = self.on_event(data)
        if inspect.isawaitable(callback_result):
            await callback_result

    async def event_stream(
        self,
        job_id: str,
        *,
        last_event_id: int = 0,
        poll_seconds: float = 0.25,
    ):
        """Yield structured events for an SSE endpoint with Last-Event-ID."""

        sequence = max(0, int(last_event_id))
        while True:
            events = self.storage.get_events(job_id, after_seq=sequence)
            for event in events:
                sequence = event.seq
                yield {"seq": event.seq, "job_id": event.job_id, **event.data}
            record = self.storage.get_job(job_id)
            if record is None:
                return
            if record.state in {"succeeded", "failed", "cancelled", "interrupted"}:
                # A terminal transition and its event share one transaction.
                # Recheck after the state read to close the small race where
                # the first query ran immediately before that transaction.
                if self.storage.get_events(job_id, after_seq=sequence):
                    continue
                return
            await asyncio.sleep(max(0.05, min(5.0, float(poll_seconds))))

    async def shutdown(self) -> None:
        """Cancel workers and leave resumable jobs marked interrupted."""

        active_ids = list(self._tasks)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # A worker waiting on a pause event may not execute its cancellation
        # handler.  Persist the restart-safe state explicitly after all tasks
        # have stopped.
        for job_id in active_ids:
            record = self.storage.get_job(job_id)
            if record and record.state in {"running", "paused", "cancelling"}:
                try:
                    self.storage.transition_job(
                        job_id,
                        "interrupted",
                        phase="interrupted",
                        error="worker manager shut down",
                        force=True,
                    )
                except Exception:
                    pass

    def get_results(self, job_id: str, *, limit: int = 1000, offset: int = 0) -> list[JobItemRecord]:
        return self.storage.list_items(job_id, limit=limit, offset=offset)


JobService = JobManager


__all__ = [
    "BatchItemProcessor",
    "ItemProcessor",
    "JobManager",
    "JobService",
    "ProcessResult",
]
