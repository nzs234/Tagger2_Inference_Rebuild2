"""Test workflow foundation: contracts, database, resources, preflight, API."""

import json
import tempfile
from pathlib import Path

import pytest


def test_workflow_contracts():
    """Test workflow contract creation and validation."""
    from backend.tagger2.workflow.contracts import (
        WorkflowJobConfigV1,
        WorkflowPathRef,
        WorkflowResourceManifestV1,
        utc_now,
        sha256_json,
    )
    
    # Test WorkflowPathRef
    path_ref = WorkflowPathRef(root_id="test_root", relative_path="images/batch1")
    assert path_ref.root_id == "test_root"
    assert path_ref.relative_path == "images/batch1"
    
    # Test WorkflowJobConfigV1
    config = WorkflowJobConfigV1(
        profile="e621",
        work_mode="full_copy",
        overwrite_mode="incremental",
        source_root=WorkflowPathRef(root_id="input_root", relative_path="dataset"),
        output_root=WorkflowPathRef(root_id="output_root", relative_path="processed"),
    )
    assert config.profile == "e621"
    assert config.schema_version == 1
    
    # Test config hash stability
    hash1 = config.config_hash()
    hash2 = config.config_hash()
    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex
    
    # Test WorkflowResourceManifestV1
    manifest = WorkflowResourceManifestV1(
        resource_id="test-resource-v1",
        resource_fingerprint="a" * 64,
        category="replace",
        created_at=utc_now(),
    )
    assert manifest.resource_id == "test-resource-v1"
    assert manifest.category == "replace"


def test_workflow_database():
    """Test workflow database operations."""
    from backend.tagger2.workflow.db import WorkflowDatabase
    from backend.tagger2.workflow.db_schema import SCHEMA_VERSION
    
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_workflow.sqlite3"
        db = WorkflowDatabase(db_path)
        
        # Verify database file created
        assert db_path.exists()
        
        # Create a job; the workspace directory is reserved by the database.
        workspace_root = Path(tmpdir) / "jobs"
        job_id, workspace = db.create_job(
            config_json={"profile": "e621", "work_mode": "full_copy"},
            config_hash="test_hash_123",
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root_id="root1",
            output_root_id="root2",
            workspace_root=workspace_root,
        )

        assert job_id
        assert len(job_id) == 32  # UUID hex
        assert workspace == workspace_root / job_id
        assert workspace.is_dir()
        
        # Get job
        job = db.get_job(job_id)
        assert job is not None
        assert job["job_id"] == job_id
        assert job["status"] == "pending"
        assert job["profile"] == "e621"
        
        # Update job status
        db.update_job_status(job_id, "running", current_module_id="caption")
        job = db.get_job(job_id)
        assert job["status"] == "running"
        assert job["current_module_id"] == "caption"
        assert job["started_at"] is not None
        
        # Create sample
        db.create_sample(job_id, 0, "image1.jpg", "jpeg")
        db.create_sample(job_id, 1, "image2.png", "png")
        
        # Update sample status
        db.update_sample_status(job_id, 0, "completed", current_module_id="caption")
        
        # Create issue
        issue_id = db.create_issue(
            job_id=job_id,
            module_id="caption",
            code="model_not_loaded",
            severity="error",
            blocking=True,
            message="Caption model not loaded",
            sample_id=1,
        )
        assert issue_id
        
        # List issues
        issues = db.list_issues(job_id)
        assert len(issues) == 1
        assert issues[0]["code"] == "model_not_loaded"
        assert issues[0]["blocking"] == 1
        
        # List blocking issues only
        blocking = db.list_issues(job_id, blocking_only=True)
        assert len(blocking) == 1


def test_workflow_resource_catalog():
    """Test workflow resource catalog operations against the real CSV contract."""
    from backend.tagger2.workflow.resources import WorkflowResourceCatalog

    with tempfile.TemporaryDirectory() as tmpdir:
        resource_dir = Path(tmpdir) / "resources"
        catalog = WorkflowResourceCatalog(resource_dir)

        csv_path = Path(tmpdir) / "test_replace.csv"
        csv_content = (
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "male,male,keep,male\n"
            "anthro,anthro,replace,furry\n"
            "duo_focus,duo_focus,replace,duo|focus\n"
            "meta_tag,meta_tag,drop,\n"
        )
        csv_path.write_text(csv_content, encoding="utf-8")

        validation = catalog.validate_csv_resource(csv_path)
        assert validation["valid"] is True, validation["errors"]
        assert validation["line_count"] == 4
        assert validation["action_counts"] == {"keep": 1, "replace": 2, "drop": 1, "pass": 0}
        assert validation["pipe_replacement_count"] == 1

        manifest = catalog.import_resource(
            source_path=csv_path,
            resource_id="replace-test-v1",
            category="replace",
        )

        assert manifest.resource_id == "replace-test-v1"
        assert manifest.category == "replace"
        assert len(manifest.resource_fingerprint) == 64

        retrieved = catalog.get_manifest("replace-test-v1")
        assert retrieved is not None
        assert retrieved.resource_fingerprint == manifest.resource_fingerprint

        resources = catalog.list_resources()
        assert [item.resource_id for item in resources] == ["replace-test-v1"]

        resource_path = catalog.get_resource_path("replace-test-v1")
        assert resource_path is not None and resource_path.exists()

        # Import is content-addressed: re-importing the same bytes is stable.
        again = catalog.import_resource(
            source_path=csv_path,
            resource_id="replace-test-v1",
            category="replace",
        )
        assert again.resource_fingerprint == manifest.resource_fingerprint


def test_workflow_replacement_index_rejects_bad_rows():
    """Invalid replacement rows are reported with their line numbers."""
    from backend.tagger2.workflow.replacement_index import (
        ReplacementIndexError,
        load_replacement_rules,
        validate_replacement_index,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        bad = Path(tmpdir) / "bad.csv"
        bad.write_text(
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "a,a,bogus,a\n"
            "b,b,drop,should_be_empty\n"
            "c,c,keep,\n"
            "a,a,keep,a\n",
            encoding="utf-8",
        )
        report = validate_replacement_index(bad)
        assert report.valid is False
        assert len(report.errors) == 4
        assert all(error.startswith("line ") for error in report.errors)
        assert "line 2" in report.errors[0]
        assert "line 5" in report.errors[3]

        wrong_header = Path(tmpdir) / "header.csv"
        wrong_header.write_text("source,action,target\na,keep,a\n", encoding="utf-8")
        header_report = validate_replacement_index(wrong_header)
        assert header_report.valid is False
        assert "header" in header_report.errors[0]

        good = Path(tmpdir) / "good.csv"
        good.write_text(
            "source_tag,canonical_e621_tag,action,replacement_tags\n"
            "male,male,keep,male\n"
            "anthro,anthro,replace,furry|beast\n"
            "junk,junk,drop,\n",
            encoding="utf-8",
        )
        rules = load_replacement_rules(good)
        assert rules["male"].action == "keep"
        assert rules["male"].replacement_tags == ("male",)
        assert rules["anthro"].replacement_tags == ("furry", "beast")
        assert rules["junk"].replacement_tags == ()

        try:
            load_replacement_rules(bad)
        except ReplacementIndexError as exc:
            assert "line 2" in str(exc)
        else:
            raise AssertionError("expected ReplacementIndexError")


def test_workflow_preflight():
    """Test workflow preflight validation."""
    from backend.tagger2.security import PathAllowlist, PathRoot
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1, WorkflowPathRef
    from backend.tagger2.workflow.preflight import WorkflowPreflightError, WorkflowPreflightService
    from backend.tagger2.workflow.resources import WorkflowResourceCatalog
    
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir)
        
        # Create test directories
        input_dir = temp_path / "input"
        output_dir = temp_path / "output"
        resource_dir = temp_path / "resources"
        input_dir.mkdir()
        output_dir.mkdir()
        
        # Setup allowlist
        allowlist = PathAllowlist([
            PathRoot(
                root_id="input",
                path=input_dir,
                label="Input",
                kind="input",
                writable=False,
            ),
            PathRoot(
                root_id="output",
                path=output_dir,
                label="Output",
                kind="output",
                writable=True,
            ),
        ])
        
        # Setup resource catalog
        catalog = WorkflowResourceCatalog(resource_dir)
        
        # Create preflight service
        service = WorkflowPreflightService(allowlist, catalog)
        
        # Test valid configuration
        valid_config = WorkflowJobConfigV1(
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root=WorkflowPathRef(root_id="input", relative_path="."),
            output_root=WorkflowPathRef(root_id="output", relative_path="."),
            caption={"enabled": False},
            classify={"enabled": False},
            replace={"enabled": False},
            ocr={"enabled": False},
            token_budget={"enabled": False},
        )
        
        report = service.validate_config(valid_config)
        assert report["valid"] is True
        assert len(report["errors"]) == 0
        
        # Test invalid configuration: overlapping paths
        invalid_config = WorkflowJobConfigV1(
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root=WorkflowPathRef(root_id="input", relative_path="."),
            output_root=WorkflowPathRef(root_id="input", relative_path="subdir"),
            caption={"enabled": False},
            classify={"enabled": False},
            replace={"enabled": False},
            ocr={"enabled": False},
            token_budget={"enabled": False},
        )
        
        (input_dir / "subdir").mkdir()
        
        with pytest.raises(WorkflowPreflightError) as exc_info:
            service.validate_config(invalid_config)
        
        assert exc_info.value.code == "preflight_failed"
        assert any("overlap" in err.lower() for err in exc_info.value.details["errors"])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
