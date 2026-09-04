"""Translate an explicit list of wiki pages into Chinese summaries.

Companion to ``build_tag_wiki.py --translate`` for parallel runs: the scope
resolver always works from the most popular pages down, so two processes
launched on the same scope would duplicate each other's work. This script
takes a pre-split, disjoint title list (one title per line) instead and runs
the exact same :func:`translate_pages` pipeline over it. Pages that already
carry a summary are skipped at startup, so re-running a chunk file is safe.

Usage::

    .\\runtime\\python.exe scripts/translate_tag_wiki_titles.py \\
        --titles-file data/tag_wiki/e621_chunk1.txt --profile e621 \\
        --provider cpa --concurrency 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.main import app  # noqa: E402  (boots the real Runtime)


async def _run(args: argparse.Namespace) -> int:
    from tagger2.tag_wiki.translator import translate_pages

    service = app.state.runtime.tag_wiki
    store = service._store_for(args.profile)
    titles = [
        line.strip()
        for line in Path(args.titles_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    pending = [title for title in titles if store.get_summary(title) is None]
    skipped = len(titles) - len(pending)
    print(
        json.dumps(
            {"titles_file": args.titles_file, "total": len(titles), "already_done": skipped, "pending": len(pending)},
            ensure_ascii=False,
        ),
        flush=True,
    )
    if not pending:
        print("[translate] nothing to do: chunk fully translated")
        return 0

    provider_id, provider = service._resolve_provider(args.provider)
    started = time.monotonic()

    def on_progress(done: int, failed: int) -> None:
        rate = done / max(time.monotonic() - started, 1e-6) * 60
        print(
            f"[translate] {done}/{len(pending)} done, {failed} failed ({rate:.0f}/min)",
            flush=True,
        )

    result = await translate_pages(
        store,
        provider,
        pending,
        model=args.model,
        provider_id=provider_id,
        on_progress=on_progress,
        concurrency=args.concurrency,
    )
    print(json.dumps(result, ensure_ascii=False))
    if result["failed"] and not result["done"]:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--titles-file", required=True, help="text file with one wiki page title per line")
    parser.add_argument("--profile", choices=["e621", "danbooru"], default="e621")
    parser.add_argument("--provider", default=None, help="explicit provider id (default: first enabled with a key)")
    parser.add_argument("--model", default=None, help="override the provider's primary model")
    parser.add_argument("--concurrency", type=int, default=8, help="parallel summary calls (1 = sequential)")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        # Progress persists per page; an interrupted chunk is simply re-run.
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
