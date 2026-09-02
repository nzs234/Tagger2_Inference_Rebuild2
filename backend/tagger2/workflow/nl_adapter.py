"""Adapter to use Tagger2 VisionProvider as NlClient."""

import asyncio
import json
import threading
from collections.abc import Sequence

from ..providers.client import VisionProvider
from .stages.nl import NlRequest


class ProviderNlAdapter:
    """Wraps an async VisionProvider to provide sync NlClient interface.

    A ``VisionProvider`` owns an ``httpx.AsyncClient`` and an ``asyncio.Semaphore``
    that must be used from exactly one event loop.  The adapter therefore owns a
    single dedicated loop created lazily on first use and reused for every
    request: a fresh loop per request strands pooled keepalive connections on a
    closed loop and leaks the provider.  ``close()`` drains the provider on that
    loop and must run once the owning pipeline call finishes, including on
    failure.
    """

    def __init__(self, provider: VisionProvider, *, model: str | None = None):
        self._provider = provider
        self._model = model.strip() if isinstance(model, str) and model.strip() else None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._closed = False

    def complete(self, request: NlRequest) -> bytes:
        """Synchronously call the async provider and return raw JSON bytes.

        The NL stage expects a raw bytes response that it will parse itself.
        This adapter converts NlRequest (which has system_prompt, payload, image_path)
        into the provider's generate call and returns the result as JSON bytes.
        """
        status, value = self.complete_many([request], concurrency=1)[0]
        if isinstance(value, Exception):
            raise value
        return value

    def complete_many(
        self,
        requests: Sequence[NlRequest],
        *,
        concurrency: int = 4,
    ) -> list[tuple[str, "bytes | Exception"]]:
        """Run a batch of requests concurrently on the adapter's dedicated loop.

        Returns one ``(status, value)`` outcome per request, in request order:
        ``("ok", response_bytes)`` or ``("error", exception)``.  Failures are
        captured per item so one bad response cannot discard the rest of the
        batch; the stage maps them to per-sample results.
        """
        if not requests:
            return []
        if self._closed:
            raise RuntimeError("NL adapter is already closed")
        prepared = [(request, self._image_argument(request)) for request in requests]
        return self._run(self._gather(prepared, max(1, int(concurrency))))

    def close(self) -> None:
        """Close the wrapped provider and the dedicated loop.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            self._loop = None
        if loop is None:
            return
        try:
            loop.run_until_complete(self._provider.aclose())
        except Exception:  # noqa: BLE001, S110 - closing must not mask the stage result
            pass
        finally:
            loop.close()

    def _image_argument(self, request: NlRequest) -> str | None:
        # Keep the frozen prompt in the provider's system channel and send the
        # sample projection as data in the user channel.  The model override is
        # part of the immutable NL contract; an empty value deliberately keeps
        # the provider profile's primary/fallback model selection.
        if request.image_path and request.image_path.is_file():
            return str(request.image_path)
        return None

    async def _gather(
        self,
        prepared: list[tuple[NlRequest, str | None]],
        concurrency: int,
    ) -> list[tuple[str, "bytes | Exception"]]:
        semaphore = asyncio.Semaphore(concurrency)

        async def one(request: NlRequest, image: str | None) -> tuple[str, "bytes | Exception"]:
            async with semaphore:
                try:
                    return ("ok", await self._generate(request, image))
                except Exception as exc:  # noqa: BLE001 - captured per item on purpose
                    return ("error", exc)

        return list(
            await asyncio.gather(*(one(request, image) for request, image in prepared))
        )

    async def _generate(self, request: NlRequest, image: str | None) -> bytes:
        user_prompt = json.dumps(request.payload, ensure_ascii=False, sort_keys=True)
        result = await self._provider.generate(
            image=image,
            prompt=user_prompt,
            model=self._model,
            system_prompt=request.system_prompt,
        )
        # Return as JSON bytes in OpenAI-compatible format
        # The NL stage will parse this and extract choices[0].message.content
        response_data = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": result,
                    }
                }
            ]
        }
        return json.dumps(response_data).encode("utf-8")

    def _run(self, coro):
        # The NL stage runs in worker threads, so there is no active event
        # loop; the dedicated loop is created once and reused.  The lock keeps
        # concurrent callers from driving the same loop at the same time.
        with self._lock:
            loop = self._loop
            if loop is None:
                loop = asyncio.new_event_loop()
                self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                asyncio.set_event_loop(None)
