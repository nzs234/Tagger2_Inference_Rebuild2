"""Fetch and import the danbooru wiki corpus through the JSON API.

Danbooru has no bulk db_export like e621; this CLI walks the
``wiki_pages.json`` API politely (paced requests, 429-aware, resumable) into
a raw JSONL cache under ``data/tag_wiki/danbooru/`` and imports the pages
into the dedicated store ``data/tag_wiki/tag_wiki_danbooru.sqlite3``.

Safe to re-run: the first run performs a full walk, later runs only fetch
pages updated since the last run, and imports are incremental (unchanged
pages are skipped, deleted pages are purged). Embeddings and Chinese
summaries are NOT built here; the vector index is a separate step once the
danbooru profile is wired into the app.

Usage::

    .\\runtime\\python.exe scripts\\fetch_danbooru_wiki.py                    # auto: full walk, then incremental
    .\\runtime\\python.exe scripts\\fetch_danbooru_wiki.py --max-requests 40  # fetch a bounded budget per invocation
    .\\runtime\\python.exe scripts\\fetch_danbooru_wiki.py --skip-fetch       # (re-)import the cached JSONL only
    .\\runtime\\python.exe scripts\\fetch_danbooru_wiki.py --status
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.tag_wiki import danbooru_importer as di  # noqa: E402
from tagger2.tag_wiki.wiki_store import WikiStore  # noqa: E402
from tagger2.workflow.contracts import utc_now  # noqa: E402


def _print_state(store: WikiStore, state: dict[str, Any]) -> None:
    print("state:")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    print("store:")
    print(
        json.dumps(
            {
                "pages": store.page_count(),
                "chunks": store.chunk_count(),
                "embedded_chunks": store.embedded_chunk_count(),
                "source": store.get_meta("source"),
                "imported_at": store.get_meta("imported_at"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _fetch(args: argparse.Namespace, client: di.DanbooruWikiClient, state: dict[str, Any]) -> bool:
    """Run the fetch phase; return True when the phase completed."""

    jsonl_path = args.jsonl
    state_path = args.state
    run_requests_at_start = client.requests
    mode = args.mode
    if mode == "auto":
        mode = "incremental" if state.get("full_walk_done") else "full"
    budget = args.max_requests
    completed = True

    if mode == "full":
        cursor = int(state.get("full_cursor") or di.INITIAL_CURSOR)
        print(f"[fetch] full walk from cursor {cursor}", flush=True)
        for batch in di.iter_full_batches(client, page_limit=args.page_limit, initial_cursor=cursor):
            written = di.append_pages_jsonl(jsonl_path, batch)
            ids = [int(page["id"]) for page in batch if page.get("id") is not None]
            state["full_cursor"] = min(ids) if ids else state.get("full_cursor")
            state["last_run_at"] = utc_now()
            state["last_run_mode"] = "full"
            di.save_state(state_path, state)
            print(
                f"[fetch] requests={client.requests} cursor={state['full_cursor']} "
                f"batch_pages={written}",
                flush=True,
            )
            if budget is not None and client.requests >= budget:
                completed = False
                print("[fetch] request budget reached; rerun to continue the walk", flush=True)
                break
        else:
            state["full_walk_done"] = True
            state["full_cursor"] = None
            state["watermark"] = args.run_start_date
            di.save_state(state_path, state)
            print("[fetch] full walk complete", flush=True)
    else:
        since = args.since or state.get("watermark")
        if not since:
            raise SystemExit(
                "no watermark yet: run a full walk first (default mode) or pass --since YYYY-MM-DD"
            )
        print(f"[fetch] incremental since {since}", flush=True)
        for batch in di.iter_updated_batches(client, since=since, page_limit=args.page_limit):
            written = di.append_pages_jsonl(jsonl_path, batch)
            state["last_run_at"] = utc_now()
            state["last_run_mode"] = "incremental"
            di.save_state(state_path, state)
            print(f"[fetch] requests={client.requests} batch_pages={written}", flush=True)
            if budget is not None and client.requests >= budget:
                completed = False
                print("[fetch] request budget reached; rerun to continue", flush=True)
                break
        else:
            state["watermark"] = args.run_start_date
            di.save_state(state_path, state)
            print("[fetch] incremental pass complete", flush=True)

    state["total_requests"] = state.get("total_requests", 0) + client.requests - run_requests_at_start
    di.save_state(state_path, state)
    return completed


def _import(args: argparse.Namespace) -> dict:
    store = WikiStore(args.store)
    seen_at_last_print = 0

    def progress(done: int, _total: int) -> None:
        nonlocal seen_at_last_print
        if done - seen_at_last_print >= 5000:
            seen_at_last_print = done
            print(f"[import] {done} rows", flush=True)

    stats = di.import_pages(store, di.iter_pages_jsonl(args.jsonl), progress=progress)
    stats["store_pages"] = store.page_count()
    stats["store_chunks"] = store.chunk_count()
    return stats


def _main(args: argparse.Namespace) -> int:
    state = di.load_state(args.state)
    if args.status:
        _print_state(WikiStore(args.store), state)
        return 0

    if not args.skip_fetch:
        client = di.DanbooruWikiClient(_build_http_client(args), min_interval=args.min_interval)
        try:
            completed = _fetch(args, client, state)
        except di.DanbooruWikiFetchError as exc:
            print(f"[fetch] FAILED: {exc}", file=sys.stderr)
            return 1
        if not completed and not args.skip_import:
            print("[import] skipped: fetch phase did not complete", flush=True)
            return 0
    if args.skip_import:
        print("[fetch] done (import skipped)", flush=True)
        return 0
    if not args.jsonl.is_file():
        print("[import] nothing to import: no cached JSONL yet", flush=True)
        return 0

    stats = _import(args)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def _build_http_client(args: argparse.Namespace) -> httpx.Client:
    return httpx.Client(
        timeout=args.timeout,
        proxy=args.proxy,
        follow_redirects=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mode",
        choices=["auto", "full", "incremental"],
        default="auto",
        help="auto: full walk on the first run, incremental afterwards (default)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="incremental lower bound override (YYYY-MM-DD or full timestamp, UTC)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="stop the fetch phase after N successful requests (resumable)",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=di.DEFAULT_MIN_INTERVAL,
        help="minimum seconds between API requests (default: %(default)s)",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=di.PAGE_LIMIT,
        help="records per API request, capped at 1000 (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds")
    parser.add_argument("--proxy", default=None, help="proxy URL passed to httpx (env vars honored too)")
    parser.add_argument("--skip-fetch", action="store_true", help="do not query the API; import cached pages only")
    parser.add_argument("--skip-import", action="store_true", help="only fetch into the JSONL cache")
    parser.add_argument("--status", action="store_true", help="print the fetch state and store counts, then exit")
    args = parser.parse_args()

    if args.page_limit < 1:
        parser.error("--page-limit must be >= 1")
    if args.since is not None:
        try:
            di.parse_bound(args.since)
        except ValueError as exc:
            parser.error(f"--since is not a valid timestamp: {exc}")
    cache_dir = di.default_danbooru_dir()
    args.jsonl = cache_dir / "wiki_pages.jsonl"
    args.state = cache_dir / "state.json"
    args.store = di.default_danbooru_store_path()
    args.run_start_date = datetime.now(UTC).date().isoformat()
    if args.max_requests is not None and args.max_requests < 1:
        parser.error("--max-requests must be >= 1")
    try:
        raise SystemExit(_main(args))
    except KeyboardInterrupt:
        print("\n[fetch] interrupted; progress is saved — rerun to continue", flush=True)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
