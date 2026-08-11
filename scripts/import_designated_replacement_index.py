"""Import the designated e621 replacement index into the workflow resource catalog."""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from backend.tagger2.workflow.resources import WorkflowResourceCatalog
from backend.tagger2.workflow.replacement_index import validate_replacement_index


DESIGNATED_CSV_PATH = Path(r"D:\QQ相关\下载\E621tag替换索引\e621_general_tag_replacement_index.csv")
RESOURCE_ID = "e621-replacement-index-v1"
CATEGORY = "replacement_index"


def main():
    if not DESIGNATED_CSV_PATH.exists():
        print(f"ERROR: Designated CSV not found: {DESIGNATED_CSV_PATH}")
        return 1
    
    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    
    # Check if already imported
    existing = catalog.get_manifest(RESOURCE_ID)
    if existing:
        print(f"Resource already imported: {RESOURCE_ID}")
        print(f"  Fingerprint: {existing.resource_fingerprint}")
        print(f"  Created: {existing.created_at}")
        return 0
    
    # Validate before import
    print(f"Validating {DESIGNATED_CSV_PATH}...")
    report = validate_replacement_index(DESIGNATED_CSV_PATH)
    
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
    print(f"\nImporting to catalog as '{RESOURCE_ID}'...")
    manifest = catalog.import_resource(
        source_path=DESIGNATED_CSV_PATH,
        resource_id=RESOURCE_ID,
        category=CATEGORY,
    )
    
    print("Import complete:")
    print(f"  Resource ID: {manifest.resource_id}")
    print(f"  Fingerprint: {manifest.resource_fingerprint}")
    print(f"  Category: {manifest.category}")
    print(f"  Created: {manifest.created_at}")
    
    # Verify we can load it
    resource_path = catalog.get_resource_path(RESOURCE_ID)
    if not resource_path:
        print("ERROR: Resource imported but path not found")
        return 1
    
    print(f"  Path: {resource_path}")
    print("\nResource successfully imported and ready for use.")
    return 0


if __name__ == "__main__":
    exit(main())
