"""Fetch a danbooru classification snapshot through the JSON API.

Danbooru has no bulk db_export like e621; tags, aliases and implications are
pulled from the paginated JSON API (paced, resumable — same discipline as the
wiki fetcher) and written as e621 db_export-style CSVs that
``scripts/import_classification_snapshot.py`` consumes directly::

    runtime\\python.exe scripts\\fetch_danbooru_classification.py
    runtime\\python.exe scripts\\import_classification_snapshot.py \\
        --profile danbooru \\
        --tags-csv data\\tag_wiki\\danbooru\\classification\\tags.csv.gz \\
        --aliases-csv data\\tag_wiki\\danbooru\\classification\\tag_aliases.csv.gz \\
        --implications-csv data\\tag_wiki\\danbooru\\classification\\tag_implications.csv.gz \\
        --resource-id classify-danbooru-<date>-v1 \\
        --source-url https://danbooru.donmai.us/

Tags below ``--min-post-count`` (default 10) are skipped: the snapshot must
categorize tags that carry wiki pages or surface in search results, not
danbooru's long tail of one-post tags. Alias and implication walks cover
every active record.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.tag_wiki.danbooru_importer import (  # noqa: E402
    DanbooruWikiClient,
    DEFAULT_MIN_INTERVAL,
    INITIAL_CURSOR,
    default_danbooru_dir,
    load_state,
    save_state,
)
from tagger2.workflow.contracts import utc_now  # noqa: E402

TAGS_ENDPOINT = "https://danbooru.donmai.us/tags.json"
ALIASES_ENDPOINT = "https://danbooru.donmai.us/tag_aliases.json"
IMPLICATIONS_ENDPOINT = "https://danbooru.donmai.us/tag_implications.json"

# e621 db_export-style column layouts the snapshot importer expects.
TAG_FIELDS = ("id", "name", "category", "post_count")
ALIAS_FIELDS = ("id", "antecedent_name", "consequent_name", "status")
IMPLICATION_FIELDS = ("id", "antecedent_name", "consequent_name", "status")

TAGS_FILE = "tags.csv.gz"
ALIASES_FILE = "tag_aliases.csv.gz"
IMPLICATIONS_FILE = "tag_implications.csv.gz"


def _open_csv(path: Path, fields: tuple[str, ...]) -> Any:
    """Open an append-mode gzipped CSV, writing the header when created."""

    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists() or path.stat().st_size == 0
    handle: Any = gzip.open(path, "at", encoding="utf-8", newline="")
    if is_new:
        csv.writer(handle).writerow(fields)
    return handle


def _walk(
    client: DanbooruWikiClient,
    *,
    endpoint: str,
    params: dict[str, str],
    cursor: int,
    writer: Any,
    fields: tuple[str, ...],
    state: dict[str, Any],
    cursor_key: str,
    done_key: str,
    state_path: Path,
    max_requests: int | None,
    skip_row,
) -> bool:
    """Walk one index by id cursor; returns True when the walk completed."""

    completed = False
    while True:
        batch = client.fetch_page({**params, "page": f"b{cursor}"}, endpoint=endpoint)
        if not batch:
            completed = True
            break
        for row in batch:
            if not skip_row(row):
                writer.writerow([row.get(field, "") for field in fields])
        ids = [int(row["id"]) for row in batch if row.get("id") is not None]
        if not ids:
            raise SystemExit(f"{endpoint}: batch without ids; aborting to stay consistent")
        next_cursor = min(ids)
        if next_cursor >= cursor:
            raise SystemExit(f"{endpoint}: cursor did not advance; aborting")
        cursor = next_cursor
        state[cursor_key] = cursor
        state["total_requests"] = state.get("total_requests", 0) + 1
        state["last_run_at"] = utc_now()
        save_state(state_path, state)
        print(f"[fetch] requests={client.requests} cursor={cursor} batch={len(batch)}", flush=True)
        if max_requests is not None and client.requests >= max_requests:
            print("[fetch] request budget reached; rerun to continue", flush=True)
            return False
    state[done_key] = True
    state[cursor_key] = None
    save_state(state_path, state)
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--min-post-count",
        type=int,
        default=10,
        help="skip tags with fewer posts (default: %(default)s)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="stop after N successful requests (resumable)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL,
        help="minimum seconds between API requests (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")
    parser.add_argument("--proxy", default=None, help="proxy URL passed to httpx")
    parser.add_argument("--status", action="store_true", help="print the fetch state and exit")
    args = parser.parse_args()

    out_dir = default_danbooru_dir() / "classification"
    state_path = out_dir / "state.json"
    state = load_state(state_path)
    if args.status:
        print(state)
        return

    import httpx

    client = DanbooruWikiClient(
        httpx.Client(timeout=args.timeout, proxy=args.proxy, follow_redirects=True),
        min_interval=args.min_interval,
    )

    tags_path = out_dir / TAGS_FILE
    aliases_path = out_dir / ALIASES_FILE
    implications_path = out_dir / IMPLICATIONS_FILE

    def skip_inactive(row: dict[str, Any]) -> bool:
        status = str(row.get("status") or "active")
        return status != "active"

    with _open_csv(tags_path, TAG_FIELDS) as tags_out, _open_csv(
        aliases_path, ALIAS_FIELDS
    ) as aliases_out, _open_csv(implications_path, IMPLICATION_FIELDS) as implications_out:
        budget_left = True
        if not state.get("tags_done"):
            budget_left = _walk(
                client,
                endpoint=TAGS_ENDPOINT,
                params={
                    "limit": "1000",
                    "search[post_count]": f">={args.min_post_count}",
                    "only": "id,name,post_count,category",
                },
                cursor=int(state.get("tags_cursor") or INITIAL_CURSOR * 500),
                writer=csv.writer(tags_out),
                fields=TAG_FIELDS,
                state=state,
                cursor_key="tags_cursor",
                done_key="tags_done",
                state_path=state_path,
                max_requests=args.max_requests,
                skip_row=lambda row: False,
            )
        if budget_left and not state.get("aliases_done"):
            budget_left = _walk(
                client,
                endpoint=ALIASES_ENDPOINT,
                params={
                    "limit": "1000",
                    "search[status]": "active",
                    "only": "id,antecedent_name,consequent_name,status",
                },
                cursor=int(state.get("aliases_cursor") or INITIAL_CURSOR * 500),
                writer=csv.writer(aliases_out),
                fields=ALIAS_FIELDS,
                state=state,
                cursor_key="aliases_cursor",
                done_key="aliases_done",
                state_path=state_path,
                max_requests=args.max_requests,
                skip_row=skip_inactive,
            )
        if budget_left and not state.get("implications_done"):
            _walk(
                client,
                endpoint=IMPLICATIONS_ENDPOINT,
                params={
                    "limit": "1000",
                    "only": "id,antecedent_name,consequent_name,status",
                },
                cursor=int(state.get("implications_cursor") or INITIAL_CURSOR * 500),
                writer=csv.writer(implications_out),
                fields=IMPLICATION_FIELDS,
                state=state,
                cursor_key="implications_cursor",
                done_key="implications_done",
                state_path=state_path,
                max_requests=args.max_requests,
                skip_row=skip_inactive,
            )

    print("[fetch] classification CSVs ready under", out_dir)
    print(
        "next: scripts\\import_classification_snapshot.py --profile danbooru "
        f"--tags-csv {tags_path} --aliases-csv {aliases_path} "
        f"--implications-csv {implications_path} --resource-id classify-danbooru-<date>-v1 "
        "--allow-official-anomalies"
    )


if __name__ == "__main__":
    main()
