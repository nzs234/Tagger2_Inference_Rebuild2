"""Regression tests for P0-D: Policy config parsing and Replace resource selection."""

import pytest
from tagger2.workflow.policy_config_parser import parse_policy_config
from tagger2.workflow.stages.policy import PolicyConfig, PolicyError


class TestPolicyConfigParser:
    """Test dict-to-dataclass conversion for policy configuration."""
    
    def test_parse_minimal_config(self):
        """Minimal config with only seed should produce valid PolicyConfig."""
        config_dict = {
            "enabled": True,
            "seed": "test-seed-v1",
        }
        
        result = parse_policy_config(config_dict)
        
        assert isinstance(result, PolicyConfig)
        assert result.seed == "test-seed-v1"
        assert result.artistEnabled is True  # default
        assert result.artistDropoutProbability == 0.0
        assert result.qualityEnabled is False  # inferred from dropout = 0
        assert result.appearanceNlEnabled is True
    
    def test_parse_full_config(self):
        """Full config with all fields should map correctly."""
        config_dict = {
            "enabled": True,
            "seed": "full-test-v2",
            "directory_to_artist": False,
            "artist_dropout": 0.25,
            "quality_dropout": 0.15,
            "appearance_nl_solo_drop_nl": 0.80,
            "appearance_nl_solo_drop_appearance": 0.10,
            "appearance_nl_non_solo_drop_nl": 0.15,
            "appearance_nl_non_solo_drop_appearance": 0.75,
            "appearance_nl_unknown_drop_nl": 0.40,
            "appearance_nl_unknown_drop_appearance": 0.30,
        }
        
        result = parse_policy_config(config_dict)
        
        assert result.seed == "full-test-v2"
        assert result.artistEnabled is False
        assert result.artistDropoutProbability == 0.25
        assert result.qualityEnabled is True  # inferred from dropout > 0
        assert result.qualityDropoutProbability == 0.15
        
        assert result.solo.dropNl == 0.80
        assert result.solo.dropAppearance == 0.10
        assert result.nonSolo.dropNl == 0.15
        assert result.nonSolo.dropAppearance == 0.75
        assert result.unknown.dropNl == 0.40
        assert result.unknown.dropAppearance == 0.30
    
    def test_parse_default_unknown_probabilities(self):
        """Unknown probabilities should default to 0.35/0.35 when not specified."""
        config_dict = {
            "seed": "default-unknown-test",
            "appearance_nl_solo_drop_nl": 0.70,
            "appearance_nl_solo_drop_appearance": 0.05,
            "appearance_nl_non_solo_drop_nl": 0.05,
            "appearance_nl_non_solo_drop_appearance": 0.70,
        }
        
        result = parse_policy_config(config_dict)
        
        assert result.unknown.dropNl == 0.35
        assert result.unknown.dropAppearance == 0.35
    
    def test_parse_invalid_coupled_probabilities(self):
        """Invalid coupled probabilities should raise PolicyError during dataclass init."""
        config_dict = {
            "seed": "invalid-test",
            "appearance_nl_solo_drop_nl": 0.80,
            "appearance_nl_solo_drop_appearance": 0.50,  # Sum > 1.0
        }
        
        with pytest.raises(PolicyError, match="must not exceed 1"):
            parse_policy_config(config_dict)
    
    def test_parse_invalid_seed(self):
        """Empty seed should raise PolicyError."""
        config_dict = {
            "seed": "",
        }
        
        with pytest.raises(PolicyError, match="seed must be a non-blank string"):
            parse_policy_config(config_dict)
    
    def test_parse_out_of_range_dropout(self):
        """Dropout probability outside [0,1] should raise PolicyError."""
        config_dict = {
            "seed": "out-of-range-test",
            "artist_dropout": 1.5,
        }
        
        with pytest.raises(PolicyError, match="must be between 0 and 1"):
            parse_policy_config(config_dict)


class TestReplaceResourceSelection:
    """Test that replace stage uses config-specified resource_id, not hardcoded value.
    
    This is an integration concern tested via the job creation contract.
    The actual API behavior is covered by existing pipeline tests.
    """
    
    def test_replace_config_has_resource_id_field(self):
        """Job config's replace section must support resource_id field."""
        from tagger2.workflow.contracts import WorkflowJobConfigV1, WorkflowPathRef
        
        config = WorkflowJobConfigV1(
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="skip",
            source_root=WorkflowPathRef(root_id="test-root", relative_path="."),
            recursive=False,
            replace={
                "enabled": True,
                "resource_id": "custom-replacement-index-v2",
            },
        )
        
        assert config.replace["enabled"] is True
        assert config.replace["resource_id"] == "custom-replacement-index-v2"
    
    def test_replace_config_default_resource_id(self):
        """Replace config should have a default resource_id."""
        from tagger2.workflow.contracts import WorkflowJobConfigV1, WorkflowPathRef
        
        config = WorkflowJobConfigV1(
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="skip",
            source_root=WorkflowPathRef(root_id="test-root", relative_path="."),
            recursive=False,
        )
        
        # The default factory should include resource_id
        assert "resource_id" in config.replace
        assert isinstance(config.replace["resource_id"], str)
        assert config.replace["resource_id"].startswith("replace-")
