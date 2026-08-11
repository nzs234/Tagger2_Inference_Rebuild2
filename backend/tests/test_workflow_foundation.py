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
        
        # Create a job
        job_id = db.create_job(
            config_json={"profile": "e621", "work_mode": "full_copy"},
            config_hash="test_hash_123",
            profile="e621",
            work_mode="full_copy",
            overwrite_mode="incremental",
            source_root_id="root1",
            output_root_id="root2",
            workspace_path="/tmp/workspace/job123",
        )
        
        assert job_id
        assert len(job_id) == 32  # UUID hex
        
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
    """Test workflow resource catalog operations."""
    from backend.tagger2.workflow.resources import WorkflowResourceCatalog
    
    with tempfile.TemporaryDirectory() as tmpdir:
        resource_dir = Path(tmpdir) / "resources"
        catalog = WorkflowResourceCatalog(resource_dir)
        
        # Create a test CSV file
        csv_path = Path(tmpdir) / "test_replace.csv"
        csv_content = """source,action,target
tag1,keep,
tag2,replace,new_tag2
tag3,drop,
"""
        csv_path.write_text(csv_content, encoding="utf-8")
        
        # Validate CSV
        validation = catalog.validate_csv_resource(csv_path)
        assert validation["valid"] is True
        assert validation["line_count"] == 3
        assert len(validation["errors"]) == 0
        
        # Import resource
        manifest = catalog.import_resource(
            source_path=csv_path,
            resource_id="replace-test-v1",
            category="replace",
        )
        
        assert manifest.resource_id == "replace-test-v1"
        assert manifest.category == "replace"
        assert len(manifest.resource_fingerprint) == 64
        
        # Get manifest
        retrieved = catalog.get_manifest("replace-test-v1")
        assert retrieved is not None
        assert retrieved.resource_id == manifest.resource_id
        assert retrieved.resource_fingerprint == manifest.resource_fingerprint
        
        # List resources
        resources = catalog.list_resources()
        assert len(resources) == 1
        assert resources[0].resource_id == "replace-test-v1"
        
        # Get resource path
        resource_path = catalog.get_resource_path("replace-test-v1")
        assert resource_path is not None
        assert resource_path.exists()
        
        # Test invalid CSV
        bad_csv_path = Path(tmpdir) / "bad.csv"
        bad_csv_path.write_text("source,action\ntag1,invalid_action\n", encoding="utf-8")
        bad_validation = catalog.validate_csv_resource(bad_csv_path)
        assert bad_validation["valid"] is False
        assert len(bad_validation["errors"]) > 0


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
