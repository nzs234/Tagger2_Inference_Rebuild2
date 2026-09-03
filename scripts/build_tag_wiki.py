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
        --min-post-count 1000 --max-pages 2000 --provider cpa

``--translate`` requires a configured online provider (same resolution as the
UI: explicit --provider, else the first enabled provider holding a key).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.main import app  # noqa: E402  (boots the real Runtime)


async def _run_build(args: argparse.Namespace) -> int:
    from tagger2.tag_wiki.contracts import BuildRequest

    service = app.state.runtime.tag_wiki
    status = await service.start_build(
        BuildRequest(
            profile=args.profile,
            download_dump=not args.no_download,
            reindex=not args.skip_reindex,
            force_reembed=args.force_reembed,
        )
    )
    print(json.dumps(status["build"], ensure_ascii=False))
    task = service._build_task
    if task is None:
        return 0
    last_phase = ""
    while not task.done():
        await asyncio.sleep(2)
        build = service.status()["build"]
        line = f"[build] {build['phase']}: {build['message']}"
        if line != last_phase:
            print(line, flush=True)
            last_phase = line
    final = service.status()["build"]
    print(json.dumps(final, ensure_ascii=False))
    if final["state"] == "error":
        print(f"[build] FAILED: {final['error']}", file=sys.stderr)
        return 1
    return 0


async def _run_translate(args: argparse.Namespace) -> int:
    from tagger2.tag_wiki.contracts import TranslateRequest

    service = app.state.runtime.tag_wiki
    progress = await service.start_translate(
        TranslateRequest(
            profile=args.profile,
            scope=args.scope,
            min_post_count=args.min_post_count,
            max_pages=args.max_pages,
            provider_id=args.provider,
            model=args.model,
        )
    )
    print(json.dumps(progress, ensure_ascii=False))
    task = service._translate_task
    if task is None:
        print("[translate] nothing to do:", progress.get("message", ""))
        return 0
    last_line = ""
    while not task.done():
        await asyncio.sleep(5)
        state = service.translate_progress()
        line = f"[translate] {state['done']}/{state['total']} done, {state['failed']} failed"
        if line != last_line:
            print(line, flush=True)
            last_line = line
    final = service.translate_progress()
    print(json.dumps(final, ensure_ascii=False))
    if final["state"] == "error":
        print(f"[translate] FAILED: {final['error']}", file=sys.stderr)
        return 1
    return 0


async def _main(args: argparse.Namespace) -> int:
    exit_code = 0
    if args.status:
        print(json.dumps(app.state.runtime.tag_wiki.status(), ensure_ascii=False, indent=2))
    if args.build:
        exit_code = await _run_build(args)
        if exit_code:
            return exit_code
    if args.translate:
        exit_code = await _run_translate(args)
    return exit_code


def main() -> None:
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
    parser.add_argument("--skip-reindex", action="store_true", help="skip dump parsing (embedding model download + vector pass still run)")
    parser.add_argument("--force-reembed", action="store_true", help="re-embed every chunk even when unchanged")
    parser.add_argument("--translate", action="store_true", help="pre-translate wiki pages into structured Chinese summaries")
    parser.add_argument("--scope", choices=["model_vocab", "popular", "all"], default="model_vocab")
    parser.add_argument("--min-post-count", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=2000)
    parser.add_argument("--provider", default=None, help="explicit provider id (default: first enabled with a key)")
    parser.add_argument("--model", default=None, help="override the provider's primary model")
    args = parser.parse_args()
    if not (args.status or args.build or args.translate):
        parser.error("nothing to do: pass --status, --build and/or --translate")
    try:
        raise SystemExit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        # Cancel the in-process build/translate task instead of leaving it
        # running in a dead interpreter's event loop.
        asyncio.run(_cancel_background_tasks())
        raise SystemExit(130) from None


async def _cancel_background_tasks() -> None:
    from tagger2.tag_wiki.service import TagWikiService

    service: TagWikiService = app.state.runtime.tag_wiki
    await service.aclose()


if __name__ == "__main__":
    main()
