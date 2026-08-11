"""Helper to convert policy dict config to PolicyConfig dataclass."""

from __future__ import annotations

from typing import Any

from .stages.policy import PolicyConfig, CoupledProbabilities


def parse_policy_config(policy_dict: dict[str, Any]) -> PolicyConfig:
    """Convert a policy configuration dict to PolicyConfig dataclass.
    
    Maps the flat config keys to the nested dataclass structure expected by apply_policy.
    
    The configuration dict uses flat keys:
    - directory_to_artist: bool -> artistEnabled
    - artist_dropout: float -> artistDropoutProbability
    - quality_dropout: float -> qualityDropoutProbability (qualityEnabled inferred)
    - appearance_nl_solo_drop_nl: float
    - appearance_nl_solo_drop_appearance: float
    - appearance_nl_non_solo_drop_nl: float
    - appearance_nl_non_solo_drop_appearance: float
    
    Unknown count probabilities default to a balanced 0.35/0.35 split.
    """
    
    # Extract scalar config
    seed = str(policy_dict.get("seed", "workflow-default-v1"))
    artist_enabled = bool(policy_dict.get("directory_to_artist", True))
    artist_dropout_prob = float(policy_dict.get("artist_dropout", 0.0))
    quality_dropout_prob = float(policy_dict.get("quality_dropout", 0.0))
    
    # Quality is enabled only if dropout > 0 (requires aesthetic scoring)
    quality_enabled = quality_dropout_prob > 0.0
    
    # Appearance/NL is always enabled in this workflow
    appearance_nl_enabled = True
    
    # Build coupled probabilities for each count category
    solo = CoupledProbabilities(
        dropNl=float(policy_dict.get("appearance_nl_solo_drop_nl", 0.70)),
        dropAppearance=float(policy_dict.get("appearance_nl_solo_drop_appearance", 0.05)),
    )
    
    non_solo = CoupledProbabilities(
        dropNl=float(policy_dict.get("appearance_nl_non_solo_drop_nl", 0.05)),
        dropAppearance=float(policy_dict.get("appearance_nl_non_solo_drop_appearance", 0.70)),
    )
    
    # Unknown defaults to balanced probabilities
    unknown = CoupledProbabilities(
        dropNl=float(policy_dict.get("appearance_nl_unknown_drop_nl", 0.35)),
        dropAppearance=float(policy_dict.get("appearance_nl_unknown_drop_appearance", 0.35)),
    )
    
    return PolicyConfig(
        seed=seed,
        artistEnabled=artist_enabled,
        artistDropoutProbability=artist_dropout_prob,
        qualityEnabled=quality_enabled,
        qualityDropoutProbability=quality_dropout_prob,
        appearanceNlEnabled=appearance_nl_enabled,
        solo=solo,
        nonSolo=non_solo,
        unknown=unknown,
    )
