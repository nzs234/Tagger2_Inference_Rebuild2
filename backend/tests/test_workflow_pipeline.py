"""End-to-end tests for the offline e621 vertical."""

import json
import tempfile
import zipfile
from pathlib import Path

import pytest
from PIL import Image


def _write_image(path: Path, size=(8, 8), mode="RGB", fmt=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, (120, 30, 30) if mode == "RGB" else (120, 30, 30, 128)).save(path, format=fmt)
    return path


def _index(path: Path) -> Path:
    path.write_text(
        "source_tag,canonical_e621_tag,action,replacement_tags\n"
        "anthro,anthro,replace,furry\n"
        "male,male,pass,male\n"
        "watermark,watermark,drop,\n"
        "duo_focus,duo_focus,replace,duo|focus\n",
        encoding="utf-8",
    )
    return path


def _config(source_root, output_root, work_mode="full_copy", export_format="both", replace=True):
    from backend.tagger2.workflow.contracts import WorkflowJobConfigV1

    return WorkflowJobConfigV1.from_payload(
        {
            "profile": "e621",
            "work_mode": work_mode,
            "overwrite_mode": "incremental",
            "source_root": {"root_id": "in", "relative_path": ""},
            "output_root": {"root_id": "out", "relative_path": ""},
            "caption": {"enabled": False, "input_txt_mode": "tag"},
            "classify": {"enabled": False},
            "replace": {"enabled": replace},
            "ocr": {"enabled": False},
            "nl": {"enabled": False},
            "token_budget": {"enabled": False},
            "export": {"format": export_format},
        }
    )


def test_import_classifies_mixed_annotation_formats():
    """Tag TXT, NL TXT, standard JSON, raw e621 JSON and bare images coexist."""
    from backend.tagger2.workflow.dataset_import import import_dataset

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        _write_image(root / "bare.png")

        _write_image(root / "tagged.png")
        (root / "tagged.txt").write_text("male, anthro, male", encoding="utf-8")

        _write_image(root / "standard.png")
        (root / "standard.json").write_text(
            json.dumps(
                {
                    "quality": [],
                    "count": "solo",
                    "character": "rex",
                    "series": "",
                    "artist": "",
                    "appearance": [],
                    "tags": ["male"],
                    "environment": [],
                    "nl": "",
                }
            ),
            encoding="utf-8",
        )

        _write_image(root / "raw.png")
        (root / "raw.json").write_text(
            json.dumps(
                {
                    "artist": ["studio"],
                    "character": ["rex"],
                    "contributor": [],
                    "copyright": ["series_x"],
                    "general": ["male", "anthro"],
                    "invalid": [],
                    "lore": [],
                    "meta": ["watermark"],
                    "species": ["wolf"],
                }
            ),
            encoding="utf-8",
        )

        result = import_dataset(root, recursive=False, input_txt_mode="tag")
        kinds = {
            sample.relative_image_path: sample.annotation_kind for sample in result.samples
        }
        assert kinds == {
            "bare.png": "none",
            "tagged.png": "tag_txt",
            "standard.png": "standard_json",
            "raw.png": "raw_e621_json",
        }
        assert not result.issues

        by_path = {sample.relative_image_path: sample for sample in result.samples}
        # Duplicate tags collapse, preserving first-seen order.
        assert by_path["tagged.png"].tags == ("male", "anthro")
        assert by_path["tagged.png"].skip_caption is True
        # Raw e621 contributes artist/character and skips the tagger.
        assert by_path["raw.png"].artist == "studio"
        assert by_path["raw.png"].character == "rex"
        assert by_path["raw.png"].skip_caption is True
        assert by_path["bare.png"].skip_caption is False


def test_import_nl_txt_mode_writes_nl_not_tags():
    """In NL mode a TXT populates `nl` and never becomes tags."""
    from backend.tagger2.workflow.dataset_import import import_dataset

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_image(root / "a.png")
        (root / "a.txt").write_text("A wolf   stands\nin snow.", encoding="utf-8")

        result = import_dataset(root, recursive=False, input_txt_mode="nl")
        sample = result.samples[0]
        assert sample.annotation_kind == "nl_txt"
        assert sample.nl == "A wolf stands in snow."
        assert sample.tags == ()
        # NL input still needs the tagger for classification tags.
        assert sample.skip_caption is False


def test_import_rejects_corrupt_raw_e621_json_without_fallback():
    """A malformed raw e621 document blocks that sample instead of falling back."""
    from backend.tagger2.workflow.dataset_import import import_dataset

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        _write_image(root / "broken.png")
        # Recognisable as raw e621 (distinctive groups) but missing required groups.
        (root / "broken.json").write_text(
            json.dumps({"general": ["male"], "meta": [], "lore": []}), encoding="utf-8"
        )

        result = import_dataset(root, recursive=False)
        assert result.samples == ()
        assert len(result.issues) == 1
        assert result.issues[0].code == "annotation_invalid"
        assert result.issues[0].blocking is True


def test_import_rejects_multi_frame_and_unsupported_images():
    """Animated images are rejected; unrelated extensions are simply skipped."""
    from backend.tagger2.workflow.dataset_import import import_dataset

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        frames = [Image.new("RGB", (8, 8), (index * 40, 0, 0)) for index in (1, 2, 3)]
        frames[0].save(
            root / "anim.webp",
            format="WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        _write_image(root / "ok.png")
        (root / "notes.md").write_text("ignored", encoding="utf-8")

        result = import_dataset(root, recursive=False)
        assert [sample.relative_image_path for sample in result.samples] == ["ok.png"]
        assert [issue.code for issue in result.issues] == ["image_invalid"]
        assert "notes.md" in result.skipped_files


def test_offline_pipeline_full_copy_exports_json_and_flat_txt():
    """The vertical produces nine-field JSON plus flat TXT and commits atomically."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output, workspace = root / "src", root / "out", root / "ws"
        source.mkdir()
        output.mkdir()

        _write_image(source / "a.png")
        (source / "a.txt").write_text("male, anthro, watermark, duo_focus", encoding="utf-8")

        report = run_offline_pipeline(
            _config(source, output),
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=_index(root / "index.csv"),
        )

        assert report.total_samples == 1
        assert report.exported_samples == 1
        assert report.failed_samples == 0
        assert report.committed_files == 2
        assert report.replacement["dropped"] == 1
        assert report.replacement["replaced"] == 2

        payload = json.loads((output / "a.json").read_text(encoding="utf-8"))
        assert list(payload) == [
            "quality",
            "count",
            "character",
            "series",
            "artist",
            "appearance",
            "tags",
            "environment",
            "nl",
        ]
        # `male` passes through, `anthro`->`furry`, `watermark` dropped, `duo_focus` expanded.
        assert payload["tags"] == ["male", "furry", "duo", "focus"]

        flat = (output / "a.txt").read_text(encoding="utf-8")
        assert flat == "male, furry, duo, focus."

        # The source dataset is untouched in full_copy mode.
        assert (source / "a.txt").read_text(encoding="utf-8") == "male, anthro, watermark, duo_focus"

        # Workspace provenance is recorded.
        manifest = (workspace / "input_manifest.jsonl").read_text(encoding="utf-8").strip()
        assert json.loads(manifest)["relative_image_path"] == "a.png"
        assert (workspace / "config_snapshot.json").is_file()
        journal = (workspace / "commit_journal.jsonl").read_text(encoding="utf-8")
        assert "commit_completed" in journal


def test_offline_pipeline_in_place_creates_verified_backup():
    """in_place mode backs up original annotations before overwriting them."""
    from backend.tagger2.workflow.commit import restore_annotation_backup
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, workspace = root / "src", root / "ws"
        source.mkdir()

        _write_image(source / "a.png")
        original = "male, anthro"
        (source / "a.txt").write_text(original, encoding="utf-8")

        report = run_offline_pipeline(
            _config(source, source, work_mode="in_place"),
            source_root=source,
            output_root=source,
            workspace=workspace,
            replacement_index_path=_index(root / "index.csv"),
        )

        assert report.backup_path is not None
        backup = Path(report.backup_path)
        assert backup.is_file()

        # The dataset was rewritten in place.
        assert (source / "a.txt").read_text(encoding="utf-8") == "male, furry."
        assert (source / "a.json").is_file()

        # The backup contains the original bytes and can restore them.
        with zipfile.ZipFile(backup) as archive:
            assert "a.txt" in archive.namelist()
            assert archive.read("a.txt").decode("utf-8") == original

        restored = restore_annotation_backup(backup, source)
        assert restored == 2
        assert (source / "a.txt").read_text(encoding="utf-8") == original
        # The JSON did not exist before the run, so restore removes ours.
        assert not (source / "a.json").exists()


def test_offline_pipeline_blocking_issue_prevents_commit():
    """A blocking import issue must leave the output dataset empty."""
    from backend.tagger2.workflow.pipeline import run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output, workspace = root / "src", root / "out", root / "ws"
        source.mkdir()
        output.mkdir()

        _write_image(source / "good.png")
        (source / "good.txt").write_text("male", encoding="utf-8")
        _write_image(source / "bad.png")
        (source / "bad.json").write_text(
            json.dumps({"general": ["male"], "lore": []}), encoding="utf-8"
        )

        report = run_offline_pipeline(
            _config(source, output),
            source_root=source,
            output_root=output,
            workspace=workspace,
            replacement_index_path=_index(root / "index.csv"),
        )

        assert report.failed_samples == 1
        assert report.committed_files == 0
        assert list(output.iterdir()) == []
        issues = (workspace / "issues.jsonl").read_text(encoding="utf-8")
        assert "annotation_invalid" in issues
        assert "commit_skipped" in (workspace / "commit_journal.jsonl").read_text(encoding="utf-8")


def test_offline_pipeline_requires_index_when_replace_enabled():
    """Enabling replace without an index is a hard configuration error."""
    from backend.tagger2.workflow.pipeline import PipelineError, run_offline_pipeline

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        source, output = root / "src", root / "out"
        source.mkdir()
        output.mkdir()
        _write_image(source / "a.png")

        with pytest.raises(PipelineError):
            run_offline_pipeline(
                _config(source, output),
                source_root=source,
                output_root=output,
                workspace=root / "ws",
                replacement_index_path=None,
            )


def test_commit_refuses_staged_file_changed_after_validation():
    """A staging tree corrupted after validation is refused, not committed."""
    from backend.tagger2.workflow.commit import (
        CommitError,
        CommitJournal,
        ExportStaging,
        commit_staged_files,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        dataset = root / "dataset"
        dataset.mkdir()
        staging = ExportStaging(root / "staging")
        staged = staging.stage("a.json", b'{"ok": true}')

        # Tamper with the staged bytes after they were validated.
        staging.staged_path("a.json").write_bytes(b'{"ok": false}')

        with pytest.raises(CommitError):
            commit_staged_files(dataset, staging, [staged], CommitJournal(root / "journal.jsonl"))
        assert not (dataset / "a.json").exists()


def test_alpha_images_composite_onto_white():
    """RGBA input is composited on white rather than losing its alpha silently."""
    from backend.tagger2.workflow.dataset_import import load_normalized_image

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "a.png"
        Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(path)
        image = load_normalized_image(path)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (255, 255, 255)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
