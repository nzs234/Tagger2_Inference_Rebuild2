"""Fail-closed behaviour for profiles and classification snapshots."""

import json
from pathlib import Path

import pytest


def _service(tmp_path: Path):
    from tagger2.security import PathAllowlist
    from tagger2.workflow.db import WorkflowDatabase
    from tagger2.workflow.preflight import WorkflowPreflightService
    from tagger2.workflow.resources import WorkflowResourceCatalog

    source = tmp_path / "input"
    source.mkdir(exist_ok=True)
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)

    allowlist = PathAllowlist()
    allowlist.register(source, kind="input", root_id="in", label="in")
    allowlist.register(output, kind="output", root_id="out", label="out", writable=True)

    catalog = WorkflowResourceCatalog(tmp_path / "resources")
    db = WorkflowDatabase(":memory:")
    return WorkflowPreflightService(allowlist, catalog, db), catalog


def _config(**overrides):
    from tagger2.workflow.contracts import WorkflowJobConfigV1

    payload = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "in", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": False},
        "replace": {"enabled": False},
        "ocr": {"enabled": False},
        "nl": {"enabled": False},
        "token_budget": {"enabled": False},
        "export": {"format": "json"},
    }
    payload.update(overrides)
    return WorkflowJobConfigV1.from_payload(payload)


def _register_snapshot(catalog, tmp_path: Path, resource_id: str, profile: str):
    document = {
        "format": "classify-snapshot-v1",
        "profile": profile,
        "tags": [{"name": "solo", "category": "general", "post_count": 1}],
        "aliases": [],
        "implications": [],
    }
    staged = tmp_path / f"{resource_id}.json"
    staged.write_text(json.dumps(document), encoding="utf-8")
    catalog.import_resource(source_path=staged, resource_id=resource_id, category="classify")


def test_classify_without_a_resource_id_is_blocking(tmp_path: Path):
    """An enabled Classify stage with no snapshot selected must not run."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, _catalog = _service(tmp_path)
    config = _config(classify={"enabled": True, "resource_id": ""})

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    errors = excinfo.value.details["errors"]
    assert any("no classification snapshot is selected" in error for error in errors)


def test_classify_with_unregistered_resource_is_blocking(tmp_path: Path):
    """A snapshot that was never imported is an error, not a warning."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, _catalog = _service(tmp_path)
    config = _config(classify={"enabled": True, "resource_id": "classify-missing-v1"})

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    errors = excinfo.value.details["errors"]
    assert any("Classify resource not found" in error for error in errors)


def test_e621_job_refuses_a_danbooru_snapshot(tmp_path: Path):
    """A cross-profile snapshot must be refused rather than silently accepted."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, catalog = _service(tmp_path)
    _register_snapshot(catalog, tmp_path, "classify-danbooru-v1", "danbooru")

    config = _config(
        profile="e621", classify={"enabled": True, "resource_id": "classify-danbooru-v1"}
    )

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    errors = excinfo.value.details["errors"]
    assert any("no cross-profile fallback is allowed" in error for error in errors)


def test_danbooru_job_refuses_an_e621_snapshot(tmp_path: Path):
    """The reverse direction is refused too: no falling back to e621."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, catalog = _service(tmp_path)
    _register_snapshot(catalog, tmp_path, "classify-e621-v1", "e621")

    config = _config(
        profile="danbooru", classify={"enabled": True, "resource_id": "classify-e621-v1"}
    )

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    errors = excinfo.value.details["errors"]
    assert any("job profile" in error and "danbooru" in error for error in errors)


def test_matching_profile_passes_preflight(tmp_path: Path):
    """A snapshot built for the job's profile validates cleanly."""
    service, catalog = _service(tmp_path)
    _register_snapshot(catalog, tmp_path, "classify-e621-v1", "e621")

    config = _config(
        profile="e621", classify={"enabled": True, "resource_id": "classify-e621-v1"}
    )

    report = service.validate_config(config)
    assert report["valid"] is True
    assert report["errors"] == []


def test_danbooru_profile_warns_and_blocks_replace_without_a_resource(tmp_path: Path):
    """Danbooru stays selectable, but Replace cannot borrow the e621 index."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, _catalog = _service(tmp_path)
    config = _config(profile="danbooru", replace={"enabled": True, "resource_id": ""})

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    details = excinfo.value.details
    assert any(
        "not a valid substitute" in error for error in details["errors"]
    )
    assert any("not bundled" in warning for warning in details["warnings"])


def test_danbooru_profile_alone_is_only_a_warning(tmp_path: Path):
    """Selecting Danbooru with every dependent stage off is allowed."""
    service, _catalog = _service(tmp_path)
    config = _config(profile="danbooru")

    report = service.validate_config(config)
    assert report["valid"] is True
    assert any("not bundled" in warning for warning in report["warnings"])


def test_nl_provider_without_image_input_is_blocking(tmp_path: Path):
    """API NL jobs must provide image context when a provider is selected."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, _catalog = _service(tmp_path)
    config = _config(
        nl={
            "enabled": True,
            "provider_id": "provider-a",
            "use_image": False,
        }
    )

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    assert any("requires image input" in error for error in excinfo.value.details["errors"])


def test_unreadable_snapshot_is_blocking(tmp_path: Path):
    """A registered file that is not valid JSON must not be treated as usable."""
    from tagger2.workflow.preflight import WorkflowPreflightError

    service, catalog = _service(tmp_path)
    staged = tmp_path / "broken.json"
    staged.write_text("{not json", encoding="utf-8")
    catalog.import_resource(
        source_path=staged, resource_id="classify-broken-v1", category="classify"
    )

    config = _config(classify={"enabled": True, "resource_id": "classify-broken-v1"})

    with pytest.raises(WorkflowPreflightError) as excinfo:
        service.validate_config(config)
    errors = excinfo.value.details["errors"]
    assert any("cannot be read" in error for error in errors)










