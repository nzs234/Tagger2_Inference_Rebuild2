"""Tests for the offline pipeline phase helpers.

These cover the shared projection builder used by both the NL stage and the
upstream projection build, plus the phase decomposition contract of
``workflow/pipeline.py``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from tagger2.workflow.dataset_import import ImportedSample
from tagger2.workflow.pipeline import (
    NINE_FIELDS,
    _projection_for_sample,
    _run_offline_pipeline_impl,
    build_projection,
    run_offline_pipeline,
)


def _sample(**overrides: object) -> ImportedSample:
    values: dict[str, object] = {
        "sample_id": 1,
        "relative_image_path": "img/one.png",
        "annotation_key": "img/one",
        "image_format": "png",
        "annotation_kind": "tag_txt",
        "txt_present": True,
        "json_present": False,
        "tags": ("a", "b"),
    }
    values.update(overrides)
    return ImportedSample(**values)  # type: ignore[arg-type]


def test_projection_for_sample_standard_json_normalizes_missing(tmp_path: Path) -> None:
    """The upstream build coerces missing standard JSON fields to []/\"\"."""
    sample = _sample(annotation_kind="standard_json", json_present=True)
    (tmp_path / "img").mkdir()
    document = {"quality": None, "count": None, "character": "renge", "nl": None}
    (tmp_path / "img" / "one.json").write_text(json.dumps(document), encoding="utf-8")

    projection = _projection_for_sample(
        sample,
        source_root=tmp_path,
        classified_projections={},
        caption_tags={},
        caption_enabled=False,
        normalize_missing=True,
        skip_uncaptioned=True,
    )

    assert projection == {
        "quality": [],
        "count": "",
        "character": "renge",
        "series": "",
        "artist": "",
        "appearance": [],
        "tags": ["a", "b"],
        "environment": [],
        "nl": "",
    }
    assert set(projection or {}) == set(NINE_FIELDS)


def test_projection_for_sample_standard_json_keeps_missing_for_nl(tmp_path: Path) -> None:
    """The NL variant keeps the raw document values, tags fallback included."""
    sample = _sample(annotation_kind="standard_json", json_present=True)
    (tmp_path / "img").mkdir()
    document = {"quality": None, "count": None, "character": "renge", "nl": None}
    (tmp_path / "img" / "one.json").write_text(json.dumps(document), encoding="utf-8")

    projection = _projection_for_sample(
        sample,
        source_root=tmp_path,
        classified_projections={},
        caption_tags={},
        caption_enabled=False,
        normalize_missing=False,
        skip_uncaptioned=False,
    )

    assert projection is not None
    assert projection["quality"] is None
    assert projection["count"] is None
    assert projection["nl"] is None
    assert projection["tags"] == ["a", "b"]


def test_projection_for_sample_raw_e621_classified_overlay() -> None:
    """Raw e621 keeps artist/character and takes classify tags minus its character."""
    sample = _sample(
        annotation_kind="raw_e621_json",
        tags=("a", "one"),
        artist="artist1",
        character="one",
    )
    classified = {
        "img/one.png": {
            "quality": ["good"],
            "tags": ["one", "two"],
            "character": ["one"],
            "artist": ["artist2"],
            "appearance": ["ap"],
            "environment": ["env"],
        }
    }

    projection = _projection_for_sample(
        sample,
        source_root=Path("."),
        classified_projections=classified,
        caption_tags={},
        caption_enabled=False,
        normalize_missing=True,
        skip_uncaptioned=True,
    )

    assert projection == dict(build_projection(sample)) | {
        "quality": ["good"],
        "tags": ["two"],
        "appearance": ["ap"],
        "environment": ["env"],
    }


def test_projection_for_sample_tag_txt_overlay_artist_merge() -> None:
    """tag_txt samples take the classify character/artist/tags overlay."""
    sample = _sample(annotation_kind="tag_txt", artist="artist1")
    classified = {
        "img/one.png": {
            "quality": ["q"],
            "tags": ["t1"],
            "character": ["chi"],
            "artist": ["artist2"],
            "appearance": [],
            "environment": [],
        }
    }

    projection = _projection_for_sample(
        sample,
        source_root=Path("."),
        classified_projections=classified,
        caption_tags={},
        caption_enabled=True,
        normalize_missing=True,
        skip_uncaptioned=True,
    )

    assert projection is not None
    assert projection["character"] == "chi"
    assert projection["tags"] == ["t1"]
    assert projection["artist"] == "artist1, artist2"


def test_projection_for_sample_caption_fallback_and_skip() -> None:
    """Caption tags fill an unclassified projection; the upstream build skips
    caption-enabled samples without any tags while the NL variant does not."""
    empty = _sample(
        sample_id=2,
        relative_image_path="img/two.png",
        annotation_key="img/two",
    )

    upstream = _projection_for_sample(
        empty,
        source_root=Path("."),
        classified_projections={},
        caption_tags={},
        caption_enabled=True,
        normalize_missing=True,
        skip_uncaptioned=True,
    )
    assert upstream is None

    nl_variant = _projection_for_sample(
        empty,
        source_root=Path("."),
        classified_projections={},
        caption_tags={"img/two.png": ("x",)},
        caption_enabled=True,
        normalize_missing=False,
        skip_uncaptioned=False,
    )
    assert nl_variant is not None
    assert nl_variant["tags"] == ["x"]

    # An import whose caption was not requested still produces a projection.
    no_caption = _projection_for_sample(
        empty,
        source_root=Path("."),
        classified_projections={},
        caption_tags={},
        caption_enabled=False,
        normalize_missing=True,
        skip_uncaptioned=True,
    )
    assert no_caption is not None
    assert no_caption["tags"] == ["a", "b"]


def test_phase_helpers_exist() -> None:
    """The orchestrator delegates to the extracted phase functions."""
    import tagger2.workflow.pipeline as pipeline_module

    for name in (
        "_load_review_overlays",
        "_prepare_imports",
        "_restore_checkpoint",
        "_write_workspace_snapshots",
        "_run_caption_phase",
        "_run_ocr_phase",
        "_run_classify_phase",
        "_prepare_replacement_rules",
        "_prepare_staging",
        "_prepare_export_state",
        "_run_nl_phase",
        "_build_upstream_projections",
        "_export_one_sample",
        "_run_export_phase",
        "_sync_control_plane",
        "_finalize_report",
        "_projection_for_sample",
        "PipelineContext",
        "PipelineState",
    ):
        assert hasattr(pipeline_module, name), name


def test_run_offline_pipeline_public_signature_stable() -> None:
    """The public entry point keeps its keyword contract with workflow/api.py."""
    parameters = inspect.signature(run_offline_pipeline).parameters
    assert list(parameters) == [
        "config",
        "source_root",
        "output_root",
        "workspace",
        "replacement_index_path",
        "resource_fingerprints",
        "resource_manifests",
        "tag_predictor",
        "policy_config",
        "token_counter",
        "classification_rules",
        "ocr_engine",
        "nl_client",
        "database",
        "job_id",
        "resource_verifier",
    ]
    public_names = list(parameters)
    impl_names = list(inspect.signature(_run_offline_pipeline_impl).parameters)
    # The internal entry point is the public signature with stage_tracker
    # inserted before resource_verifier.
    assert impl_names == public_names[:15] + ["stage_tracker"] + public_names[15:]
