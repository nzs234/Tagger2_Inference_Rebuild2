from tagger2.schemas import TagItem
from tagger2.tag_output import escape_tag_parentheses, format_local_tags


def test_escape_tag_parentheses_adds_one_backslash_without_double_escaping():
    assert escape_tag_parentheses("fennix (fortnite)") == r"fennix \(fortnite\)"
    assert escape_tag_parentheses(r"fennix \(fortnite\)") == r"fennix \(fortnite\)"
    assert escape_tag_parentheses(r"fennix \\(fortnite\\)") == r"fennix \(fortnite\)"


def test_local_tag_output_defaults_filter_rating_and_escape_parentheses():
    tags = [
        TagItem(text="fennix_(fortnite)", category="character", model_id="tagger"),
        TagItem(text="questionable", category="rating", model_id="tagger"),
    ]

    values = format_local_tags(tags, {})

    assert [value["text"] for value in values] == [r"fennix_\(fortnite\)"]
    assert tags[0].text == "fennix_(fortnite)"


def test_local_tag_output_switches_can_include_rating_and_disable_escaping():
    tags = [
        TagItem(text="fennix_(fortnite)", category="character", model_id="tagger"),
        TagItem(text="questionable", category="RATING", model_id="tagger"),
    ]

    values = format_local_tags(
        tags,
        {
            "include_rating": True,
            "replace_underscores": True,
            "escape_parentheses": False,
        },
    )

    assert [value["text"] for value in values] == ["fennix (fortnite)", "questionable"]


def test_local_tag_output_merges_duplicate_tags_and_keeps_the_highest_score():
    tags = [
        TagItem(text="shared_tag", category="general", score=0.4, model_id="first"),
        TagItem(text="shared tag", category="general", score=0.9, model_id="second"),
        TagItem(text="other_tag", category="general", score=0.8, model_id="second"),
    ]

    values = format_local_tags(tags, {"replace_underscores": True})

    assert [value["text"] for value in values] == ["shared tag", "other tag"]
    assert values[0]["score"] == 0.9


def test_local_tag_output_prioritizes_character_and_species_before_general():
    values = format_local_tags(
        [
            TagItem(text="high score general", category="general", score=0.99, model_id="tagger"),
            TagItem(text="character name", category="character", score=0.60, model_id="tagger"),
            TagItem(text="canid", category="species", score=0.70, model_id="tagger"),
            TagItem(text="lower score general", category="general", score=0.70, model_id="tagger"),
        ],
        {},
    )

    assert [value["text"] for value in values] == [
        "character name",
        "canid",
        "high score general",
        "lower score general",
    ]
