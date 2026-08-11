"""Tests for the NL stage: frozen prompt assembly and strict response handling."""

import json
import tempfile
from pathlib import Path

import pytest
from PIL import Image


def _completion(content: str, finish_reason: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "cmpl-1",
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }
    ).encode("utf-8")


def _structured(nl: str, count="solo", layout="single_scene", repeated=False) -> bytes:
    return _completion(
        json.dumps(
            {"nl": nl, "count": count, "layout": layout, "sameCharacterRepeated": repeated}
        )
    )


class FakeClient:
    def __init__(self, responses=None, default=None, fail_on=()):
        self.responses = responses or {}
        self.default = default or _structured("A wolf stands in snow.")
        self.fail_on = set(fail_on)
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        if request.relative_image_path in self.fail_on:
            raise RuntimeError("endpoint unreachable")
        return self.responses.get(request.relative_image_path, self.default)


class Sample:
    def __init__(self, path, nl=""):
        self.relative_image_path = path
        self.nl = nl
        self.annotation_kind = "none"
        self.skip_caption = False


def test_frozen_prompt_layers_are_assembled_in_order():
    """Base rules, preset, then length budget, from the ported fragment files."""
    from backend.tagger2.workflow.stages.nl import build_system_prompt

    prompt = build_system_prompt("general", "medium")
    assert "exactly these keys: nl, count, layout, sameCharacterRepeated" in prompt
    assert "untrusted data" in prompt
    assert "exactly 4-5 sentences" in prompt
    # Order: base before preset before length.
    assert prompt.index("exactly these keys") < prompt.index("Describe all observable content")
    assert prompt.index("Describe all observable content") < prompt.index("exactly 4-5 sentences")


def test_prompt_presets_and_lengths_are_distinct():
    """Each preset and length maps to its own frozen fragment."""
    from backend.tagger2.workflow.stages.nl import LENGTHS, PRESETS, build_system_prompt

    assert PRESETS == ("general", "style", "character")
    assert LENGTHS == ("short", "medium", "long")

    style = build_system_prompt("style", "short")
    assert "Do not describe artist, style, medium, rendering, quality, lighting" in style
    assert "exactly 2-3 sentences" in style

    character = build_system_prompt("character", "long")
    assert "structured primaryCharacterName" in character
    assert "exactly 6-8 sentences" in character


def test_prompt_rejects_unknown_preset_or_length():
    from backend.tagger2.workflow.stages.nl import NlError, build_system_prompt

    with pytest.raises(NlError):
        build_system_prompt("nope", "medium")
    with pytest.raises(NlError):
        build_system_prompt("general", "epic")


def test_nl_stage_reuses_existing_caption():
    """An existing NL is preserved rather than silently regenerated."""
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        client = FakeClient()
        samples = [Sample("a.png", nl="A human wrote this.")]

        report = run_nl_stage(
            samples,
            {"a.png": {"nl": "A human wrote this.", "tags": ["male"]}},
            source_root=root,
            client=client,
            reuse_original_nl=True,
        )

        assert report.reused == 1
        assert report.generated == 0
        assert client.requests == []
        assert report.by_path()["a.png"].nl == "A human wrote this."


def test_nl_stage_generates_and_captures_observation():
    """A structured response yields NL plus the count observation."""
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        Image.new("RGB", (8, 8)).save(root / "a.png")

        report = run_nl_stage(
            [Sample("a.png")],
            {"a.png": {"tags": ["male"], "character": "rex, other"}},
            source_root=root,
            client=FakeClient(default=_structured("A wolf stands.", count="duo")),
            reuse_original_nl=True,
        )

        assert report.generated == 1
        result = report.by_path()["a.png"]
        assert result.nl == "A wolf stands."
        assert result.observation["status"] == "observed"
        assert result.observation["countValue"] == "duo"


def test_nl_stage_rejects_model_refusal():
    """A refusal is a failure, never written as a caption."""
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        report = run_nl_stage(
            [Sample("a.png")],
            {"a.png": {"tags": []}},
            source_root=root,
            client=FakeClient(default=_structured("I cannot analyze images.")),
            use_image=False,
        )
        assert report.failed == 1
        assert report.generated == 0
        assert report.by_path()["a.png"].nl == ""


def test_nl_stage_rejects_truncated_response():
    """A truncated completion is refused rather than stored half-written."""
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        report = run_nl_stage(
            [Sample("a.png")],
            {"a.png": {"tags": []}},
            source_root=Path(tmpdir),
            client=FakeClient(default=_completion("half a sentence", finish_reason="length")),
            use_image=False,
        )
        assert report.failed == 1


def test_nl_stage_marks_unstructured_response_as_not_requested():
    """A bare caption is accepted, but its observation is not trusted."""
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        report = run_nl_stage(
            [Sample("a.png")],
            {"a.png": {"tags": []}},
            source_root=Path(tmpdir),
            client=FakeClient(default=_completion("A wolf stands in snow.")),
            use_image=False,
        )
        result = report.by_path()["a.png"]
        assert result.nl == "A wolf stands in snow."
        assert result.observation["status"] == "not_requested"
        assert result.observation["notRequestedReason"] == "unstructured_response"


def test_nl_stage_strips_code_fences_and_labels():
    """Wrapped output from weak prompts is unwrapped by the ported validator."""
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        report = run_nl_stage(
            [Sample("a.png")],
            {"a.png": {"tags": []}},
            source_root=Path(tmpdir),
            client=FakeClient(default=_completion("```\nCaption: A wolf stands.\n```")),
            use_image=False,
        )
        assert report.by_path()["a.png"].nl == "A wolf stands."


def test_nl_stage_records_transport_failure_per_sample():
    from backend.tagger2.workflow.stages.nl import run_nl_stage

    with tempfile.TemporaryDirectory() as tmpdir:
        report = run_nl_stage(
            [Sample("a.png"), Sample("b.png")],
            {"a.png": {"tags": []}, "b.png": {"tags": []}},
            source_root=Path(tmpdir),
            client=FakeClient(fail_on={"a.png"}),
            use_image=False,
        )
        assert report.failed == 1
        assert report.generated == 1
        assert "unreachable" in report.by_path()["a.png"].error


def test_payload_carries_only_declared_fields():
    """The untrusted payload exposes tags or business JSON, plus primary character."""
    from backend.tagger2.workflow.stages.nl import build_payload

    projection = {
        "quality": [],
        "count": "solo",
        "character": "rex, sidekick",
        "series": "",
        "artist": "studio",
        "appearance": ["blue_fur"],
        "tags": ["male"],
        "environment": ["forest"],
        "nl": "",
    }

    lean = build_payload(projection, use_full_json=False)
    assert set(lean) == {"tags", "primaryCharacterName"}
    assert lean["primaryCharacterName"] == "rex"

    full = build_payload(projection, use_full_json=True)
    assert set(full) == {"businessJson", "primaryCharacterName"}
    assert "nl" not in full["businessJson"]


def test_openai_request_attaches_image_as_data_url():
    """The image is inlined so no server path is sent to a third party."""
    from backend.tagger2.workflow.stages.nl import (
        NlRequest,
        build_system_prompt,
        encode_openai_request,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        image = Path(tmpdir) / "a.png"
        Image.new("RGB", (4, 4)).save(image)

        request = NlRequest(
            relative_image_path="a.png",
            system_prompt=build_system_prompt("general", "short"),
            payload={"tags": ["male"]},
            image_path=image,
        )
        body = encode_openai_request(request, model="gpt-4o-mini")

        assert body["model"] == "gpt-4o-mini"
        assert body["messages"][0]["role"] == "system"
        parts = body["messages"][1]["content"]
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert tmpdir not in json.dumps(body)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
