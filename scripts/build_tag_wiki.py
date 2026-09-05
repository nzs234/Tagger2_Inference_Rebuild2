"""Build, update and pre-translate the local tag wiki from the CLI.

Runs the exact same pipeline as the Tag Wiki UI buttons (download dump ->
import -> embedding model -> vector index, optionally followed by the Chinese
summary translation job), so server operators can bootstrap or refresh the
feature without opening the browser. Safe to re-run: imports are incremental,
only un-embedded chunks are vectorized and already-translated pages are
skipped.

Usage::

    .\\runtime\\python.exe scripts/build_tag_wiki.py --build
    .\\runtime\\python.exe scripts/build_tag_wiki.py --build --force-reembed
    .\\runtime\\python.exe scripts\\build_tag_wiki.py --translate --scope popular \\
        --min-post-count 1000 --max-pages 2000 --provider cpa --concurrency 8

Flag semantics (offline control):

- ``--no-download`` reuses the newest cached dump under
  ``data/tag_wiki/downloads/`` and skips the e621 dump refresh check.
- ``--skip-reindex`` skips re-importing the dump into SQLite. The dump
  refresh check, pruning, the embedding model check and the vector pass
  still run, so pair it with ``--no-download`` for a fully offline index
  refresh.
- ``--force-reembed`` re-embeds every chunk even when its content hash is
  unchanged (full vector pass).

``--translate`` requires a configured online provider (same resolution as the
UI: explicit --provider, else the first enabled provider holding a key).

Exit codes: 0 success (including "nothing to do"), 1 the started job
finished in an error state, 2 the job could not be started (one is already
running or setup is incomplete), 130 interrupted (Ctrl-C).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.main import app  # noqa: E402  (boots the real Runtime)

BUILD_POLL_INTERVAL = 2.0
TRANSLATE_POLL_INTERVAL = 5.0


def _tag_wiki_service() -> Any:
    """The app's TagWikiService; only its public API is used here."""
    return app.state.runtime.tag_wiki


async def _follow(
    wait: Awaitable[dict[str, Any]],
    snapshot: Callable[[], dict[str, Any]],
    *,
    interval: float,
    render: Callable[[dict[str, Any]], str],
) -> dict[str, Any]:
    """Await a service wait coroutine while printing rendered progress lines."""
    waiter = asyncio.ensure_future(wait)
    last_line = ""
    while True:
        line = render(snapshot())
        if line != last_line:
            print(line, flush=True)
            last_line = line
        if waiter.done():
            break
        await asyncio.sleep(interval)
    return await waiter


async def _run_build(service: Any, args: argparse.Namespace) -> int:
    from tagger2.tag_wiki.contracts import BuildRequest
    from tagger2.tag_wiki.service import TagWikiError

    try:
        status = await service.start_build(
            BuildRequest(
                profile=args.profile,
                download_dump=not args.no_download,
                reindex=not args.skip_reindex,
                force_reembed=args.force_reembed,
            )
        )
    except TagWikiError as exc:
        # Busy (409) or setup incomplete (no data / no provider / no model...).
        print(f"[build] cannot start: {exc.message}", file=sys.stderr)
        return 2
    print(json.dumps(status["build"], ensure_ascii=False))
    final = await _follow(
        service.wait_build(),
        snapshot=lambda: service.status()["build"],
        interval=BUILD_POLL_INTERVAL,
        render=lambda build: f"[build] {build['phase']}: {build['message']}",
    )
    print(json.dumps(final, ensure_ascii=False))
    if final["state"] == "error":
        print(f"[build] FAILED: {final['error']}", file=sys.stderr)
        return 1
    return 0


async def _run_translate(service: Any, args: argparse.Namespace) -> int:
    from tagger2.tag_wiki.contracts import TranslateRequest
    from tagger2.tag_wiki.service import TagWikiError

    try:
        progress = await service.start_translate(
            TranslateRequest(
                profile=args.profile,
                scope=args.scope,
                min_post_count=args.min_post_count,
                max_pages=args.max_pages,
                concurrency=args.concurrency,
                provider_id=args.provider,
                model=args.model,
            )
        )
    except TagWikiError as exc:
        print(f"[translate] cannot start: {exc.message}", file=sys.stderr)
        return 2
    print(json.dumps(progress, ensure_ascii=False))
    if service.translate_task() is None:
        print("[translate] nothing to do:", progress.get("message", ""))
        return 0
    final = await _follow(
        service.wait_translate(),
        snapshot=service.translate_progress,
        interval=TRANSLATE_POLL_INTERVAL,
        render=lambda state: f"[translate] {state['done']}/{state['total']} done, {state['failed']} failed",
    )
    print(json.dumps(final, ensure_ascii=False))
    if final["state"] == "error":
        print(f"[translate] FAILED: {final['error']}", file=sys.stderr)
        return 1
    return 0


async def _main(args: argparse.Namespace) -> int:
    service = _tag_wiki_service()
    try:
        exit_code = 0
        if args.status:
            print(json.dumps(service.status(), ensure_ascii=False, indent=2))
        if args.build:
            exit_code = await _run_build(service, args)
            if exit_code:
                return exit_code
        if args.translate:
            exit_code = await _run_translate(service, args)
        return exit_code
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C (asyncio.Runner turns SIGINT into a cancellation of this
        # task): cancel the in-process background jobs on this still-open
        # event loop instead of leaving them attached to a dead interpreter.
        await service.aclose()
        return 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="print the wiki status document and exit")
    parser.add_argument("--build", action="store_true", help="download/import the wiki dump and build the vector index")
    parser.add_argument(
        "--profile",
        choices=["e621", "danbooru"],
        default="e621",
        help="which wiki mirror to build/translate (danbooru corpus ships pre-imported)",
    )
    parser.add_argument("--no-download", action="store_true", help="reuse the newest cached dump instead of re-checking e621")
    parser.add_argument(
        "--skip-reindex",
        action="store_true",
        help=(
            "skip re-importing the dump (the dump refresh check, pruning, model "
            "check and the vector pass still run; add --no-download to avoid the network)"
        ),
    )
    parser.add_argument("--force-reembed", action="store_true", help="re-embed every chunk even when unchanged")
    parser.add_argument("--translate", action="store_true", help="pre-translate wiki pages into structured Chinese summaries")
    parser.add_argument("--scope", choices=["model_vocab", "popular", "all"], default="model_vocab")
    parser.add_argument("--min-post-count", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=2000)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="how many pages to summarize in parallel (1 = sequential)",
    )
    parser.add_argument("--provider", default=None, help="explicit provider id (default: first enabled with a key)")
    parser.add_argument("--model", default=None, help="override the provider's primary model")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not (args.status or args.build or args.translate):
        parser.error("nothing to do: pass --status, --build and/or --translate")
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        # Second Ctrl-C (or SIGINT outside an await): asyncio.run has already
        # cancelled everything on its way out.
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
