"""Probe and register the local CPU PaddleOCR runtime descriptor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "backend"))

# ruff: noqa: E402 - repository backend is added before imports.
from tagger2.workflow.ocr import build_ocr_runtime_manifest
from tagger2.workflow.resources import WorkflowResourceCatalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-python", type=Path, default=None)
    parser.add_argument("--model-dir", type=Path, default=None)
    parser.add_argument("--resource-id", default="ocr-paddleocr-2-9-1-cpu-v1")
    args = parser.parse_args()
    try:
        manifest = build_ocr_runtime_manifest(args.runtime_python, args.model_dir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1
    # Keep the descriptor beside the ignored isolated runtime; the catalog
    # stores its immutable copy and the user model cache is never modified.
    descriptor = project_root / "runtime_ocr" / "ocr-runtime.manifest.json"
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    import json

    descriptor.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    registered = catalog.import_resource(
        descriptor,
        args.resource_id,
        "ocr",
        builder_version="paddleocr-runtime-v1",
    )
    print(f"Registered {registered.resource_id}: {registered.resource_fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
