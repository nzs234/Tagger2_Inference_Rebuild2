"""Tests for the ported replacement index reader and nine-field caption format."""

import tempfile
from pathlib import Path

import pytest


def _write(path: Path, rows: str) -> Path:
    path.write_text(
        "source_tag,canonical_e621_tag,action,replacement_tags\n" + rows,
        encoding="utf-8",
    )
    return path


def test_pass_action_is_identity_passthrough():
    """`pass` rows validate but are omitted from the executable rule table."""
    from backend.tagger2.workflow.replacement_index import (
        load_replacement_rules,
        validate_replacement_index,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write(
            Path(tmpdir) / "index.csv",
            "male,male,pass,male\n"
            "anthro,anthro,replace,furry\n"
            "junk,junk,drop,\n",
        )

        report = validate_replacement_index(path)
        assert report.valid is True, report.errors
        assert report.passthrough_count == 1
        assert report.rule_count == 2
        assert report.action_counts["pass"] == 1

        rules = load_replacement_rules(path)
        assert "male" not in rules
        assert set(rules) == {"anthro", "junk"}


def test_pass_action_must_repeat_source_tag():
    """A `pass` row that rewrites its tag is an error, never a silent rewrite."""
    from backend.tagger2.workflow.replacement_index import (
        ReplacementIndexError,
        load_replacement_rules,
        validate_replacement_index,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write(Path(tmpdir) / "index.csv", "male,male,pass,female\n")

        report = validate_replacement_index(path)
        assert report.valid is False
        assert "line 2" in report.errors[0]

        with pytest.raises(ReplacementIndexError):
            load_replacement_rules(path)


def test_whitespace_only_source_tag_is_valid_junk_tag():
    """U+3000 style junk tags carry real drop rules and must be accepted."""
    from backend.tagger2.workflow.replacement_index import (
        load_replacement_rules,
        validate_replacement_index,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write(
            Path(tmpdir) / "index.csv",
            "\u3000,\u3000,drop,\n"
            " padded ,padded,keep,padded\n",
        )

        report = validate_replacement_index(path)
        assert report.valid is False
        assert len(report.errors) == 1
        assert "padded" in report.errors[0]

        good = _write(Path(tmpdir) / "good.csv", "\u3000,\u3000,drop,\n")
        assert validate_replacement_index(good).valid is True
        assert "\u3000" in load_replacement_rules(good)


def test_replace_projection_applies_priority_dedup():
    """Cross-field dedup follows the frozen quality/character/appearance/tags/environment order."""
    from backend.tagger2.workflow.stages.replacement import replace_projection, rule_from_csv

    rules = {
        "anthro": rule_from_csv("replace", "furry"),
        "meta": rule_from_csv("drop", ""),
        "duo_focus": rule_from_csv("replace", "duo|focus"),
    }

    payload = {
        "quality": ["masterpiece"],
        "count": "duo",
        "character": "rex",
        "series": "",
        "artist": "@studio",
        "appearance": ["anthro"],
        "tags": ["anthro", "meta", "duo_focus"],
        "environment": ["forest"],
        "nl": "Two characters in a forest.",
    }

    result, summary = replace_projection(payload, rules)

    assert result["appearance"] == ["furry"]
    # `anthro` already emitted `furry` from appearance, so tags cannot repeat it.
    assert result["tags"] == ["duo", "focus"]
    assert result["environment"] == ["forest"]
    assert result["count"] == "duo"
    assert summary.dropped == 1
    assert summary.replaced == 3


def test_nine_field_normalization_and_flat_txt():
    """Normalization emits exactly nine ordered fields; flat TXT is deterministic."""
    import json

    from backend.tagger2.workflow.caption_format import (
        CaptionDisplayPolicy,
        normalize_json_bytes,
        serialize_flat_txt,
    )

    policy = CaptionDisplayPolicy(
        replace_underscores_with_spaces=True,
        preserve_escapes=True,
        triggers_enabled=False,
        trigger_terms=(),
    )

    raw = json.dumps(
        {
            "quality": ["masterpiece", "masterpiece"],
            "count": "solo",
            "character": "rex, rex",
            "series": "",
            "artist": "@studio",
            "appearance": "blue_fur, red_eyes",
            "tags": ["holding_cup_(object)"],
            "environment": ["forest"],
            "nl": "A   single character.",
        }
    ).encode("utf-8")

    result = normalize_json_bytes(raw, policy, export_format="both")
    assert result.valid, result.field_errors
    assert list(result.payload) == [
        "quality",
        "count",
        "character",
        "series",
        "artist",
        "appearance",
        "tags",
        "environment",
        "nl",
    ]
    assert result.payload["quality"] == ["masterpiece"]
    assert result.payload["character"] == "rex"
    assert result.payload["appearance"] == ["blue_fur", "red_eyes"]
    assert result.payload["nl"] == "A single character."

    flat = serialize_flat_txt(result.payload, policy).decode("utf-8")
    assert "blue fur" in flat
    assert "holding cup \\(object\\)" in flat
    assert flat.endswith(".")


def test_normalization_rejects_extra_fields_and_bad_count():
    """Unknown fields and out-of-range count values are blocking errors."""
    import json

    from backend.tagger2.workflow.caption_format import CaptionDisplayPolicy, normalize_json_bytes

    policy = CaptionDisplayPolicy(True, True, False, ())

    raw = json.dumps(
        {
            "quality": [],
            "count": "many",
            "character": "",
            "series": "",
            "artist": "",
            "appearance": [],
            "tags": ["cat"],
            "environment": [],
            "nl": "",
            "unexpected": "value",
        }
    ).encode("utf-8")

    result = normalize_json_bytes(raw, policy)
    assert not result.valid
    codes = {error.code for error in result.field_errors}
    assert "extra_field" in codes
    assert "count_invalid" in codes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
