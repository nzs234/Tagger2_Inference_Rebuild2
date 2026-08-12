"""Probe the isolated PaddleOCR runtime without downloading or mutating models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "backend"))

from tagger2.workflow.ocr import write_ocr_runtime_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime-python",
        type=Path,
        default=None,
        help="Explicit isolated interpreter (defaults to project runtime_ocr)",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="PaddleOCR model cache root (defaults to PADDLEOCR_HOME/~/.paddleocr)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON manifest destination; stdout is always emitted",
    )
    parser.add_argument(
        "--resource-id",
        default=None,
        help="Register the descriptor in the workflow catalog under this immutable ID",
    )
    parser.add_argument(
        "--resource-dir",
        type=Path,
        default=project_root / "data" / "workflows" / "resources",
        help="Workflow resource catalog directory used with --resource-id",
    )
    args = parser.parse_args()
    output_path = args.output
    if output_path is None and args.resource_id:
        output_path = project_root / "runtime_ocr" / "ocr-runtime.manifest.json"
    if output_path is not None:
        manifest = write_ocr_runtime_manifest(
            output_path,
            runtime_python=args.runtime_python,
            model_dir=args.model_dir,
        )
    else:
        from tagger2.workflow.ocr import build_ocr_runtime_manifest

        manifest = build_ocr_runtime_manifest(args.runtime_python, args.model_dir)

    if args.resource_id:
        from tagger2.workflow.resources import WorkflowResourceCatalog

        if output_path is None:  # pragma: no cover - resource-id sets output_path
            raise RuntimeError("resource registration requires a descriptor path")
        catalog = WorkflowResourceCatalog(args.resource_dir)
        catalog.import_resource(
            source_path=output_path,
            resource_id=args.resource_id,
            category="ocr",
            builder_version="paddleocr-runtime-v1",
        )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
