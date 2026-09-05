"""Full re-embed of both tag wiki stores through the configured backend.

Maintenance tool for model swaps (e.g. rebuilding the vectors with an
LM Studio-hosted model): skips the dump pipeline entirely — chunks are
unchanged, only their embeddings are stale. Mirrors the embed loop of
``TagWikiService._embed_pending_sync`` (same passage text assembly, same
batching) with progress output, and reports the final dimension per store.

Usage (from the project root)::

    python scripts/reembed_tag_wiki.py            # e621 + danbooru
    python scripts/reembed_tag_wiki.py e621       # one profile only

Reads [tag_wiki] from config/app.toml; the backend must be reachable
(lm Studio running with the embedding model loaded) before starting.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.config import get_settings  # noqa: E402
from tagger2.tag_wiki.service import TagWikiService  # noqa: E402
from tagger2.tag_wiki.wiki_store import WikiStore  # noqa: E402

BATCH_SIZE = 256
PROGRESS_EVERY_BATCHES = 10


def reembed(service: TagWikiService, profile: str) -> None:
    store: WikiStore = service._store_for(profile)
    meta = store.page_meta()
    total = int(meta["chunks"])
    if total == 0:
        print(f"[{profile}] empty store, nothing to do", flush=True)
        return

    embedder, error = service._get_embedder()
    if embedder is None:
        raise SystemExit(f"[{profile}] embedder unavailable: {error}")
    print(
        f"[{profile}] {total} chunks | dim {embedder.dimension} | "
        f"passage prefix applied by embedder",
        flush=True,
    )

    store.clear_embeddings()
    started = time.time()
    processed = 0
    batches = 0
    while True:
        pending = store.pending_embedding_chunks(BATCH_SIZE)
        if not pending:
            break
        # Same text assembly as TagWikiService._embed_pending_sync: the tag
        # name anchors retrieval, prefixes are the embedder's business.
        texts = [
            "\n".join(
                part for part in (chunk["page_title"], chunk["heading"], chunk["text"]) if part
            )
            for chunk in pending
        ]
        vectors = embedder.embed_passages(texts)
        store.mark_embedded([int(chunk["id"]) for chunk in pending], vectors)
        processed += len(pending)
        batches += 1
        if batches % PROGRESS_EVERY_BATCHES == 0:
            elapsed = max(time.time() - started, 1e-6)
            rate = processed / elapsed
            eta_min = (total - processed) / max(rate, 1e-6) / 60
            print(
                f"[{profile}] {processed}/{total} ({processed / total:5.1%}) "
                f"{rate:6.0f} chunks/s | ETA {eta_min:4.0f} min",
                flush=True,
            )

    minutes = (time.time() - started) / 60
    print(f"[{profile}] done: {processed} chunks in {minutes:.1f} min", flush=True)


def main() -> None:
    profiles = sys.argv[1:] or ["e621", "danbooru"]
    settings = get_settings()
    if settings.tag_wiki_embed_backend not in {"local", "openai"}:
        raise SystemExit(f"unsupported tag_wiki embed_backend: {settings.tag_wiki_embed_backend}")
    service = TagWikiService(
        data_dir=settings.data_dir,
        embed_repo=settings.tag_wiki_embed_repo,
        embed_backend=settings.tag_wiki_embed_backend,
        embed_endpoint=settings.tag_wiki_embed_endpoint,
        embed_api_key=settings.tag_wiki_embed_api_key,
        embed_model=settings.tag_wiki_embed_model,
        embed_passage_prefix=settings.tag_wiki_embed_passage_prefix,
        embed_query_prefix=settings.tag_wiki_embed_query_prefix,
    )
    print(
        f"backend={settings.tag_wiki_embed_backend} "
        f"model={settings.tag_wiki_embed_model or settings.tag_wiki_embed_repo}",
        flush=True,
    )
    for profile in profiles:
        reembed(service, profile)


if __name__ == "__main__":
    main()
