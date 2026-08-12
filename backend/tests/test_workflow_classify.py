"""Tests for the Classify stage."""

import pytest

from tagger2.workflow.stages.classify import (
    ClassifyError,
    build_classification_rules,
    classify_tags,
)


def test_build_classification_rules_e621():
    """Build e621 classification rules from snapshot data."""
    tags_data = [
        {"name": "solo", "category": "general", "post_count": 1000000},
        {"name": "anthro", "category": "species", "post_count": 500000},
        {"name": "female", "category": "general", "post_count": 800000},
        {"name": "pikachu", "category": "character", "post_count": 50000},
        {"name": "rating_safe", "category": "meta", "post_count": 1200000},
    ]
    aliases_data = [
        {"antecedent_name": "pika", "consequent_name": "pikachu"},
    ]
    
    rules = build_classification_rules("e621", tags_data, aliases_data)
    
    assert rules.profile == "e621"
    assert len(rules.tags) == 5
    assert rules.tags["pikachu"].category == "character"
    assert rules.normalize("pika") == "pikachu"
    assert rules.normalize("solo") == "solo"
    assert rules.category("pikachu") == "character"
    assert rules.category("unknown_tag") is None


def test_alias_chain_flattening():
    """Flatten multi-hop alias chains."""
    tags_data = [
        {"name": "final", "category": "character", "post_count": 100},
    ]
    aliases_data = [
        {"antecedent_name": "a", "consequent_name": "b"},
        {"antecedent_name": "b", "consequent_name": "c"},
        {"antecedent_name": "c", "consequent_name": "final"},
    ]
    
    rules = build_classification_rules("e621", tags_data, aliases_data)
    
    assert rules.normalize("a") == "final"
    assert rules.normalize("b") == "final"
    assert rules.normalize("c") == "final"


def test_alias_cycle_detection():
    """Detect and reject alias cycles."""
    tags_data = []
    aliases_data = [
        {"antecedent_name": "a", "consequent_name": "b"},
        {"antecedent_name": "b", "consequent_name": "c"},
        {"antecedent_name": "c", "consequent_name": "a"},
    ]
    
    with pytest.raises(ClassifyError, match="alias cycle detected"):
        build_classification_rules("e621", tags_data, aliases_data)


def test_classify_tags_basic():
    """Classify tags into nine-field structure."""
    tags_data = [
        {"name": "solo", "category": "general"},
        {"name": "anthro", "category": "species"},
        {"name": "pikachu", "category": "character"},
        {"name": "rating_safe", "category": "meta"},
        {"name": "nintendo", "category": "copyright"},
    ]
    aliases_data = []
    
    rules = build_classification_rules("e621", tags_data, aliases_data)
    result = classify_tags(["solo", "anthro", "pikachu", "rating_safe", "nintendo"], rules)
    
    assert result["quality"] == ["rating_safe"]
    assert result["character"] == ["pikachu"]
    assert result["appearance"] == ["anthro"]
    assert "solo" in result["tags"]
    assert "nintendo" in result["tags"]


def test_classify_tags_with_aliases():
    """Classify tags after resolving aliases."""
    tags_data = [
        {"name": "pikachu", "category": "character"},
    ]
    aliases_data = [
        {"antecedent_name": "pika", "consequent_name": "pikachu"},
        {"antecedent_name": "electric_mouse", "consequent_name": "pikachu"},
    ]
    
    rules = build_classification_rules("e621", tags_data, aliases_data)
    result = classify_tags(["pika", "electric_mouse"], rules)
    
    # Both aliases should resolve to pikachu, and dedup should leave one
    assert result["character"] == ["pikachu"]


def test_classify_unknown_tags():
    """Unknown tags go to the general tags field."""
    tags_data = [
        {"name": "known_tag", "category": "general"},
    ]
    aliases_data = []
    
    rules = build_classification_rules("e621", tags_data, aliases_data)
    result = classify_tags(["known_tag", "unknown_tag"], rules)
    
    assert "known_tag" in result["tags"]
    assert "unknown_tag" in result["tags"]


def test_invalid_profile():
    """Reject unsupported profiles."""
    with pytest.raises(ClassifyError, match="unsupported classification profile"):
        build_classification_rules("invalid", [], [])


def test_invalid_tag_data():
    """Reject malformed tag data."""
    with pytest.raises(ClassifyError, match="invalid tag record"):
        build_classification_rules("e621", [{"bad": "data"}], [])


def test_invalid_alias_data():
    """Reject malformed alias data."""
    with pytest.raises(ClassifyError, match="invalid alias record"):
        build_classification_rules("e621", [], [{"bad": "data"}])
