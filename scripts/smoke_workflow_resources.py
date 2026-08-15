"""Run a small real-data workflow smoke using provisioned local resources.

The command copies a deterministic sample into a temporary working directory,
so the supplied dataset is never modified.  It exercises the classification
snapshot, replacement index, tokenizer and isolated CPU OCR together.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "backend"))

# ruff: noqa: E402 - the script adds the repository backend to sys.path first.
from tagger2.workflow.classify_snapshot import load_classification_rules
from tagger2.workflow.contracts import WorkflowJobConfigV2
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.ocr import PaddleOCREngine
from tagger2.workflow.pipeline import run_offline_pipeline
from tagger2.workflow.preflight import WorkflowPreflightService
from tagger2.workflow.replacement_index import validate_replacement_index
from tagger2.workflow.resources import WorkflowResourceCatalog
from tagger2.workflow.tokenizer_resource import load_tokenizer_counter
from tagger2.security import PathAllowlist, PathRoot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="read-only dataset root")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--work-root", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 1 or not args.input.is_dir():
        print("input must be an existing directory and --limit must be positive")
        return 2
    images = sorted(
        path
        for path in args.input.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
        and path.with_suffix(".json").is_file()
    )
    if len(images) < args.limit:
        print(f"only found {len(images)} image/json pairs; need {args.limit}")
        return 2
    selected = random.Random(args.seed).sample(images, args.limit)
    work_root = (
        Path(args.work_root)
        if args.work_root is not None
        else Path(tempfile.mkdtemp(prefix="tagger2-resource-smoke-"))
    )
    input_root = work_root / "input"
    output_root = work_root / "output"
    workspace = work_root / "workspace"
    input_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(selected):
        shutil.copy2(image, input_root / f"{index}{image.suffix.casefold()}")
        shutil.copy2(image.with_suffix(".json"), input_root / f"{index}.json")

    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    resource_ids = {
        "classify": "classify-e621-20260812-v1",
        "replace": "replace-e621-pass-drop-v2",
        "tokenizer": "tokenizer-qwen3-0-6b-tokenizer-v1",
        "ocr": "ocr-paddleocr-2-9-1-cpu-v1",
    }
    paths = {key: catalog.get_resource_path(value) for key, value in resource_ids.items()}
    if any(path is None for path in paths.values()):
        print("one or more provisioned resources are unavailable")
        return 3
    assert all(path is not None for path in paths.values())
    replacement_report = validate_replacement_index(paths["replace"])
    if not replacement_report.valid:
        print(json.dumps(replacement_report.as_dict(), ensure_ascii=False))
        return 3
    config = WorkflowJobConfigV2.from_payload(
        {
            "profile": "e621",
            "work_mode": "full_copy",
            "overwrite_mode": "incremental",
            "source_root": {"root_id": "src", "relative_path": ""},
            "output_root": {"root_id": "out", "relative_path": ""},
            "recursive": False,
            "caption": {"enabled": False},
            "classify": {"enabled": True, "resource_id": resource_ids["classify"]},
        "replace": {"enabled": True, "resource_id": resource_ids["replace"]},
            "ocr": {"enabled": True, "resource_id": resource_ids["ocr"]},
            "nl": {"enabled": False},
            "count_review": {"enabled": False},
            "token_budget": {
                "enabled": True,
                "tokenizer_resource_id": resource_ids["tokenizer"],
                "max_tokens": 512,
            },
            "export": {"format": "both"},
        }
    )
    allowlist = PathAllowlist(
        [
            PathRoot("src", input_root, "smoke input", "input", writable=False),
            PathRoot("out", output_root, "smoke output", "output", writable=True),
        ]
    )
    smoke_db = WorkflowDatabase(work_root / "smoke.db")
    WorkflowPreflightService(allowlist, catalog, smoke_db).validate_config(config)
    report = run_offline_pipeline(
        config,
        source_root=input_root,
        output_root=output_root,
        workspace=workspace,
        replacement_index_path=paths["replace"],
        classification_rules=load_classification_rules(paths["classify"]),
        token_counter=load_tokenizer_counter(paths["tokenizer"]),
        ocr_engine=PaddleOCREngine(),
    )
    print(
        json.dumps(
            {
                "work_root": str(work_root),
                "selected": [str(path) for path in selected],
                "report": report.as_dict(),
                "output_files": sum(1 for path in output_root.rglob("*") if path.is_file()),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0 if not report.issues else 4


if __name__ == "__main__":
    raise SystemExit(main())
