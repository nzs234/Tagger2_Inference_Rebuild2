import json

import pytest

from tagger2.anima import (
    ANIMA_JSON_KEYS,
    extract_json_object,
    parse_anima_response,
    replace_anima_underscores,
)
from tagger2.artifacts import render_online_txt


def _payload(**overrides):
    value = {
        "quality": ["highres"],
        "count": "solo",
        "character": "",
        "series": "",
        "artist": "",
        "appearance": ["red fur"],
        "tags": ["digital art"],
        "environment": ["outdoors"],
        "nl": "A detailed caption.",
    }
    value.update(overrides)
    return value


def test_extracts_fenced_balanced_json():
    raw = "prefix\n```json\n" + json.dumps(_payload()) + "\n```\ntrailing prose"
    assert extract_json_object(raw)["count"] == "solo"


def test_normalizes_and_deduplicates_trigger():
    payload = parse_anima_response(
        json.dumps(_payload(appearance=["Fox", "fox", "@artist"], tags=["digital art", "FOX"])),
        trigger_artist="@artist",
    )
    assert payload.artist == "@artist"
    assert payload.appearance == ["Fox"]
    assert payload.tags == ["digital art"]


def test_rejects_missing_or_extra_fields():
    value = _payload()
    value.pop("nl")
    with pytest.raises(ValueError):
        parse_anima_response(json.dumps(value))
    value = _payload(extra="nope")
    with pytest.raises(ValueError):
        parse_anima_response(json.dumps(value))


def test_output_has_exact_schema_keys():
    payload = parse_anima_response(json.dumps(_payload()))
    assert tuple(payload.model_dump().keys()) == ANIMA_JSON_KEYS


def test_replaces_underscores_only_in_structured_anima_fields():
    payload = parse_anima_response(
        json.dumps(
            _payload(
                quality=["best_quality"],
                character="fox_girl",
                series="demo_series",
                appearance=["red_hair"],
                tags=["digital_art"],
                environment=["night_sky"],
                nl="Keep_this natural-language caption unchanged.",
            )
        ),
        trigger_artist="@demo_artist",
    )

    replaced = replace_anima_underscores(payload)

    assert replaced.quality == ["best quality"]
    assert replaced.character == "fox girl"
    assert replaced.series == "demo series"
    assert replaced.artist == "@demo artist"
    assert replaced.appearance == ["red hair"]
    assert replaced.tags == ["digital art"]
    assert replaced.environment == ["night sky"]
    assert replaced.nl == "Keep_this natural-language caption unchanged."


def test_online_txt_defaults_to_nl_and_can_append_tags():
    assert render_online_txt("A caption.", ["red hair", "portrait"]) == "A caption.\n"
    assert render_online_txt(
        "A caption.", ["red hair", "portrait"], include_tags=True
    ) == "A caption.\n\nred hair, portrait\n"
