"""Translate the local tag wiki into Chinese summaries with a LOCAL LLM.

The online path (``--translate`` on build_tag_wiki.py / the UI button) needs a
configured text provider. This script instead runs Qwen3-4B-Instruct locally
on the machine's GPU through the transformers stack the app already ships,
and writes summaries through the exact same prompt, parse and persist helpers
the online job uses — so the result is identical, just without network cost,
and it can be overwritten later by re-running the online job.

Usage::

    runtime\\python.exe scripts\\translate_tag_wiki_local.py --limit 2000
    runtime\\python.exe scripts\\translate_tag_wiki_local.py --limit 50 --batch-size 8   # smoke test

Safe to re-run: pages that already carry a summary are skipped, and pages
whose wiki body holds no summarizable text are counted as skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.main import app  # noqa: E402  (boots the real Runtime)

from tagger2.tag_wiki.contracts import TranslateRequest  # noqa: E402
from tagger2.tag_wiki.translator import (  # noqa: E402
    SUMMARY_SYSTEM_PROMPT,
    _clean_field,
    _clean_tags,
    extract_summary_json,
    page_context_text,
)

DEFAULT_LOCAL_MODEL = "data/tag_wiki/models/local/Qwen3-4B-Instruct-2507"
MAX_NEW_TOKENS = 460


def _load_model(model_dir: Path):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    tokenizer.padding_side = "left"
    model = AutoModelForCausalLM.from_pretrained(
        str(model_dir),
        dtype=torch.bfloat16,
        device_map="cuda" if torch.cuda.is_available() else "cpu",
    )
    model.eval()
    return tokenizer, model


def _translate_batch(
    tokenizer: Any,
    model: Any,
    items: list[tuple[str, str, str]],  # (title, tag, text)
    batch_size: int,
) -> tuple[int, int, int]:
    """Translate one scope slice in GPU batches; returns (done, failed, skipped)."""

    import torch

    done = failed = skipped = 0
    store = app.state.runtime.tag_wiki.store
    started = time.perf_counter()
    translated = 0
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        prompts: list[str] = []
        for _title, tag, text in batch:
            user = json.dumps({"tag": tag, "title": tag, "text": text}, ensure_ascii=False)
            prompts.append(
                tokenizer.apply_chat_template(
                    [
                        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    tokenize=False,
                    add_generation_prompt=True,
                )
            )
        encoded = tokenizer(
            prompts, return_tensors="pt", padding=True, truncation=True, max_length=4096
        ).to(model.device)
        input_len = encoded["input_ids"].shape[1]
        with torch.inference_mode():
            output = model.generate(
                **encoded,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,
                repetition_penalty=1.05,
            )
        replies = tokenizer.batch_decode(output[:, input_len:], skip_special_tokens=True)
        for (title, _tag, _text), reply in zip(batch, replies):
            data = extract_summary_json(reply)
            meaning = _clean_field(data.get("meaning")) if data else ""
            usage = _clean_field(data.get("usage")) if data else ""
            pairing = _clean_field(data.get("pairing")) if data else ""
            notes = _clean_field(data.get("notes")) if data else ""
            if not any((meaning, usage, pairing, notes)):
                failed += 1
                continue
            store.upsert_summary(
                title,
                {
                    "meaning": meaning,
                    "usage": usage,
                    "pairing": pairing,
                    "notes": notes,
                    "tags": _clean_tags(data.get("tags")) if data else [],
                    "provider_id": "local-qwen3-4b",
                    "model": "Qwen/Qwen3-4B-Instruct-2507",
                },
            )
            done += 1
        translated = done
        elapsed = time.perf_counter() - started
        rate = translated / elapsed if elapsed > 0 else 0
        print(
            f"[local-translate] {min(start + batch_size, len(items))}/{len(items)} done={done} failed={failed} ({rate:.1f} pages/s)",
            flush=True,
        )
    return done, failed, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scope", choices=["model_vocab", "popular", "all"], default="popular")
    parser.add_argument("--min-post-count", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=2000)
    parser.add_argument("--limit", type=int, default=None, help="hard cap on pages for this run")
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--model-dir", default=DEFAULT_LOCAL_MODEL)
    args = parser.parse_args()

    service = app.state.runtime.tag_wiki
    request = TranslateRequest(
        scope=args.scope, min_post_count=args.min_post_count, max_pages=args.max_pages
    )
    # Reuse the service scope resolution: canonical names, page intersection,
    # artist/character link-list pages excluded.
    titles = service._translate_scope(request)
    missing = service.store.missing_summary_titles(titles)
    if args.limit is not None:
        missing = missing[: args.limit]
    print(f"[local-translate] scope={args.scope} candidates={len(titles)} to translate={len(missing)}")
    if not missing:
        print("[local-translate] nothing to do")
        return

    model_dir = Path(args.model_dir)
    if not model_dir.is_absolute():
        model_dir = project_root / model_dir
    tokenizer, model = _load_model(model_dir)

    items: list[tuple[str, str, str]] = []
    for title in missing:
        page = service.store.get_page(title)
        if page is None:
            continue
        text = page_context_text(page)
        if not text:
            continue
        items.append((title, str(page.get("title", title)), text))

    done, failed, _skipped = _translate_batch(tokenizer, model, items, args.batch_size)
    print(json.dumps({"done": done, "failed": failed, "total_summaries": service.store.summary_count()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
