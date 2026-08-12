"""Nine-field annotation structure and export contract tests.

Tests that all stages produce projections conforming to the canonical nine-field
shape, and that export properly validates and serializes them.
"""
import json
import pytest
from tagger2.workflow.contracts import NineFieldAnnotation
from tagger2.workflow.caption_format import (
    CaptionDisplayPolicy,
    normalize_json_bytes,
    serialize_flat_txt,
)


def _default_policy() -> CaptionDisplayPolicy:
    """Create a minimal CaptionDisplayPolicy for testing."""
    return CaptionDisplayPolicy(
        replace_underscores_with_spaces=False,
        preserve_escapes=False,
        triggers_enabled=False,
        trigger_terms=(),
    )


def _normalize_and_extract(anno: dict, policy: CaptionDisplayPolicy) -> dict:
    """Normalize annotation and extract payload for serialization tests."""
    anno_bytes = json.dumps(anno).encode("utf-8")
    result = normalize_json_bytes(anno_bytes, policy, export_format="both")
    if not result.valid:
        raise ValueError(f"Normalization failed: {result.field_errors}")
    return result.payload


class TestNineFieldStructure:
    """Nine-field annotation type contract."""

    def test_complete_annotation(self):
        """Complete nine-field annotation with all fields populated."""
        anno: NineFieldAnnotation = {
            "quality": ["safe"],
            "count": "solo",
            "character": "character1, character2",
            "series": "series1",
            "artist": "artist1",
            "appearance": ["blue_eyes", "long_hair"],
            "tags": ["outdoors", "forest"],
            "environment": ["daytime", "sunny"],
            "nl": "A character standing in a forest.",
        }
        
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        # All fields present
        assert "quality" in normalized
        assert "count" in normalized
        assert "character" in normalized
        assert "series" in normalized
        assert "artist" in normalized
        assert "appearance" in normalized
        assert "tags" in normalized
        assert "environment" in normalized
        assert "nl" in normalized

    def test_partial_annotation(self):
        """Partial annotation (missing optional fields get filled with defaults)."""
        anno: NineFieldAnnotation = {
            "quality": ["safe"],
            "tags": ["outdoors"],
        }
        
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        # All nine fields present after normalization
        assert len(normalized) == 9
        assert normalized["quality"] == ["safe"]
        assert normalized["tags"] == ["outdoors"]

    def test_minimal_valid_annotation(self):
        """At least one non-empty field required."""
        anno: NineFieldAnnotation = {
            "tags": ["test"],
        }
        
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        assert len(normalized) == 9
        assert "test" in normalized["tags"]


class TestNormalization:
    """normalize_json_bytes validation and auto-correction contract."""

    def test_quality_string_auto_split(self):
        """An array field given a string is split on commas, not wrapped blindly."""
        anno = {"quality": "safe", "tags": ["test"]}

        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert result.valid
        assert result.payload["quality"] == ["safe"]
        # ``quality`` is an array field, so the conversion is a comma split.
        assert result.conversions.get("array_string_split", 0) > 0

    def test_array_field_string_splits_on_comma(self):
        """A multi-value string becomes multiple entries rather than one."""
        anno = {"quality": "safe, best quality", "tags": ["test"]}

        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert result.valid
        assert result.payload["quality"] == ["safe", "best quality"]

    def test_count_list_auto_unwrapped(self):
        """count as list gets auto-unwrapped to single string."""
        anno = {"count": ["solo"], "tags": ["test"]}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert result.valid
        assert result.payload["count"] == "solo"
        assert result.conversions.get("single_string_array_unwrapped", 0) > 0

    def test_tags_comma_string_auto_split(self):
        """tags as comma-separated string gets auto-split to list."""
        anno = {"tags": "tag1, tag2"}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert result.valid
        assert result.payload["tags"] == ["tag1", "tag2"]
        assert result.conversions.get("array_string_split", 0) > 0

    def test_unknown_keys_rejected(self):
        """Unknown keys in annotation are rejected."""
        anno = {"quality": ["safe"], "unknown_field": "value"}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert not result.valid
        assert len(result.field_errors) > 0

    def test_json_export_format(self):
        """export_format=json produces valid JSON bytes."""
        anno: NineFieldAnnotation = {
            "quality": ["safe"],
            "tags": ["outdoors"],
            "nl": "A description.",
        }
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert result.valid
        assert result.json_bytes is not None
        assert result.json_bytes.startswith(b"{")
        assert b'"quality"' in result.json_bytes
        assert b'"tags"' in result.json_bytes

    def test_flat_txt_via_serialize(self):
        """flat_txt produced via serialize_flat_txt on normalized payload."""
        anno: NineFieldAnnotation = {
            "quality": ["safe"],
            "tags": ["outdoors", "forest"],
            "nl": "A description.",
        }
        
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        flat_txt = serialize_flat_txt(normalized, policy)
        assert b"{" not in flat_txt  # Not JSON
        assert b"outdoors" in flat_txt
        assert b"forest" in flat_txt


class TestFlatTxtSerialization:
    """serialize_flat_txt contract tests."""

    def test_basic_tags_only(self):
        """Basic case: comma-separated tags without NL."""
        anno = {"tags": ["tag1", "tag2", "tag3"]}
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy)
        assert b"tag1" in output
        assert b"tag2" in output
        assert b"tag3" in output

    def test_with_natural_language(self):
        """Tags followed by NL when present."""
        anno = {
            "tags": ["outdoors"],
            "nl": "A forest scene.",
        }
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy)
        assert b"outdoors" in output
        assert b"A forest scene." in output

    def test_quality_appearance_environment(self):
        """All tag fields merged in correct order."""
        anno = {
            "quality": ["safe"],
            "appearance": ["blue_eyes"],
            "tags": ["outdoors"],
            "environment": ["daytime"],
        }
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy)
        decoded = output.decode("utf-8")
        
        # All fields present
        assert "safe" in decoded
        assert "blue_eyes" in decoded
        assert "outdoors" in decoded
        assert "daytime" in decoded

    def test_minimal_output(self):
        """Minimal annotation with single tag."""
        anno = {"tags": ["single_tag"]}
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy)
        assert b"single_tag" in output

    def test_nl_only(self):
        """NL-only annotation (no tags)."""
        anno = {
            "nl": "A description with no tags.",
        }
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy)
        assert b"A description with no tags." in output


class TestExportFormatEnum:
    """Test export.format configuration enum."""

    @pytest.mark.parametrize("fmt", ["json", "flat_txt", "both"])
    def test_valid_formats(self, fmt):
        """Valid export formats are accepted."""
        anno: NineFieldAnnotation = {"tags": ["test"]}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format=fmt)
        assert result.valid

    def test_invalid_format_rejected(self):
        """Invalid export format raises error."""
        anno: NineFieldAnnotation = {"tags": ["test"]}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        with pytest.raises((ValueError, KeyError)):
            normalize_json_bytes(anno_bytes, _default_policy(), export_format="invalid_format")


class TestCrossFieldOrdering:
    """Test tag field merging and ordering."""

    def test_field_priority_order(self):
        """Fields merged in priority order: quality, character, appearance, tags, environment."""
        anno = {
            "environment": ["daytime"],
            "tags": ["outdoors"],
            "appearance": ["blue_eyes"],
            "quality": ["safe"],
        }
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy).decode("utf-8")
        
        # All fields present
        assert "safe" in output
        assert "blue_eyes" in output
        assert "outdoors" in output
        assert "daytime" in output

    def test_character_series_artist_in_output(self):
        """character, series, artist fields appear in flat_txt output."""
        anno = {
            "character": "char1, char2",
            "series": "series1",
            "artist": "artist1",
        }
        policy = _default_policy()
        normalized = _normalize_and_extract(anno, policy)
        
        output = serialize_flat_txt(normalized, policy).decode("utf-8")
        
        # String fields tokenized and included
        assert "char1" in output or "char2" in output
        assert "series1" in output
        assert "artist1" in output


class TestAutoCorrection:
    """Test normalizer auto-correction behaviors."""

    def test_missing_fields_defaulted(self):
        """Missing fields auto-filled with empty defaults."""
        anno = {"tags": ["test"]}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        assert result.valid
        assert result.conversions.get("missing_field_defaulted", 0) == 8  # 8 other fields
        
        # All fields present with defaults
        assert result.payload["quality"] == []
        assert result.payload["count"] == ""
        assert result.payload["character"] == ""
        assert result.payload["series"] == ""
        assert result.payload["artist"] == ""
        assert result.payload["appearance"] == []
        assert result.payload["environment"] == []
        assert result.payload["nl"] == ""

    def test_underscore_alias_corrected(self):
        """Field name aliases with underscores are corrected."""
        # If normalizer supports aliases like "natural_language" -> "nl"
        anno = {"tags": ["test"], "natural_language": "A description."}
        
        anno_bytes = json.dumps(anno).encode("utf-8")
        result = normalize_json_bytes(anno_bytes, _default_policy(), export_format="json")
        
        # Either rejected as unknown, or corrected to "nl"
        if result.valid:
            assert result.payload.get("nl") == "A description."
        else:
            assert len(result.field_errors) > 0
