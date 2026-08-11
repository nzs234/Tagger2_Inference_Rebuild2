"""Test importing the designated e621 replacement index."""

import pytest
from pathlib import Path


DESIGNATED_CSV_PATH = Path(r"D:\QQ相关\下载\E621tag替换索引\e621_general_tag_replacement_index.csv")
EXPECTED_SHA256 = "4834C1CDA2CD560641A7CD67D7CEF8D99D381A89F7BEAB4A86E2EF4F90643DED"
EXPECTED_SIZE = 7418754
EXPECTED_DATA_ROWS = 155706


@pytest.mark.skipif(not DESIGNATED_CSV_PATH.exists(), reason="Designated CSV not available")
def test_designated_csv_exists_and_matches_fingerprint():
    """The designated e621 replacement CSV must match the documented fingerprint."""
    from backend.tagger2.workflow.resources import WorkflowResourceCatalog
    
    catalog = WorkflowResourceCatalog(Path("data/workflows/resources"))
    
    assert DESIGNATED_CSV_PATH.is_file(), f"CSV not found: {DESIGNATED_CSV_PATH}"
    assert DESIGNATED_CSV_PATH.stat().st_size == EXPECTED_SIZE
    
    fingerprint = catalog.fingerprint_file(DESIGNATED_CSV_PATH)
    assert fingerprint.upper() == EXPECTED_SHA256


@pytest.mark.skipif(not DESIGNATED_CSV_PATH.exists(), reason="Designated CSV not available")
def test_designated_csv_validates_cleanly():
    """The designated index must validate with expected counts."""
    from backend.tagger2.workflow.replacement_index import validate_replacement_index
    
    report = validate_replacement_index(DESIGNATED_CSV_PATH)
    
    assert report.valid is True, f"Validation errors: {report.errors}"
    assert report.errors == []
    assert not report.truncated
    
    # Expected breakdown from compatibility report
    assert report.action_counts["keep"] == 47095
    assert report.action_counts["replace"] == 3171
    assert report.action_counts["drop"] == 644
    assert report.action_counts["pass"] == 104796
    
    # 50,910 executable rules (keep + replace + drop)
    assert report.rule_count == 50910
    assert report.passthrough_count == 104796
    
    # 115 replace rules expand to multiple tags
    assert report.pipe_replacement_count == 115


@pytest.mark.skipif(not DESIGNATED_CSV_PATH.exists(), reason="Designated CSV not available")
def test_designated_csv_loads_without_error():
    """The designated index must load into an executable rule table."""
    from backend.tagger2.workflow.replacement_index import load_replacement_rules
    
    rules = load_replacement_rules(DESIGNATED_CSV_PATH)
    
    # 50,910 executable rules (passthrough rows are omitted)
    assert len(rules) == 50910
    
    # Spot check from compatibility report: anthro->furry, fur->body_fur
    assert "anthro" in rules
    assert rules["anthro"].action == "replace"
    assert rules["anthro"].replacement_tags == ("furry",)
    
    assert "fur" in rules
    assert rules["fur"].action == "replace"
    assert rules["fur"].replacement_tags == ("body_fur",)
    
    # male and solo have keep rules (not pass)
    assert "male" in rules
    assert rules["male"].action == "keep"
    
    assert "solo" in rules
    assert rules["solo"].action == "keep"
    
    # watermark has no rule (true passthrough)
    assert "watermark" not in rules


@pytest.mark.skipif(not DESIGNATED_CSV_PATH.exists(), reason="Designated CSV not available")
def test_designated_csv_spot_check_replacement():
    """Verify spot-check replacements from compatibility report."""
    from backend.tagger2.workflow.replacement_index import load_replacement_rules
    from backend.tagger2.workflow.stages.replacement import replace_projection
    
    rules = load_replacement_rules(DESIGNATED_CSV_PATH)
    
    # Spot check: male, anthro, watermark, duo_focus, solo, fur, forest
    input_projection = {
        "quality": [],
        "count": "",
        "character": "",
        "series": "",
        "artist": "",
        "appearance": [],
        "tags": ["male", "anthro", "watermark", "duo_focus", "solo", "fur", "forest"],
        "environment": [],
        "nl": "",
    }
    
    result, summary = replace_projection(input_projection, rules)
    
    # Expected: anthro->furry, fur->body_fur, others kept or passthrough
    assert "furry" in result["tags"]
    assert "body_fur" in result["tags"]
    assert "male" in result["tags"]
    assert "watermark" in result["tags"]
    assert "solo" in result["tags"]
    assert "forest" in result["tags"]
    
    # duo_focus depends on the actual rule in CSV
    assert "anthro" not in result["tags"]  # replaced
    assert "fur" not in result["tags"]  # replaced
    
    # Verify summary
    assert summary.replaced >= 2  # At least anthro and fur


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
