"""Strict validation of WorkflowJobConfigV1 payloads (plan stage 1).

Before this contract existed, a typo such as ``enabledd`` or an out-of-range
probability was accepted and silently changed how a job ran. These tests pin the
rejection so a regression cannot pass unnoticed.
"""

import pytest
from tagger2.workflow.contracts import WorkflowJobConfigV1

BASE = {
    "profile": "e621",
    "work_mode": "in_place",
    "overwrite_mode": "incremental",
    "source_root": {"root_id": "in", "relative_path": "."},
}


def _build(**patch):
    return WorkflowJobConfigV1.from_payload({**BASE, **patch})


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"caption": {"enabledd": True}}, "unknown caption fields"),
        ({"policy": {"seedd": "x"}}, "unknown policy fields"),
        ({"caption": {"enabled": "yes"}}, "must be true or false"),
        ({"caption": {"enabled": 1}}, "must be true or false"),
        ({"recursive": "yes"}, "must be true or false"),
        ({"policy": {"artist_dropout": 5.0}}, "between 0.0 and 1.0"),
        ({"policy": {"artist_dropout": -1}}, "between 0.0 and 1.0"),
        ({"ocr": {"min_confidence": 99}}, "between 0.0 and 1.0"),
        ({"token_budget": {"max_tokens": 0}}, "between 1 and 32768"),
        ({"token_budget": {"max_tokens": "many"}}, "must be an integer"),
        ({"token_budget": {"max_tokens": True}}, "must be an integer"),
        ({"export": {"format": "yaml"}}, "must be one of"),
        ({"nl": {"length": "epic"}}, "must be one of"),
        ({"caption": {"input_txt_mode": "prose"}}, "must be one of"),
        ({"profile": "gelbooru"}, "profile must be one of"),
        ({"work_mode": "symlink"}, "work_mode must be one of"),
        ({"overwrite_mode": "append"}, "overwrite_mode must be one of"),
        ({"policy": [1, 2]}, "must be an object"),
        ({"nonexistent_top_level": 1}, "unknown job config fields"),
    ],
)
def test_invalid_config_is_rejected(patch, expected):
    with pytest.raises(ValueError, match=expected):
        _build(**patch)


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"caption": {"enabled": False}},
        {"policy": {"artist_dropout": 0.0}},
        {"policy": {"artist_dropout": 1.0}},
        {"token_budget": {"max_tokens": 512}},
        {"caption": {"resource_id": None}},
        {"export": {"format": "both"}},
        {"nl": {"length": "short"}},
    ],
)
def test_valid_config_is_accepted(patch):
    assert _build(**patch).schema_version == 1


def test_partial_section_keeps_other_defaults():
    """Overriding one key must not drop the rest of the section."""
    default = _build()
    config = _build(caption={"enabled": False})

    assert config.caption["enabled"] is False
    assert set(config.caption) == set(default.caption)
    assert config.caption["input_txt_mode"] == default.caption["input_txt_mode"]


def test_config_hash_is_stable_and_sensitive():
    assert _build().config_hash() == _build().config_hash()
    assert _build().config_hash() != _build(caption={"enabled": False}).config_hash()


def test_unsupported_schema_version_is_rejected():
    with pytest.raises(ValueError, match="schema_version"):
        _build(schema_version=99)
