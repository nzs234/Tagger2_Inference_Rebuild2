"""Guards that the verbatim-ported source algorithms stay unmodified.

If a port genuinely needs to change, update the expected digest here in the same
commit and record the reason in docs/workflow_compatibility_report.md.
"""

import hashlib
from pathlib import Path

import pytest

SOURCE_ROOT = Path(r"E:\AI\e621-standard-capotion-workflow")

PORTS = {
    "backend/tagger2/workflow/caption_format/normalizer.py":
        "shared/anima_caption_format/anima_caption_format/normalizer.py",
    "backend/tagger2/workflow/caption_format/flat_txt.py":
        "shared/anima_caption_format/anima_caption_format/flat_txt.py",
    "backend/tagger2/workflow/stages/replacement.py":
        "workers/replace/src/anima_replace_worker/replacement.py",
    "backend/tagger2/workflow/raw_e621.py":
        "core/src/anima_core/raw_e621.py",
}


def _body(path: Path) -> bytes:
    """Return the file without its ported-from banner comment."""
    lines = path.read_bytes().split(b"\n")
    while lines and lines[0].startswith(b"#"):
        lines.pop(0)
    return b"\n".join(lines)


@pytest.mark.parametrize("local_path", sorted(PORTS))
def test_ported_file_carries_provenance_banner(local_path: str):
    """Every ported file names its origin so the boundary stays obvious."""
    header = Path(local_path).read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("#")
    assert "e621-standard-caption-workflow" in header


@pytest.mark.parametrize("local_path,source_relative", sorted(PORTS.items()))
def test_ported_file_matches_source_when_available(local_path: str, source_relative: str):
    """The port is byte-identical to the source file, ignoring the banner."""
    source = SOURCE_ROOT / source_relative
    if not source.is_file():
        pytest.skip("source project is not available on this machine")
    assert _body(Path(local_path)) == source.read_bytes()


def test_ported_modules_import_and_expose_their_api():
    """A port that cannot be imported is worse than a missing one."""
    from backend.tagger2.workflow.caption_format import (
        FIELDS,
        CaptionDisplayPolicy,
        normalize_json_bytes,
        serialize_flat_txt,
    )
    from backend.tagger2.workflow.raw_e621 import parse_raw_e621_annotation
    from backend.tagger2.workflow.stages.replacement import replace_projection

    assert FIELDS == (
        "quality",
        "count",
        "character",
        "series",
        "artist",
        "appearance",
        "tags",
        "environment",
        "nl",
    )
    assert callable(normalize_json_bytes)
    assert callable(serialize_flat_txt)
    assert callable(parse_raw_e621_annotation)
    assert callable(replace_projection)
    assert CaptionDisplayPolicy(True, True, False, ()).replace_underscores_with_spaces is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
