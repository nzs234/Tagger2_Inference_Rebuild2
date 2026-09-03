"""Pre-fetch the model-class workflow resources that the package omits.

The portable package ships only manifests for model-class resources
(classification snapshots, tokenizer packs); the app downloads them on first
use. This CLI pulls them ahead of time so the first launch works offline, or
so machines on metered connections can fetch during off-hours.

Usage::

    runtime\\python.exe scripts/fetch_workflow_resources.py            # all
    runtime\\python.exe scripts/fetch_workflow_resources.py --resource classify-e621-20260812-v1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

FETCH_CATEGORIES = ("classify", "tokenizer")


def main() -> None:
    from tagger2.config import get_settings
    from tagger2.workflow.resource_fetch import manager_for
    from tagger2.workflow.resources import WorkflowResourceCatalog

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--resource", action="append", default=[], help="resource id to fetch (repeatable; default: all)")
    args = parser.parse_args()

    data_dir = get_settings().data_dir or project_root / "data"
    catalog = WorkflowResourceCatalog(data_dir / "workflows" / "resources")
    manager = manager_for(catalog)

    if args.resource:
        resource_ids = list(dict.fromkeys(args.resource))
    else:
        resource_ids = [
            manifest.resource_id
            for category in FETCH_CATEGORIES
            for manifest in catalog.list_resources(category)
        ]
    if not resource_ids:
        print("[fetch] nothing to do: no manifest found under", catalog.resource_dir)
        return

    exit_code = 0
    for resource_id in resource_ids:
        state = manager.get_or_start(resource_id)
        while not state.done.wait(timeout=2.0):
            print(f"[fetch] {resource_id}: {state.progress_text()}", flush=True)
        if state.state == "ready":
            print(f"[fetch] {resource_id}: 完成 -> {state.path}")
            continue
        print(f"[fetch] {resource_id}: 失败：{state.error}", file=sys.stderr)
        exit_code = 1
    # Keep the process alive long enough for daemon fetch threads to finish
    # when several run concurrently.
    time.sleep(0.1)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
