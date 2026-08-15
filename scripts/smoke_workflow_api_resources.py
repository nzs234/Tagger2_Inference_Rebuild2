"""Exercise the FastAPI workflow path with all locally provisioned resources."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

# ruff: noqa: E402 - repository backend is added before imports.
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "backend"))

from tagger2.security import PathAllowlist, PathRoot
from tagger2.workflow.api import create_workflow_router
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.resources import WorkflowResourceCatalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--limit", type=int, default=1)
    args = parser.parse_args()
    images = sorted(
        path
        for path in args.input.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"}
        and path.with_suffix(".json").is_file()
    )[: args.limit]
    if not images:
        raise SystemExit("no image/json pairs found")
    root = Path(tempfile.mkdtemp(prefix="tagger2-api-resource-smoke-"))
    source = root / "source"
    output = root / "output"
    source.mkdir()
    output.mkdir()
    for index, image in enumerate(images):
        shutil.copy2(image, source / f"{index}{image.suffix.casefold()}")
        shutil.copy2(image.with_suffix(".json"), source / f"{index}.json")

    allowlist = PathAllowlist(
        [
            PathRoot("src", source, "smoke input", "input"),
            PathRoot("out", output, "smoke output", "output", writable=True),
        ]
    )
    database = WorkflowDatabase(root / "workflow.db")
    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    app = FastAPI()
    app.include_router(create_workflow_router(allowlist, catalog, database=database))
    config = {
        "profile": "e621",
        "work_mode": "full_copy",
        "overwrite_mode": "incremental",
        "source_root": {"root_id": "src", "relative_path": ""},
        "output_root": {"root_id": "out", "relative_path": ""},
        "caption": {"enabled": False},
        "classify": {"enabled": True, "resource_id": "classify-e621-20260812-v1"},
        "replace": {"enabled": True, "resource_id": "replace-e621-pass-drop-v2"},
        "ocr": {"enabled": True, "resource_id": "ocr-paddleocr-2-9-1-cpu-v1"},
        "nl": {"enabled": False},
        "count_review": {"enabled": False},
        "token_budget": {
            "enabled": True,
            "tokenizer_resource_id": "tokenizer-qwen3-0-6b-tokenizer-v1",
            "max_tokens": 512,
        },
        "export": {"format": "both"},
    }
    with TestClient(app) as client:
        created = client.post("/api/v1/workflows/jobs", json={"config": config})
        created.raise_for_status()
        job_id = created.json()["job_id"]
        started = client.post(f"/api/v1/workflows/jobs/{job_id}/start")
        started.raise_for_status()
        deadline = time.monotonic() + 180
        status: dict[str, object]
        while True:
            response = client.get(f"/api/v1/workflows/jobs/{job_id}")
            response.raise_for_status()
            status = response.json()
            if status["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            if time.monotonic() >= deadline:
                raise SystemExit(f"workflow did not finish: {status}")
            time.sleep(0.5)
        report = client.get(f"/api/v1/workflows/jobs/{job_id}/report").json()
    print(json.dumps({"root": str(root), "job_id": job_id, "status": status, "report": report}, ensure_ascii=False, indent=2, default=str))
    return 0 if status["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
