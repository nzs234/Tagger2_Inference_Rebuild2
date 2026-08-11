"""End-to-end test for workflow job execution."""

import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture()
def e2e_client(monkeypatch):
    """Build the real application with test images."""
    from backend.tagger2.config import AppConfig, reset_settings_cache
    from backend.tagger2.main import create_app

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        (root / "models").mkdir()

        # Create test images with TXT tags
        for i in range(3):
            img = Image.new("RGB", (256, 256), color=(i * 50, 100, 150))
            img.save(input_dir / f"test_{i}.jpg")
            # Add TXT file with tags
            txt_path = input_dir / f"test_{i}.txt"
            txt_path.write_text(f"1girl solo character_name_{i} quality_tag")

        monkeypatch.setenv("TAGGER2_DATA_DIR", str(root / "data"))
        monkeypatch.setenv("TAGGER2_CACHE_DIR", str(root / "cache"))
        monkeypatch.setenv("TAGGER2_LOG_DIR", str(root / "logs"))
        monkeypatch.setenv("TAGGER2_ALLOW_LAN", "0")
        reset_settings_cache()

        settings = AppConfig.from_env()
        settings.ensure_directories()
        app = create_app(settings)

        # Register paths in allowlist
        runtime = app.state.runtime
        runtime.allowlist.register(
            input_dir, kind="input", root_id="testinput", label="Test input"
        )
        runtime.allowlist.register(
            output_dir, kind="output", root_id="testoutput", label="Test output", writable=True
        )

        from fastapi.testclient import TestClient
        yield TestClient(app), root, input_dir, output_dir


def test_job_executes_to_completion_without_caption(e2e_client):
    """A minimal job transitions from pending -> running -> completed."""
    client, _root, _input_dir, output_dir = e2e_client

    config = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "testinput", "relative_path": ""},
        "output_root": {"root_id": "testoutput", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    }

    # Create job
    created = client.post("/api/v1/workflows/jobs", json={"config": config})
    assert created.status_code == 200, created.text
    job_id = created.json()["job_id"]
    assert created.json()["status"] == "pending"

    # Poll until job completes (with timeout)
    max_wait = 30
    start = time.time()
    final_status = None

    while time.time() - start < max_wait:
        status_resp = client.get(f"/api/v1/workflows/jobs/{job_id}")
        assert status_resp.status_code == 200
        status = status_resp.json()["status"]
        
        if status in ("completed", "failed"):
            final_status = status
            break
        
        time.sleep(0.5)

    assert final_status is not None, "Job did not complete within timeout"

    # Surface the recorded diagnosis when the run did not finish cleanly.
    if final_status == "failed":
        issues = client.get(f"/api/v1/workflows/jobs/{job_id}/issues").json()
        report = client.get(f"/api/v1/workflows/jobs/{job_id}/report").json()
        pytest.fail(f"job failed: issues={issues} report={report}")

    assert final_status == "completed", f"Job failed: {status_resp.json()}"

    # Verify output files were created
    output_files = list(output_dir.glob("*.jpg"))
    assert len(output_files) == 3, f"Expected 3 output images, got {len(output_files)}"

    # Verify JSON files were created
    json_files = list(output_dir.glob("*.json"))
    assert len(json_files) == 3, f"Expected 3 JSON files, got {len(json_files)}"

    # Check report is available
    report_resp = client.get(f"/api/v1/workflows/jobs/{job_id}/report")
    assert report_resp.status_code == 200
    report_body = report_resp.json()
    assert report_body["available"] is True
    assert report_body["report"]["exported_samples"] == 3


def test_job_creation_rejects_invalid_path(e2e_client):
    """Preflight rejects an unallowlisted source root before creating a job."""
    client, _root, _input_dir, _output_dir = e2e_client

    config = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "invalid_root", "relative_path": ""},
        "output_root": {"root_id": "testoutput", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
    }

    # Preflight fails closed: the job is never created.
    created = client.post("/api/v1/workflows/jobs", json={"config": config})
    assert created.status_code == 400, created.text
    body = created.json()
    assert body["code"] == "preflight_failed"
    assert "not allowed" in body["message"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
