"""Tests for the classification snapshot resource."""

import json
from pathlib import Path

import pytest

from tagger2.workflow.classify_snapshot import (
    CLASSIFY_SNAPSHOT_FORMAT,
    ClassifySnapshotError,
    build_snapshot_from_official_csv,
    load_classification_rules,
    validate_classify_snapshot,
)


def _snapshot(**overrides):
    document = {
        "format": CLASSIFY_SNAPSHOT_FORMAT,
        "profile": "e621",
        "source": {"url": "https://e621.net/db_export/", "timestamp": "2026-08-11T00:00:00Z"},
        "tags": [
            {"name": "solo", "category": "general", "post_count": 100},
            {"name": "hatsune_miku", "category": "character", "post_count": 80},
            {"name": "vocaloid", "category": "copyright", "post_count": 50},
            {"name": "human", "category": "species", "post_count": 200},
            {"name": "rating_safe", "category": "meta", "post_count": 900},
        ],
        "aliases": [{"antecedent_name": "1girl", "consequent_name": "solo"}],
        "implications": [{"antecedent_name": "hatsune_miku", "consequent_name": "vocaloid"}],
    }
    document.update(overrides)
    return document


def _write(path: Path, document) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_valid_snapshot_reports_counts(tmp_path):
    """A clean snapshot validates and reports per-category counts."""
    path = _write(tmp_path / "snap.json", _snapshot())

    report = validate_classify_snapshot(path)

    assert report.valid is True, report.errors
    assert report.profile == "e621"
    assert report.tag_count == 5
    assert report.alias_count == 1
    assert report.implication_count == 1
    assert report.category_counts["character"] == 1
    assert report.category_counts["species"] == 1


def test_snapshot_loads_into_rules(tmp_path):
    """Loading yields rules that resolve aliases and categories."""
    path = _write(tmp_path / "snap.json", _snapshot())

    rules = load_classification_rules(path)

    assert rules.profile == "e621"
    assert rules.normalize("1girl") == "solo"
    assert rules.category("hatsune_miku") == "character"
    assert rules.category("unknown_tag") is None
    assert rules.implications["hatsune_miku"] == ("vocaloid",)


def test_wrong_format_marker_is_rejected(tmp_path):
    """A document without the expected format marker is refused."""
    path = _write(tmp_path / "snap.json", _snapshot(format="something-else"))

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert "format must be" in report.errors[0]

    with pytest.raises(ClassifySnapshotError):
        load_classification_rules(path)


def test_unknown_profile_is_rejected(tmp_path):
    """Only e621 and danbooru are accepted profiles."""
    path = _write(tmp_path / "snap.json", _snapshot(profile="gelbooru"))

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert "unsupported classification profile" in report.errors[0]


def test_category_outside_the_profile_is_rejected(tmp_path):
    """`species` is not a Danbooru category, so it must not be accepted there."""
    document = _snapshot(
        profile="danbooru",
        tags=[{"name": "human", "category": "species", "post_count": 1}],
        aliases=[],
        implications=[],
    )
    path = _write(tmp_path / "snap.json", document)

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert "category must be one of" in report.errors[0]


def test_duplicate_tag_and_padded_name_report_their_index(tmp_path):
    """A duplicate row and a padded name each report the offending index."""
    document = _snapshot(
        tags=[
            {"name": "solo", "category": "general", "post_count": 1},
            {"name": "solo", "category": "general", "post_count": 2},
            {"name": " padded ", "category": "general", "post_count": 3},
        ],
        aliases=[],
        implications=[],
    )
    path = _write(tmp_path / "snap.json", document)

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert any("tags[1]" in error and "duplicate" in error for error in report.errors)
    assert any("tags[2]" in error and "padded" in error for error in report.errors)


def test_alias_cycle_is_reported_by_validation(tmp_path):
    """A cycle only appears once chains are flattened, so validation must catch it."""
    document = _snapshot(
        tags=[{"name": "a", "category": "general", "post_count": 1}],
        aliases=[
            {"antecedent_name": "a", "consequent_name": "b"},
            {"antecedent_name": "b", "consequent_name": "a"},
        ],
        implications=[],
    )
    path = _write(tmp_path / "snap.json", document)

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert any("cycle" in error for error in report.errors)


def test_self_referencing_alias_is_rejected(tmp_path):
    """An alias pointing at itself is malformed input, not a no-op."""
    document = _snapshot(aliases=[{"antecedent_name": "solo", "consequent_name": "solo"}])
    path = _write(tmp_path / "snap.json", document)

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert any("points at itself" in error for error in report.errors)


def test_negative_post_count_is_rejected(tmp_path):
    """post_count must be a non-negative integer."""
    document = _snapshot(tags=[{"name": "solo", "category": "general", "post_count": -1}])
    path = _write(tmp_path / "snap.json", document)

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert any("post_count" in error for error in report.errors)


def test_invalid_json_is_reported_not_repaired(tmp_path):
    """A truncated document reports a JSON error rather than partially loading."""
    path = tmp_path / "snap.json"
    path.write_text('{"format": "classify-snapshot-v1", "profile": "e621",', encoding="utf-8")

    report = validate_classify_snapshot(path)
    assert report.valid is False
    assert "not valid JSON" in report.errors[0]


def test_bom_is_tolerated(tmp_path):
    """A BOM from an exporting tool is not treated as a failure."""
    path = tmp_path / "snap.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_snapshot()).encode("utf-8"))

    assert validate_classify_snapshot(path).valid is True


def test_build_snapshot_from_official_csv_maps_integer_categories(tmp_path):
    """The published exports use integer categories, mapped per profile."""
    tags_csv = tmp_path / "tags.csv"
    tags_csv.write_text(
        "id,name,category,post_count\n"
        "1,solo,0,100\n"
        "2,some_artist,1,10\n"
        "3,vocaloid,3,50\n"
        "4,hatsune_miku,4,80\n"
        "5,human,5,200\n"
        "6,rating_safe,7,900\n",
        encoding="utf-8",
    )
    aliases_csv = tmp_path / "aliases.csv"
    aliases_csv.write_text(
        "id,antecedent_name,consequent_name,status\n"
        "1,1girl,solo,active\n"
        "2,retired_tag,solo,deleted\n",
        encoding="utf-8",
    )
    implications_csv = tmp_path / "implications.csv"
    implications_csv.write_text(
        "id,antecedent_name,consequent_name,status\n1,hatsune_miku,vocaloid,active\n",
        encoding="utf-8",
    )

    document = build_snapshot_from_official_csv(
        profile="e621",
        tags_csv=tags_csv,
        aliases_csv=aliases_csv,
        implications_csv=implications_csv,
        source_url="https://e621.net/db_export/",
    )

    assert document["format"] == CLASSIFY_SNAPSHOT_FORMAT
    assert document["profile"] == "e621"
    by_name = {row["name"]: row for row in document["tags"]}
    assert by_name["solo"]["category"] == "general"
    assert by_name["some_artist"]["category"] == "artist"
    assert by_name["vocaloid"]["category"] == "copyright"
    assert by_name["hatsune_miku"]["category"] == "character"
    assert by_name["human"]["category"] == "species"
    assert by_name["rating_safe"]["category"] == "meta"

    # Only active aliases are applied by the site, so a deleted row is skipped.
    assert document["aliases"] == [{"antecedent_name": "1girl", "consequent_name": "solo"}]
    assert document["implications"] == [
        {"antecedent_name": "hatsune_miku", "consequent_name": "vocaloid"}
    ]

    # The generated bundle must satisfy the reader it was built for.
    path = _write(tmp_path / "snap.json", document)
    assert validate_classify_snapshot(path).valid is True


def test_build_snapshot_rejects_unknown_category_code(tmp_path):
    """An unmapped category code is an error, never folded into `general`."""
    tags_csv = tmp_path / "tags.csv"
    tags_csv.write_text("id,name,category,post_count\n1,solo,99,100\n", encoding="utf-8")
    aliases_csv = tmp_path / "aliases.csv"
    aliases_csv.write_text("id,antecedent_name,consequent_name,status\n", encoding="utf-8")

    with pytest.raises(ClassifySnapshotError) as excinfo:
        build_snapshot_from_official_csv(
            profile="e621", tags_csv=tags_csv, aliases_csv=aliases_csv
        )
    assert "line 2" in str(excinfo.value)
    assert "unknown e621 category" in str(excinfo.value)


def test_build_snapshot_rejects_missing_column(tmp_path):
    """A missing required column names the column instead of failing obscurely."""
    tags_csv = tmp_path / "tags.csv"
    tags_csv.write_text("id,name,post_count\n1,solo,100\n", encoding="utf-8")
    aliases_csv = tmp_path / "aliases.csv"
    aliases_csv.write_text("id,antecedent_name,consequent_name,status\n", encoding="utf-8")

    with pytest.raises(ClassifySnapshotError) as excinfo:
        build_snapshot_from_official_csv(
            profile="e621", tags_csv=tags_csv, aliases_csv=aliases_csv
        )
    assert "category" in str(excinfo.value)


def test_danbooru_snapshot_round_trips(tmp_path):
    """A Danbooru export maps through its own category table."""
    tags_csv = tmp_path / "tags.csv"
    tags_csv.write_text(
        "id,name,category,post_count\n1,solo,0,100\n2,some_artist,1,5\n3,commentary,5,7\n",
        encoding="utf-8",
    )
    aliases_csv = tmp_path / "aliases.csv"
    aliases_csv.write_text("id,antecedent_name,consequent_name,status\n", encoding="utf-8")

    document = build_snapshot_from_official_csv(
        profile="danbooru", tags_csv=tags_csv, aliases_csv=aliases_csv
    )
    by_name = {row["name"]: row["category"] for row in document["tags"]}
    assert by_name == {"solo": "general", "some_artist": "artist", "commentary": "meta"}

    path = _write(tmp_path / "snap.json", document)
    rules = load_classification_rules(path)
    assert rules.profile == "danbooru"
