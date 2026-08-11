from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from tagger2.config import AppConfig
from tagger2.main import create_app
from tagger2.video_prompts import (
    FL2VA_SYSTEM_PROMPT,
    FL2VAPromptPackage,
    VIDEO_PROMPT_SYSTEM_PROMPT,
    VideoPromptPackage,
    parse_fl2va_prompt_response,
    parse_video_prompt_response,
    resolve_h3_base_mode,
)


def _package() -> dict[str, object]:
    return {
        "change_summary_zh": "Created a stable H3 reference-generation prompt.",
        "subject_definitions": [
            {
                "subject_number": 1,
                "picture_number": 1,
                "zh": "reference image main subject, visible clothing, pose, and visual style",
                "en": "the reference image main subject, visible clothing, pose, and visual style",
            }
        ],
        "summary": {
            "zh": "[reference generation] Create one continuous, restrained motion shot.",
            "en": "[reference generation] Create one continuous, restrained motion shot.",
        },
        "retention_analysis": [
            {
                "subject_number": 1,
                "shot_number": 1,
                "visual_retention": "fully_preserved",
                "zh": "Keep the visible subject identity, clothing, composition, and lighting stable.",
                "en": "Keep the visible subject identity, clothing, composition, and lighting stable.",
            }
        ],
        "detailed_description": {
            "overview": {
                "zh": "a single continuous reference-generation video with restrained subject motion and a stable ending",
                "en": "a single continuous reference-generation video with restrained subject motion and a stable ending",
            },
            "shots": [
                {
                    "shot_number": 1,
                    "cut_time_seconds": None,
                    "zh": "The visible subject begins in the reference composition and makes a subtle natural movement.",
                    "en": "The visible subject begins in the reference composition and makes a subtle natural movement.",
                },
                {
                    "shot_number": 2,
                    "cut_time_seconds": 3.5,
                    "zh": "Maintain continuity while the camera finishes a restrained push-in and settles on a stable frame.",
                    "en": "Maintain continuity while the camera finishes a restrained push-in and settles on a stable frame.",
                },
            ],
        },
        "overall_soundscape": {
            "zh": "A quiet natural ambience with soft room tone and subtle movement sounds.",
            "en": "A quiet natural ambience with soft room tone and subtle movement sounds.",
        },
        "non_diegetic_music": {
            "zh": "A sparse, unobtrusive ambient score that stays below the physical soundscape.",
            "en": "A sparse, unobtrusive ambient score that stays below the physical soundscape.",
        },
        "assumptions_zh": [],
    }


def _fl2va_package() -> dict[str, object]:
    return {
        "change_summary_zh": "Created a continuous FL2VA opening-to-ending motion path.",
        "base_mode": "fl2va",
        "reference_alignment": {
            "zh": "参考图 1 对齐到目标视频 0.00 秒的首帧；参考图 2 对齐到目标视频 5.00 秒的末帧。",
            "en": "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; Picture 2 (from Shot 2) aligns with the 5.00-second mark of the target video.",
        },
        "integrated_multimodal_description": {
            "zh": "[Shot 1] 以参考图中的主体、构图和光线开始，镜头缓慢推进，主体完成一个连贯动作。\n[Shot 2] At 00:03.500, 镜头保持连续性并在稳定的结束状态停住。",
            "en": "[Shot 1] Live-action, cinematic, the visible subject begins in the composition established by Picture 1 while preserving identity, clothing, props, lighting, and spatial relationships. The camera pushes in with small amplitude at slow speed as the subject performs one coherent action and moves toward the requested ending state.\n[Shot 2] At 00:03.500, the camera maintains continuity as the action settles into a stable final composition and holds the ending state.",
        },
        "overall_soundscape": {
            "zh": "安静的环境底噪持续，动作带来轻微的衣料和脚步声。",
            "en": "Quiet room ambience continues underneath subtle fabric movement and soft footsteps.",
        },
        "non_diegetic_music": {
            "zh": "无非叙事音乐。",
            "en": "N/A",
        },
        "assumptions_zh": [],
    }


def _image() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (16, 10), "navy").save(stream, format="PNG")
    return stream.getvalue()


def _client(tmp_path: Path) -> TestClient:
    settings = AppConfig(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        production=True,
    )
    return TestClient(create_app(settings))


def test_video_prompt_parser_extracts_a_strict_h3_package() -> None:
    valid = _package()
    parsed = parse_video_prompt_response(json.dumps(valid))
    assert parsed.summary.en.startswith("[reference generation]")
    assert parsed.detailed_description.shots[1].cut_time_seconds == 3.5

    wrapped = f"Model response: {json.dumps(valid)}\nThanks"
    assert parse_video_prompt_response(wrapped).subject_definitions[0].picture_number == 1

    extra = dict(valid, unexpected=True)
    with pytest.raises(ValueError):
        VideoPromptPackage.model_validate(extra)


def test_video_prompt_parser_requires_h3_bilingual_content_and_references() -> None:
    blank = _package()
    blank["summary"] = {"zh": "[reference generation] valid", "en": ""}
    with pytest.raises(ValueError):
        VideoPromptPackage.model_validate(blank)

    invalid_picture = _package()
    invalid_picture["subject_definitions"] = [{
        "subject_number": 1,
        "picture_number": 10,
        "zh": "a subject",
        "en": "a subject",
    }]
    with pytest.raises(ValueError):
        VideoPromptPackage.model_validate(invalid_picture)

    unknown_reference = _package()
    unknown_reference["retention_analysis"] = [{
        "subject_number": 2,
        "shot_number": 1,
        "visual_retention": "fully_preserved",
        "zh": "Keep it stable.",
        "en": "Keep it stable.",
    }]
    with pytest.raises(ValueError, match="unknown subject"):
        VideoPromptPackage.model_validate(unknown_reference)

    invalid_summary = _package()
    invalid_summary["summary"] = {"zh": "Reference generation", "en": "Reference generation"}
    with pytest.raises(ValueError, match="bracketed H3 task type"):
        VideoPromptPackage.model_validate(invalid_summary)


def test_ref2va_rejects_picture_labels_outside_uploaded_reference_set() -> None:
    two_picture_package = _package()
    two_picture_package["subject_definitions"] = [{
        "subject_number": 1,
        "picture_number": 2,
        "zh": "second reference image subject",
        "en": "the second reference image subject",
    }]

    assert parse_video_prompt_response(
        json.dumps(two_picture_package), reference_image_count=2,
    ).subject_definitions[0].picture_number == 2
    with pytest.raises(ValueError, match="was not uploaded"):
        parse_video_prompt_response(json.dumps(two_picture_package), reference_image_count=1)


def test_h3_base_mode_follows_official_zero_to_two_image_rules() -> None:
    assert resolve_h3_base_mode(0) == "t2va"
    assert resolve_h3_base_mode(1, "first") == "i2va"
    assert resolve_h3_base_mode(1, "last") == "l2va"
    assert resolve_h3_base_mode(2) == "fl2va"
    with pytest.raises(ValueError, match="at most two"):
        resolve_h3_base_mode(3)


def test_video_prompt_parser_requires_connected_h3_shot_timing() -> None:
    first_shot_timestamp = _package()
    first_shot_timestamp["detailed_description"] = {
        "overview": {"zh": "Overview", "en": "Overview"},
        "shots": [{
            "shot_number": 1,
            "cut_time_seconds": 0.5,
            "zh": "First shot.",
            "en": "First shot.",
        }],
    }
    with pytest.raises(ValueError, match="Shot 1 must not have"):
        VideoPromptPackage.model_validate(first_shot_timestamp)

    missing_later_timestamp = _package()
    missing_later_timestamp["detailed_description"] = {
        "overview": {"zh": "Overview", "en": "Overview"},
        "shots": [
            {"shot_number": 1, "cut_time_seconds": None, "zh": "First shot.", "en": "First shot."},
            {"shot_number": 2, "cut_time_seconds": None, "zh": "Second shot.", "en": "Second shot."},
        ],
    }
    with pytest.raises(ValueError, match="later shots require"):
        VideoPromptPackage.model_validate(missing_later_timestamp)

    reversed_cuts = _package()
    reversed_cuts["detailed_description"] = {
        "overview": {"zh": "Overview", "en": "Overview"},
        "shots": [
            {"shot_number": 1, "cut_time_seconds": None, "zh": "First shot.", "en": "First shot."},
            {"shot_number": 2, "cut_time_seconds": 4.0, "zh": "Second shot.", "en": "Second shot."},
            {"shot_number": 3, "cut_time_seconds": 3.5, "zh": "Third shot.", "en": "Third shot."},
        ],
    }
    with pytest.raises(ValueError, match="strictly increasing"):
        VideoPromptPackage.model_validate(reversed_cuts)


def test_fl2va_parser_uses_base_guide_fields_and_rejects_extra_keys() -> None:
    valid = _fl2va_package()
    parsed = parse_fl2va_prompt_response(json.dumps(valid))
    assert parsed.integrated_multimodal_description.en.startswith("[Shot 1]")
    assert "integrated_multimodal_description" in FL2VA_SYSTEM_PROMPT

    extra = dict(valid, summary={"zh": "unexpected", "en": "unexpected"})
    with pytest.raises(ValueError):
        FL2VAPromptPackage.model_validate(extra)

    missing_alignment = _fl2va_package()
    missing_alignment["reference_alignment"] = {"zh": "start", "en": "Picture 1 starts here"}
    with pytest.raises(ValueError, match="alignment instruction"):
        FL2VAPromptPackage.model_validate(missing_alignment)


def test_video_prompt_endpoint_is_stateless_and_uses_current_package(tmp_path: Path) -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[list[bytes], str, dict[str, object]]] = []

        async def generate(self, image, prompt, **kwargs):
            self.calls.append((list(image), prompt, kwargs))
            package = _fl2va_package() if str(kwargs.get("system_prompt", "")).startswith(FL2VA_SYSTEM_PROMPT) else _package()
            return json.dumps(package)

    fake = FakeProvider()
    with _client(tmp_path) as client:
        runtime = client.app.state.runtime
        runtime.provider = lambda provider_id: fake
        response = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "instruction": "Use a slow camera push-in."},
            files={"image": ("frame.png", _image(), "image/png")},
        )
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["en"].startswith("[reference generation]")
        assert runtime.storage.list_jobs() == []
        assert not [path for path in (tmp_path / "data" / "uploads").rglob("*") if path.is_file()]

        follow_up = client.post(
            "/api/v1/video-prompts/generate",
            data={
                "provider_id": "gemini",
                "provider_model": "vision-pro",
                "instruction": "Make the camera static.",
                "current_package_json": json.dumps(response.json()),
            },
            files={"image": ("frame.png", _image(), "image/png")},
        )
        assert follow_up.status_code == 200, follow_up.text

    assert len(fake.calls) == 2
    assert fake.calls[0][0] == [_image()]
    assert "LATEST_USER_INSTRUCTION:" in fake.calls[0][1]
    assert "CURRENT_PROMPT_PACKAGE:" not in fake.calls[0][1]
    assert "CURRENT_PROMPT_PACKAGE:" in fake.calls[1][1]
    assert fake.calls[1][2]["model"] == "vision-pro"
    assert str(fake.calls[1][2]["system_prompt"]).startswith(VIDEO_PROMPT_SYSTEM_PROMPT)
    assert callable(fake.calls[1][2]["validator"])


def test_video_prompt_endpoint_selects_fl2va_preset_and_baseline(tmp_path: Path) -> None:
    class FakeProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def generate(self, _image, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return json.dumps(_fl2va_package())

    fake = FakeProvider()
    with _client(tmp_path) as client:
        client.app.state.runtime.provider = lambda _provider_id: fake
        response = client.post(
            "/api/v1/video-prompts/generate",
            data={
                "provider_id": "gemini",
                "prompt_mode": "fl2va",
                "instruction": "Reach a calm final pose.",
            },
            files=[
                ("images", ("first.png", _image(), "image/png")),
                ("images", ("last.png", _image(), "image/png")),
            ],
        )
        assert response.status_code == 200, response.text
        assert response.json()["reference_alignment"]["en"].startswith("How the reference pictures align")

        follow_up = client.post(
            "/api/v1/video-prompts/generate",
            data={
                "provider_id": "gemini",
                "prompt_mode": "fl2va",
                "instruction": "Slow the ending.",
                "current_package_json": json.dumps(response.json()),
            },
            files=[
                ("images", ("first.png", _image(), "image/png")),
                ("images", ("last.png", _image(), "image/png")),
            ],
        )
        assert follow_up.status_code == 200, follow_up.text

    assert len(fake.calls) == 2
    assert "CURRENT_PROMPT_PACKAGE:" in fake.calls[1][0]
    assert str(fake.calls[0][1]["system_prompt"]).startswith(FL2VA_SYSTEM_PROMPT)
    assert "H3_BASE_MODE: fl2va" in str(fake.calls[0][1]["system_prompt"])
    assert callable(fake.calls[0][1]["validator"])


def test_video_prompt_endpoint_rejects_invalid_current_package(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.post(
            "/api/v1/video-prompts/generate",
            data={
                "provider_id": "gemini",
                "instruction": "Generate one shot.",
                "current_package_json": "{not-json",
            },
            files={"image": ("frame.png", _image(), "image/png")},
        )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_current_package"
    assert response.json()["fields"]


def test_video_prompt_endpoint_enforces_request_limits_and_provider_existence(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        invalid_mode = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "prompt_mode": "unknown", "instruction": "Generate a motion plan"},
            files={"image": ("frame.png", _image(), "image/png")},
        )
        assert invalid_mode.status_code == 400
        assert invalid_mode.json()["code"] == "invalid_prompt_mode"

        invalid_provider = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "missing", "instruction": "Generate a motion plan"},
            files={"image": ("frame.png", _image(), "image/png")},
        )
        assert invalid_provider.status_code == 404
        assert invalid_provider.json()["code"] == "provider_not_found"

        too_long = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "instruction": "x" * 8_001},
            files={"image": ("frame.png", _image(), "image/png")},
        )
        assert too_long.status_code == 400
        assert too_long.json()["code"] == "instruction_too_long"

        invalid_image = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "instruction": "Generate a motion plan"},
            files={"image": ("frame.txt", b"not an image", "text/plain")},
        )
        assert invalid_image.status_code == 400

        missing_ref2va_images = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "instruction": "Generate a motion plan"},
        )
        assert missing_ref2va_images.status_code == 400
        assert missing_ref2va_images.json()["code"] == "invalid_reference_image_count"

        too_many_ref2va_images = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "instruction": "Generate a motion plan"},
            files=[("images", (f"frame-{index}.png", _image(), "image/png")) for index in range(10)],
        )
        assert too_many_ref2va_images.status_code == 400
        assert too_many_ref2va_images.json()["code"] == "invalid_reference_image_count"

        too_many_fl2va_images = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "prompt_mode": "fl2va", "instruction": "Generate a motion plan"},
            files=[("images", (f"frame-{index}.png", _image(), "image/png")) for index in range(3)],
        )
        assert too_many_fl2va_images.status_code == 400
        assert too_many_fl2va_images.json()["code"] == "invalid_reference_image_count"


def test_video_prompt_endpoint_returns_structured_502_for_invalid_model_output(tmp_path: Path) -> None:
    class InvalidProvider:
        async def generate(self, *_args, **_kwargs):
            return '{"not": "a video prompt package"}'

    with _client(tmp_path) as client:
        client.app.state.runtime.provider = lambda _provider_id: InvalidProvider()
        response = client.post(
            "/api/v1/video-prompts/generate",
            data={"provider_id": "gemini", "instruction": "Generate a motion plan"},
            files={"image": ("frame.png", _image(), "image/png")},
        )

    assert response.status_code == 502
    assert response.json()["code"] == "provider_invalid_video_prompt"
