from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

from tagger2.workflow.replacement_index import validate_replacement_index


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "convert_replacement_pass_to_drop.py"
_SPEC = importlib.util.spec_from_file_location("convert_replacement_pass_to_drop", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
convert = _MODULE.convert


def test_designated_conversion_only_changes_pass_action(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    destination = tmp_path / "destination.csv"
    rows = [
        {
            "source_tag": "keep_tag",
            "canonical_e621_tag": "keep_tag",
            "action": "keep",
            "replacement_tags": "keep_tag",
        },
        {
            "source_tag": "replace_tag",
            "canonical_e621_tag": "canonical_tag",
            "action": "replace",
            "replacement_tags": "canonical_tag|extra_tag",
        },
        {
            "source_tag": "pass_tag",
            "canonical_e621_tag": "pass_tag",
            "action": "pass",
            "replacement_tags": "pass_tag",
        },
    ]
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=["source_tag", "canonical_e621_tag", "action", "replacement_tags"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    counts = convert(source, destination)
    assert dict(counts) == {"keep": 1, "replace": 1, "drop": 1}

    with destination.open("r", encoding="utf-8", newline="") as stream:
        converted = list(csv.DictReader(stream))
    assert converted[0] == rows[0]
    assert converted[1] == rows[1]
    assert converted[2] == {
        "source_tag": "pass_tag",
        "canonical_e621_tag": "pass_tag",
        "action": "drop",
        "replacement_tags": "",
    }
    report = validate_replacement_index(destination)
    assert report.valid is True
    assert report.action_counts == {"keep": 1, "replace": 1, "drop": 1, "pass": 0}
    assert report.passthrough_count == 0
