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
    "backend/tagger2/workflow/stages/nl_validation.py":
        "workers/nl/src/anima_nl_worker/validation.py",
    "backend/tagger2/workflow/stages/count_rules.py":
        "workers/classify/src/anima_classify_worker/count.py",
    "backend/tagger2/workflow/stages/policy.py":
        "workers/policy/src/anima_policy_worker/policy.py",
}

# token_budget.py is ported with one deliberate change: the shared caption-format
# import path differs because that module lives inside this package.
ADAPTED_PORTS = {
    "backend/tagger2/workflow/stages/token_budget.py":
        "workers/token_budget/src/anima_token_budget_worker/budget.py",
}


def _body(path: Path) -> bytes:
    """Return the file without its ported-from banner comment.

    Line endings are normalized: git and the editors here rewrite CRLF, which is
    not a behavioural difference, so comparing on LF keeps the guard meaningful.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    lines = data.split(b"\n")
    while lines and lines[0].startswith(b"#"):
        lines.pop(0)
    return b"\n".join(lines)


def _source_body(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n")


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
    assert _body(Path(local_path)) == _source_body(source)


@pytest.mark.parametrize("local_path,source_relative", sorted(ADAPTED_PORTS.items()))
def test_adapted_port_differs_only_by_import_path(local_path: str, source_relative: str):
    """An adapted port must differ from its source only in its import lines."""
    source = SOURCE_ROOT / source_relative
    if not source.is_file():
        pytest.skip("source project is not available on this machine")

    local_lines = _body(Path(local_path)).decode("utf-8").splitlines()
    source_lines = _source_body(source).decode("utf-8").splitlines()
    assert len(local_lines) == len(source_lines)

    differing = [
        (ours, theirs)
        for ours, theirs in zip(local_lines, source_lines)
        if ours != theirs
    ]
    assert differing, "adapted port is identical; move it to PORTS instead"
    for ours, theirs in differing:
        assert ours.lstrip().startswith("from "), ours
        assert theirs.lstrip().startswith("from "), theirs


@pytest.mark.parametrize("local_path", sorted(ADAPTED_PORTS))
def test_adapted_port_carries_provenance_banner(local_path: str):
    header = Path(local_path).read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("#")
    assert "e621-standard-caption-workflow" in header


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

    from backend.tagger2.workflow.stages.count_rules import decide_count
    from backend.tagger2.workflow.stages.nl_validation import validate_nl
    from backend.tagger2.workflow.stages.policy import apply_policy
    from backend.tagger2.workflow.stages.token_budget import fit

    assert callable(decide_count)
    assert callable(validate_nl)
    assert callable(apply_policy)
    assert callable(fit)
    assert CaptionDisplayPolicy(True, True, False, ()).replace_underscores_with_spaces is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
