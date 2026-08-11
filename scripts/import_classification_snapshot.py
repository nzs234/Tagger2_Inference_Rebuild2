"""Build and register a classification snapshot from official DB exports.

The Classify stage needs the official tag table plus the alias table. e621 and
Danbooru publish those as CSV exports; this script converts them into the
``classify-snapshot-v1`` bundle the stage reads, validates it, and registers it
in the workflow resource catalog with its SHA-256 fingerprint.

Nothing is repaired. A malformed row aborts with its line number so the source
export can be fixed rather than silently importing partial data.

Usage::

    .\\runtime\\python.exe scripts/import_classification_snapshot.py \\
        --profile e621 \\
        --tags-csv D:\\snapshots\\e621\\tags.csv \\
        --aliases-csv D:\\snapshots\\e621\\tag_aliases.csv \\
        --implications-csv D:\\snapshots\\e621\\tag_implications.csv \\
        --resource-id classify-e621-20260811-v1

Download the exports first (they are not bundled):
  e621:    https://e621.net/db_export/
  Danbooru: https://danbooru.donmai.us/ (see their export documentation)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.workflow.classify_snapshot import (
    ClassifySnapshotError,
    build_snapshot_from_official_csv,
    validate_classify_snapshot,
)
from tagger2.workflow.resources import WorkflowResourceCatalog

CATEGORY = "classify"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=["e621", "danbooru"])
    parser.add_argument("--tags-csv", required=True, type=Path)
    parser.add_argument("--aliases-csv", required=True, type=Path)
    parser.add_argument("--implications-csv", type=Path, default=None)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--source-timestamp", default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also keep the generated snapshot at this path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build and validate without registering the resource",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    for label, path in (("tags", args.tags_csv), ("aliases", args.aliases_csv)):
        if not path.is_file():
            print(f"ERROR: {label} CSV not found: {path}")
            return 1
    if args.implications_csv is not None and not args.implications_csv.is_file():
        print(f"ERROR: implications CSV not found: {args.implications_csv}")
        return 1

    print(f"Building {args.profile} snapshot...")
    try:
        document = build_snapshot_from_official_csv(
            profile=args.profile,
            tags_csv=args.tags_csv,
            aliases_csv=args.aliases_csv,
            implications_csv=args.implications_csv,
            source_url=args.source_url,
            source_timestamp=args.source_timestamp,
        )
    except ClassifySnapshotError as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"  tags:         {len(document['tags']):,}")
    print(f"  aliases:      {len(document['aliases']):,}")
    print(f"  implications: {len(document['implications']):,}")

    with tempfile.TemporaryDirectory() as tmpdir:
        staged = Path(tmpdir) / "snapshot.json"
        staged.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )

        print("Validating...")
        report = validate_classify_snapshot(staged)
        if not report.valid:
            print("ERROR: snapshot did not validate:")
            for error in report.errors[:20]:
                print(f"  {error}")
            if report.truncated:
                print("  ... error list truncated")
            return 1

        print(f"  valid, {report.tag_count:,} tags across {len(report.category_counts)} categories")
        for category, count in sorted(report.category_counts.items()):
            print(f"    {category:<12} {count:,}")

        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(staged.read_bytes())
            print(f"  snapshot written to {args.out}")

        if args.dry_run:
            print("Dry run: resource not registered.")
            return 0

        catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
        existing = catalog.get_manifest(args.resource_id)
        if existing is not None:
            print(f"Resource already registered: {args.resource_id}")
            print(f"  fingerprint: {existing.resource_fingerprint}")
            return 0

        manifest = catalog.import_resource(
            source_path=staged,
            resource_id=args.resource_id,
            category=CATEGORY,
            source_url=args.source_url,
            source_timestamp=args.source_timestamp,
        )

    print("Registered:")
    print(f"  resource_id:  {manifest.resource_id}")
    print(f"  category:     {manifest.category}")
    print(f"  fingerprint:  {manifest.resource_fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
