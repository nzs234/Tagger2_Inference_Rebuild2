"""Integration tests for classification stage in the full pipeline."""

import json
from pathlib import Path


def test_classify_stage_maps_caption_tags_to_nine_fields(tmp_path: Path):
    """Classify stage should map raw caption tags into structured nine-field projection."""
    from backend.tagger2.workflow.stages.classify import build_classification_rules
    from backend.tagger2.workflow.pipeline import run_offline_pipeline
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1

    # Set up minimal classification rules in the correct format
    tags_data = [
        {"name": "solo", "category": "general", "post_count": 10000},
        {"name": "1girl", "category": "general", "post_count": 50000},
        {"name": "long_hair", "category": "general", "post_count": 30000},
        {"name": "rating_safe", "category": "meta", "post_count": 100000},
        {"name": "hatsune_miku", "category": "character", "post_count": 80000},
    ]
    aliases_data = [
        {"antecedent_name": "1girls", "consequent_name": "1girl"},
    ]
    implications_data = []
    
    rules = build_classification_rules("e621", tags_data, aliases_data, implications_data)

    # Create source dataset with tag TXT
    source = tmp_path / "source"
    source.mkdir()
    
    img_path = source / "test.png"
    from PIL import Image
    Image.new("RGB", (64, 64), color="white").save(img_path)
    
    txt_path = source / "test.txt"
    txt_path.write_text("solo, 1girl, long_hair, rating_safe, hatsune_miku", encoding="utf-8")

    # Run pipeline with classification
    output = tmp_path / "output"
    output.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = WorkflowJobConfigV1.from_payload({
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": True},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    })

    report = run_offline_pipeline(
        source_root=source,
        output_root=output,
        workspace=workspace,
        config=config,
        classification_rules=rules,
    )

    assert report.committed_files == 2  # JSON + image
    assert report.failed_samples == 0

    # Verify output has structured fields
    output_json = output / "test.json"
    assert output_json.exists()
    
    data = json.loads(output_json.read_text(encoding="utf-8"))
    
    # Check that tags were classified
    assert "quality" in data
    assert "character" in data
    assert "appearance" in data
    assert "tags" in data
    assert "environment" in data
    
    # rating_safe should map to quality
    assert "rating_safe" in data["quality"]
    
    # hatsune_miku should map to character
    assert "hatsune_miku" in data["character"]


def test_classify_stage_preserves_e621_json_structure(tmp_path: Path):
    """Original e621 JSON should preserve its structure and skip classification."""
    from backend.tagger2.workflow.stages.classify import build_classification_rules
    from backend.tagger2.workflow.pipeline import run_offline_pipeline
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1

    # Minimal rules (won''t be used for e621 JSON)
    rules = build_classification_rules("e621", [], [], [])

    source = tmp_path / "source"
    source.mkdir()
    
    img_path = source / "test.png"
    from PIL import Image
    Image.new("RGB", (64, 64), color="white").save(img_path)
    
    # Original e621 JSON with all required groups
    json_path = source / "test.json"
    e621_data = {
        "general": ["solo", "long_hair"],
        "character": ["hatsune_miku"],
        "copyright": ["vocaloid"],
        "artist": ["artist_name"],
        "species": ["human"],
        "meta": ["rating_safe"],
        "contributor": [],
        "invalid": [],
        "lore": [],
    }
    json_path.write_text(json.dumps(e621_data), encoding="utf-8")

    output = tmp_path / "output"
    output.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = WorkflowJobConfigV1.from_payload({
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": True},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    })

    report = run_offline_pipeline(
        source_root=source,
        output_root=output,
        workspace=workspace,
        config=config,
        classification_rules=rules,
    )

    assert report.committed_files == 2  # JSON + image
    assert report.failed_samples == 0

    # Verify e621 structure was preserved
    output_json = output / "test.json"
    assert output_json.exists()
    
    data = json.loads(output_json.read_text(encoding="utf-8"))
    
    # Should have the nine-field structure with classified tags
    assert data.get("character") == "hatsune_miku"
    assert data.get("artist") == "artist_name"
    assert "quality" in data
    assert "tags" in data


def test_classify_failure_creates_issue_and_continues(tmp_path: Path):
    """If classification fails for one sample, it should record an issue and continue."""
    from backend.tagger2.workflow.stages.classify import ClassificationRules
    from backend.tagger2.workflow.pipeline import run_offline_pipeline
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1

    # Create rules that will work normally
    rules = ClassificationRules(
        profile="e621",
        tags={},
        aliases={},
        implications={},
    )

    source = tmp_path / "source"
    source.mkdir()

    # Create two samples: one good, one that might cause issues
    img1 = source / "good.png"
    from PIL import Image
    Image.new("RGB", (64, 64), color="white").save(img1)
    txt1 = source / "good.txt"
    txt1.write_text("solo", encoding="utf-8")

    img2 = source / "unknown_tags.png"
    Image.new("RGB", (64, 64), color="blue").save(img2)
    txt2 = source / "unknown_tags.txt"
    txt2.write_text("completely_unknown_tag, another_unknown", encoding="utf-8")

    output = tmp_path / "output"
    output.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = WorkflowJobConfigV1.from_payload({
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": True},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    })

    report = run_offline_pipeline(
        source_root=source,
        output_root=output,
        workspace=workspace,
        config=config,
        classification_rules=rules,
    )

    # Both samples should still be processed
    assert report.committed_files == 4  # 2 JSON + 2 images
    # Unknown tags should not cause failures, just pass through
    assert report.failed_samples == 0


def test_classify_keeps_artist_tags_instead_of_dropping_them(tmp_path: Path):
    """An artist-category tag reaches the `artist` field rather than vanishing."""
    from backend.tagger2.workflow.stages.classify import build_classification_rules
    from backend.tagger2.workflow.pipeline import run_offline_pipeline
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1
    from PIL import Image

    rules = build_classification_rules(
        "e621",
        [
            {"name": "solo", "category": "general", "post_count": 10},
            {"name": "some_artist", "category": "artist", "post_count": 10},
        ],
        [],
        [],
    )

    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (64, 64), color="white").save(source / "test.png")
    (source / "test.txt").write_text("solo, some_artist", encoding="utf-8")

    output = tmp_path / "output"
    output.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = WorkflowJobConfigV1.from_payload({
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": True},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    })

    report = run_offline_pipeline(
        source_root=source,
        output_root=output,
        workspace=workspace,
        config=config,
        classification_rules=rules,
    )

    assert report.failed_samples == 0
    data = json.loads((output / "test.json").read_text(encoding="utf-8"))

    assert data["artist"] == "some_artist"
    # The artist tag must not be duplicated into `tags`.
    assert "some_artist" not in data["tags"]
    assert "solo" in data["tags"]
    # `series` stays empty for e621, matching the frozen source behaviour.
    assert data["series"] == ""
