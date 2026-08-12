"""Import the designated e621 replacement index into the workflow resource catalog."""

import argparse
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# ruff: noqa: E402 - repository backend is added before imports.
from backend.tagger2.workflow.resources import WorkflowResourceCatalog
from backend.tagger2.workflow.replacement_index import validate_replacement_index


DEFAULT_CSV_PATH = Path(r"D:\QQ相关\下载\E621tag替换索引\e621_general_tag_replacement_index.csv")
# Keep the historical ``replace-`` prefix used by the V2 default contract.
# The catalog may also contain the canonical alias ``e621-replacement-index-v1``
# for older jobs, but new installs should provision this ID.
RESOURCE_ID = "replace-e621-index-v1"
CATEGORY = "replacement_index"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path, nargs="?", default=DEFAULT_CSV_PATH)
    parser.add_argument("--resource-id", default=RESOURCE_ID)
    args = parser.parse_args()
    source_path = args.csv_path.expanduser().resolve()
    if not source_path.exists():
        print(f"ERROR: Designated CSV not found: {source_path}")
        return 1
    
    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    
    # Check if already imported
    existing = catalog.get_manifest(args.resource_id)
    if existing:
        print(f"Resource already imported: {args.resource_id}")
        print(f"  Fingerprint: {existing.resource_fingerprint}")
        print(f"  Created: {existing.created_at}")
        return 0
    
    # Validate before import
    print(f"Validating {source_path}...")
    report = validate_replacement_index(source_path)
    
    if not report.valid:
        print("Validation FAILED:")
        for error in report.errors[:10]:
            print(f"  {error}")
        if report.truncated:
            print("  ... (errors truncated)")
        return 1
    
    print("Validation passed:")
    print(f"  Rule count: {report.rule_count}")
    print(f"  Keep: {report.action_counts['keep']}")
    print(f"  Replace: {report.action_counts['replace']}")
    print(f"  Drop: {report.action_counts['drop']}")
    print(f"  Pass: {report.action_counts['pass']}")
    print(f"  Pipe replacements: {report.pipe_replacement_count}")
    
    # Import
    print(f"\nImporting to catalog as '{args.resource_id}'...")
    manifest = catalog.import_resource(
        source_path=source_path,
        resource_id=args.resource_id,
        category=CATEGORY,
        source_url="local-designated-index",
        builder_version="replacement-index-v1",
    )
    
    print("Import complete:")
    print(f"  Resource ID: {manifest.resource_id}")
    print(f"  Fingerprint: {manifest.resource_fingerprint}")
    print(f"  Category: {manifest.category}")
    print(f"  Created: {manifest.created_at}")
    
    # Verify we can load it
    resource_path = catalog.get_resource_path(args.resource_id)
    if not resource_path:
        print("ERROR: Resource imported but path not found")
        return 1
    
    print(f"  Path: {resource_path}")
    print("\nResource successfully imported and ready for use.")
    return 0


if __name__ == "__main__":
    exit(main())
