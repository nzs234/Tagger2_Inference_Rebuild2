"""Tests for the ported Policy (seeded dropout) and Token Budget stages."""

import pytest


def _payload(**overrides):
    base = {
        "quality": [],
        "count": "solo",
        "character": "rex",
        "series": "",
        "artist": "",
        "appearance": ["blue_fur"],
        "tags": ["male"],
        "environment": ["forest"],
        "nl": "A wolf stands in snow.",
    }
    base.update(overrides)
    return base


def _config(**overrides):
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, PolicyConfig

    defaults = dict(
        seed="seed-1",
        artistEnabled=False,
        artistDropoutProbability=0.0,
        qualityEnabled=False,
        qualityDropoutProbability=0.0,
        appearanceNlEnabled=False,
        solo=CoupledProbabilities(0.5, 0.5),
        nonSolo=CoupledProbabilities(0.5, 0.5),
        unknown=CoupledProbabilities(0.5, 0.5),
    )
    defaults.update(overrides)
    return PolicyConfig(**defaults)


def test_artist_directory_parsing():
    """The first-level number_artist directory supplies the artist tag."""
    from backend.tagger2.workflow.stages.policy import PolicyError, artist_from_image_path

    assert artist_from_image_path("12_studio/img.png") == "@studio"
    assert artist_from_image_path("3_noartname/img.png") == ""

    for bad in ("img.png", "studio/img.png", "../12_studio/img.png"):
        with pytest.raises(PolicyError):
            artist_from_image_path(bad)


def test_quality_banding_is_score_driven():
    """Aesthetic score maps onto the frozen quality bands."""
    from backend.tagger2.workflow.stages.policy import PolicyError, quality_for_score

    assert quality_for_score(1.0) == ["low quality"]
    assert quality_for_score(2.5) == ["normal quality"]
    assert quality_for_score(3.5) == ["good quality"]
    assert quality_for_score(5.0) == ["masterpiece", "best quality"]

    for bad in (0.5, 5.5, float("nan"), True):
        with pytest.raises(PolicyError):
            quality_for_score(bad)


def test_dropout_is_reproducible_for_a_given_seed():
    """The same seed and key always draw the same value, across separators."""
    from backend.tagger2.workflow.stages.policy import stable_random

    config = _config()
    first = stable_random(config, "12_studio/img", "artist")
    assert first == stable_random(config, "12_studio\\img", "artist")
    assert first == stable_random(config, "12_STUDIO/IMG", "artist")
    # A different decision name and a different seed both change the draw.
    assert first != stable_random(config, "12_studio/img", "quality")
    assert first != stable_random(_config(seed="seed-2"), "12_studio/img", "artist")


def test_policy_rejects_invalid_probabilities():
    """Coupled probabilities cannot exceed 1 in total."""
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, PolicyError

    with pytest.raises(PolicyError):
        CoupledProbabilities(0.7, 0.7)
    with pytest.raises(PolicyError):
        CoupledProbabilities(-0.1, 0.0)
    CoupledProbabilities(0.5, 0.5)


def test_policy_appends_artist_and_can_drop_it():
    """Artist is merged from the directory, then dropped per the seeded draw."""
    from backend.tagger2.workflow.stages.policy import apply_policy

    kept, decision = apply_policy(
        _payload(),
        annotation_key="12_studio/img",
        relative_image_path="12_studio/img.png",
        config=_config(artistEnabled=True, artistDropoutProbability=0.0),
        aesthetic_score=None,
    )
    assert kept["artist"] == "@studio"
    assert decision.artistDropped is False

    dropped, decision = apply_policy(
        _payload(),
        annotation_key="12_studio/img",
        relative_image_path="12_studio/img.png",
        config=_config(artistEnabled=True, artistDropoutProbability=1.0),
        aesthetic_score=None,
    )
    assert dropped["artist"] == ""
    assert decision.artistDropped is True


def test_policy_never_removes_both_appearance_and_nl():
    """The coupled dropout always leaves one of the two signals in place."""
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, apply_policy

    # dropNl = 1.0 removes NL and must keep appearance.
    result, decision = apply_policy(
        _payload(),
        annotation_key="a/b",
        relative_image_path="1_studio/b.png",
        config=_config(
            appearanceNlEnabled=True,
            solo=CoupledProbabilities(1.0, 0.0),
        ),
        aesthetic_score=None,
    )
    assert decision.appearanceNlAction == "drop_nl"
    assert result["nl"] == ""
    assert result["appearance"] == ["blue_fur"]

    # dropAppearance = 1.0 removes appearance and must keep NL.
    result, decision = apply_policy(
        _payload(),
        annotation_key="a/b",
        relative_image_path="1_studio/b.png",
        config=_config(
            appearanceNlEnabled=True,
            solo=CoupledProbabilities(0.0, 1.0),
        ),
        aesthetic_score=None,
    )
    assert decision.appearanceNlAction == "drop_appearance"
    assert result["appearance"] == []
    assert result["nl"]


def test_policy_leaves_single_sided_payload_untouched():
    """When one side is already empty the other is protected."""
    from backend.tagger2.workflow.stages.policy import CoupledProbabilities, apply_policy

    result, decision = apply_policy(
        _payload(appearance=[], nl="Only NL here."),
        annotation_key="a/b",
        relative_image_path="1_studio/b.png",
        config=_config(appearanceNlEnabled=True, solo=CoupledProbabilities(1.0, 0.0)),
        aesthetic_score=None,
    )
    assert decision.appearanceNlAction == "unchanged"
    assert result["nl"] == "Only NL here."


def test_policy_requires_score_when_quality_enabled():
    """Quality banding without a scorer is a hard error, not a guess."""
    from backend.tagger2.workflow.stages.policy import PolicyError, apply_policy

    with pytest.raises(PolicyError):
        apply_policy(
            _payload(),
            annotation_key="a/b",
            relative_image_path="1_studio/b.png",
            config=_config(qualityEnabled=True),
            aesthetic_score=None,
        )


def test_policy_never_alters_protected_fields():
    """count, character, series, tags and environment are never rewritten."""
    from backend.tagger2.workflow.stages.policy import apply_policy

    payload = _payload()
    result, _ = apply_policy(
        payload,
        annotation_key="a/b",
        relative_image_path="1_studio/b.png",
        config=_config(artistEnabled=True, appearanceNlEnabled=True),
        aesthetic_score=None,
    )
    for field in ("count", "character", "series", "tags", "environment"):
        assert result[field] == payload[field]


# --- Token budget ---

CAPTION_FORMAT = {
    "replaceUnderscoresWithSpaces": True,
    "preserveEscapes": True,
    "triggersEnabled": False,
    "triggerTerms": [],
}


def _word_counter(texts):
    return [len(text.decode("utf-8").split()) for text in texts]


def test_token_budget_within_budget_is_unchanged():
    from backend.tagger2.workflow.stages.token_budget import fit

    result = fit(_payload(), CAPTION_FORMAT, 1000, _word_counter)
    assert result.status == "within_budget"
    assert result.original_tokens == result.final_tokens
    assert all(not removed for removed in result.removed.values())


def test_token_budget_trims_in_frozen_field_order():
    """Quality is trimmed before environment, then tags, then appearance."""
    from backend.tagger2.workflow.stages.token_budget import TRIMMABLE_FIELDS, fit

    assert TRIMMABLE_FIELDS == ("quality", "environment", "tags", "appearance")

    payload = _payload(
        quality=["masterpiece", "best quality"],
        environment=["forest", "snow"],
        tags=["male", "standing"],
        nl="",
    )
    # Budget large enough that only the first trimmable field must give way.
    result = fit(payload, CAPTION_FORMAT, 7, _word_counter)
    assert result.status == "trimmed"
    assert result.removed["quality"]
    assert result.annotation is not None
    assert result.final_tokens <= 7


def test_token_budget_reports_overflow_without_producing_output():
    """An impossible budget yields overflow and no annotation to commit."""
    from backend.tagger2.workflow.stages.token_budget import fit

    result = fit(_payload(nl="A very long sentence that cannot be trimmed away."), CAPTION_FORMAT, 1, _word_counter)
    assert result.status == "overflow"
    assert result.annotation is None


def test_token_budget_rejects_invalid_budget():
    from backend.tagger2.workflow.stages.token_budget import TokenBudgetError, fit

    for bad in (0, -1, 1.5, True):
        with pytest.raises(TokenBudgetError):
            fit(_payload(), CAPTION_FORMAT, bad, _word_counter)


def test_token_budget_rejects_dishonest_tokenizer():
    """A tokenizer returning the wrong shape of counts is an error.

    The first probe is always a single caption, so a short batch is only
    detectable once trimming starts; drive the budget low enough to get there.
    """
    from backend.tagger2.workflow.stages.token_budget import TokenBudgetError, fit

    def short_batch(texts):
        # Honest for the initial single probe, truncated for the trim search.
        return [99] if len(texts) == 1 else [1]

    with pytest.raises(TokenBudgetError):
        fit(_payload(), CAPTION_FORMAT, 10, short_batch)

    with pytest.raises(TokenBudgetError):
        fit(_payload(), CAPTION_FORMAT, 10, lambda texts: [-1] * len(texts))

    with pytest.raises(TokenBudgetError):
        fit(_payload(), CAPTION_FORMAT, 10, lambda texts: [1.5] * len(texts))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
