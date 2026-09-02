"""Tests for the ProviderNlAdapter event-loop ownership and the NL stage batch path."""

import asyncio
import json

import pytest


def _completion(content: str, finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "cmpl-1",
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    ).encode("utf-8")


def _structured(nl: str) -> bytes:
    return _completion(
        json.dumps(
            {"nl": nl, "count": "solo", "layout": "single_scene", "sameCharacterRepeated": False}
        )
    )


class Sample:
    def __init__(self, path, nl=""):
        self.relative_image_path = path
        self.nl = nl
        self.annotation_kind = "none"
        self.skip_caption = False


class FakeProvider:
    """Stands in for VisionProvider; records loop identity and inflight calls."""

    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.loops = []
        self.calls = 0
        self.closed = 0
        self.inflight = 0
        self.max_inflight = 0

    async def generate(self, image, prompt, *, model=None, system_prompt=None):
        self.calls += 1
        self.loops.append(asyncio.get_running_loop())
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        await asyncio.sleep(0.01)
        self.inflight -= 1
        if any(marker in prompt for marker in self.fail_on):
            raise RuntimeError("endpoint unreachable")
        return "caption text"

    async def aclose(self):
        self.closed += 1


def _request(path, payload=None):
    from backend.tagger2.workflow.stages.nl import NlRequest

    return NlRequest(
        relative_image_path=path,
        system_prompt="system",
        payload=payload or {"tags": [path]},
        image_path=None,
    )


def test_adapter_reuses_one_dedicated_event_loop_across_calls():
    """A fresh loop per request would strand pooled connections; one loop is mandatory."""
    from backend.tagger2.workflow.nl_adapter import ProviderNlAdapter

    provider = FakeProvider()
    adapter = ProviderNlAdapter(provider)

    adapter.complete(_request("a.png"))
    adapter.complete(_request("b.png"))

    assert provider.calls == 2
    assert provider.loops[0] is provider.loops[1]
    adapter.close()


def test_adapter_close_drains_provider_once_and_is_idempotent():
    from backend.tagger2.workflow.nl_adapter import ProviderNlAdapter

    provider = FakeProvider()
    adapter = ProviderNlAdapter(provider)
    adapter.complete(_request("a.png"))
    loop = adapter._loop

    adapter.close()
    adapter.close()

    assert provider.closed == 1
    assert loop.is_closed()
    assert adapter._loop is None
    with pytest.raises(RuntimeError):
        adapter.complete(_request("a.png"))


def test_adapter_complete_many_preserves_order_and_captures_errors():
    from backend.tagger2.workflow.nl_adapter import ProviderNlAdapter

    provider = FakeProvider(fail_on={"bad"})
    adapter = ProviderNlAdapter(provider)

    outcomes = adapter.complete_many(
        [_request("a"), _request("bad"), _request("c")], concurrency=2
    )

    assert [status for status, _ in outcomes] == ["ok", "error", "ok"]
    assert outcomes[0][1].startswith(b'{"choices"')
    assert isinstance(outcomes[1][1], RuntimeError)
    adapter.close()


def test_adapter_complete_many_respects_concurrency_bound():
    from backend.tagger2.workflow.nl_adapter import ProviderNlAdapter

    provider = FakeProvider()
    adapter = ProviderNlAdapter(provider)

    outcomes = adapter.complete_many([_request(f"p{i}") for i in range(8)], concurrency=3)

    assert all(status == "ok" for status, _ in outcomes)
    assert provider.max_inflight <= 3
    adapter.close()


def test_adapter_close_without_use_skips_loop_creation():
    from backend.tagger2.workflow.nl_adapter import ProviderNlAdapter

    provider = FakeProvider()
    adapter = ProviderNlAdapter(provider)
    adapter.close()

    assert provider.closed == 0
    assert adapter._loop is None


class FakeBatchClient:
    """Duck-typed NlClient exposing complete_many like the real adapter."""

    def __init__(self):
        self.batch_calls = 0

    def complete(self, request):
        if request.relative_image_path == "bad.png":
            raise RuntimeError("endpoint unreachable")
        return _structured("A wolf stands in snow.")

    def complete_many(self, requests, *, concurrency=1):
        self.batch_calls += 1
        outcomes = []
        for request in requests:
            if request.relative_image_path == "bad.png":
                outcomes.append(("error", RuntimeError("endpoint unreachable")))
            else:
                outcomes.append(("ok", _structured("A wolf stands in snow.")))
        return outcomes


def test_nl_stage_batch_path_matches_sequential_semantics():
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    samples = [Sample("a.png"), Sample("bad.png"), Sample("c.png")]
    projections = {f"{p}.png": {} for p in ("a", "bad", "c")}

    sequential = run_nl_stage(
        samples, dict(projections), source_root=".", client=FakeBatchClient(), use_image=False
    )
    batch = run_nl_stage(
        samples,
        dict(projections),
        source_root=".",
        client=FakeBatchClient(),
        use_image=False,
        concurrency=4,
    )

    assert sequential.generated == batch.generated == 2
    assert sequential.failed == batch.failed == 1
    assert [r.error for r in batch.results if r.error] == [
        "NL request failed: endpoint unreachable"
    ]
    assert [r.nl for r in batch.results if r.ok] == ["A wolf stands in snow."] * 2


def test_nl_stage_batch_path_only_used_when_client_supports_it():
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    class PlainClient:
        def __init__(self):
            self.calls = 0

        def complete(self, request):
            self.calls += 1
            return _structured("A wolf stands in snow.")

    client = PlainClient()
    report = run_nl_stage(
        [Sample("a.png"), Sample("b.png")],
        {"a.png": {}, "b.png": {}},
        source_root=".",
        client=client,
        use_image=False,
        concurrency=4,
    )

    assert client.calls == 2
    assert report.generated == 2


def test_nl_concurrency_contract_validation():
    from backend.tagger2.workflow.contracts import _validate_section

    _validate_section("nl", {"concurrency": 4})
    _validate_section("nl", {"concurrency": 1})
    _validate_section("nl", {"concurrency": 32})
    with pytest.raises(ValueError):
        _validate_section("nl", {"concurrency": 0})
    with pytest.raises(ValueError):
        _validate_section("nl", {"concurrency": 33})
    with pytest.raises(ValueError):
        _validate_section("nl", {"concurrency": "many"})
    with pytest.raises(ValueError):
        _validate_section("nl", {"concurrency": True})
