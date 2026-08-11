"""Workflow resource catalog and fingerprinting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import WorkflowResourceManifestV1, utc_now, RESOURCE_ID_PATTERN


class WorkflowResourceCatalog:
    """Content-addressed resource catalog."""

    def __init__(self, resource_dir: Path):
        self.resource_dir = Path(resource_dir)
        self.resource_dir.mkdir(parents=True, exist_ok=True)

    def fingerprint_file(self, path: Path) -> str:
        """Compute SHA-256 fingerprint of a file."""
        hasher = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    def import_resource(
        self,
        source_path: Path,
        resource_id: str,
        category: str,
        source_url: str | None = None,
        source_timestamp: str | None = None,
        builder_version: str | None = None,
    ) -> WorkflowResourceManifestV1:
        """Import a resource file and register its manifest."""
        if not RESOURCE_ID_PATTERN.match(resource_id):
            raise ValueError(f"Invalid resource_id: {resource_id}")
        
        if not source_path.is_file():
            raise FileNotFoundError(f"Resource file not found: {source_path}")
        
        # Compute fingerprint
        fingerprint = self.fingerprint_file(source_path)
        
        # Copy to content-addressed location
        target_dir = self.resource_dir / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{resource_id}.{fingerprint[:16]}"
        
        if not target_path.exists():
            import shutil
            shutil.copy2(source_path, target_path)
        
        # Create manifest
        manifest = WorkflowResourceManifestV1(
            resource_id=resource_id,
            resource_fingerprint=fingerprint,
            category=category,
            created_at=utc_now(),
            source_url=source_url,
            source_timestamp=source_timestamp,
            builder_version=builder_version,
        )
        
        # Write manifest
        manifest_path = target_dir / f"{resource_id}.manifest.json"
        with manifest_path.open("w", encoding="utf-8") as f:
            json.dump(manifest.__dict__, f, indent=2, ensure_ascii=False)
        
        return manifest

    def get_manifest(self, resource_id: str) -> WorkflowResourceManifestV1 | None:
        """Get resource manifest by ID."""
        for category_dir in self.resource_dir.iterdir():
            if not category_dir.is_dir():
                continue
            manifest_path = category_dir / f"{resource_id}.manifest.json"
            if manifest_path.exists():
                with manifest_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return WorkflowResourceManifestV1(**data)
        return None

    def list_resources(self, category: str | None = None) -> list[WorkflowResourceManifestV1]:
        """List all resources, optionally filtered by category."""
        manifests: list[WorkflowResourceManifestV1] = []
        
        for category_dir in self.resource_dir.iterdir():
            if not category_dir.is_dir():
                continue
            if category and category_dir.name != category:
                continue
            
            for manifest_path in category_dir.glob("*.manifest.json"):
                with manifest_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                manifests.append(WorkflowResourceManifestV1(**data))
        
        return manifests

    def get_resource_path(self, resource_id: str) -> Path | None:
        """Get path to resource file."""
        manifest = self.get_manifest(resource_id)
        if not manifest:
            return None
        
        category_dir = self.resource_dir / manifest.category
        resource_path = category_dir / f"{resource_id}.{manifest.resource_fingerprint[:16]}"
        
        return resource_path if resource_path.exists() else None

    def validate_csv_resource(self, csv_path: Path) -> dict[str, Any]:
        """Validate a replacement index CSV before import.

        Delegates to the ported replacement-index reader so the accepted
        format is exactly the source project's ``e621-replacement-csv-v1``.
        """

        from .replacement_index import validate_replacement_index

        report = validate_replacement_index(csv_path)
        # Reuse the report's own projection so a new counter cannot be dropped here.
        summary = report.as_dict()
        summary["line_count"] = summary.pop("rule_count")
        return summary


__all__ = ["WorkflowResourceCatalog"]
