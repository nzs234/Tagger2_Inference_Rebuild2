"""Validate and register a local serialized tokenizer resource."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "backend"))

# ruff: noqa: E402 - repository backend is added before imports.
from tagger2.workflow.resources import WorkflowResourceCatalog
from tagger2.workflow.tokenizer_resource import validate_tokenizer_resource


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tokenizer_json", type=Path)
    parser.add_argument("--resource-id", default="tokenizer-qwen3-0-6b-tokenizer-v1")
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--source-timestamp", default=None)
    args = parser.parse_args()
    source = args.tokenizer_json.expanduser().resolve()
    report = validate_tokenizer_resource(source)
    if not report["valid"]:
        print("ERROR: tokenizer validation failed")
        for error in report["errors"]:
            print(f"  {error}")
        return 1
    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    manifest = catalog.import_resource(
        source,
        args.resource_id,
        "tokenizer",
        source_url=args.source_url,
        source_timestamp=args.source_timestamp,
        builder_version="tokenizers-resource-v1",
    )
    print(f"Registered {manifest.resource_id}: {manifest.resource_fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
