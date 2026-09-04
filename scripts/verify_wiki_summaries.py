"""Audit the accuracy of stored wiki Chinese summaries with a local judge model.

Every summary is re-checked against the wiki page text it was produced from:
a local judge model (default: the ``lmstudio`` provider, Gemma) receives the
page text plus the summary and returns a verdict JSON
(``accurate`` / ``minor`` / ``inaccurate`` + a one-line reason). Entries are
processed in small batches and every verdict is appended to a JSONL file the
moment it lands, so an interrupted sweep resumes without re-judging.

Read-only with respect to the wiki databases; verdicts go to ``--out``.

Usage::

    python scripts/verify_wiki_summaries.py --limit 8          # smoke test
    python scripts/verify_wiki_summaries.py                    # full sweep
    python scripts/verify_wiki_summaries.py --summary          # aggregate only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.providers.base import ProviderConfig, ProviderKind  # noqa: E402
from tagger2.providers.client import create_provider  # noqa: E402

DATABASES = {
    "e621": project_root / "data" / "tag_wiki" / "tag_wiki.sqlite3",
    "danbooru": project_root / "data" / "tag_wiki" / "tag_wiki_danbooru.sqlite3",
}

JUDGE_SYSTEM_PROMPT = (
    "你是 booru 标签百科翻译质量审核员。用户消息 JSON 包含若干条目：title（标签名）、"
    "text（英文百科原文，可能被截断）、summary（中文摘要，含 meaning/usage/pairing/notes/tags）。"
    "逐条判断中文摘要是否忠实于原文：含义有没有错、有没有编造原文没有的内容、usage 方向是否正确。"
    "轻微的措辞差异或简化判 accurate；实质错误（含义错误、编造、张冠李戴）判 inaccurate；"
    "介于两者之间判 minor。原文太短时只要求摘要不与原文矛盾。"
    "返回 ONLY 一个 JSON 对象：{\"verdicts\": [{\"title\": \"...\", \"verdict\": \"accurate|minor|inaccurate\", "
    "\"reason\": \"一句话中文理由\"}]}，verdicts 数组必须覆盖输入的每一个 title。不要输出 JSON 以外的内容。"
)

VERDICTS = {"accurate", "minor", "inaccurate"}


def _collect_entries(profile: str, path: Path) -> list[dict[str, str]]:
    from tagger2.tag_wiki.translator import page_context_text
    from tagger2.tag_wiki.wiki_store import WikiStore

    store = WikiStore(path)
    entries: list[dict[str, str]] = []
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT page_title, meaning, usage, pairing, notes, tags FROM summaries ORDER BY page_title"
        ).fetchall()
    for title, meaning, usage, pairing, notes, tags_json in rows:
        page = store.get_page(title)
        text = page_context_text(page) if page is not None else ""
        entries.append(
            {
                "profile": profile,
                "title": title,
                "text": text,
                "summary": {"meaning": meaning, "usage": usage, "pairing": pairing, "notes": notes, "tags": tags_json},
            }
        )
    store.close()
    return entries


def _load_done(out_path: Path) -> set[str]:
    done: set[str] = set()
    if out_path.is_file():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            done.add(f"{row.get('profile')}:{row.get('title')}")
    return done


async def _judge_batch(provider: Any, batch: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    payload = json.dumps(
        {"entries": [{"title": e["title"], "text": e["text"], "summary": e["summary"]} for e in batch]},
        ensure_ascii=False,
    )
    reply = await provider.generate(image=None, prompt=payload, model=None, system_prompt=JUDGE_SYSTEM_PROMPT)
    from tagger2.tag_wiki.translator import extract_summary_json

    data = extract_summary_json(str(reply or ""))
    verdicts: dict[str, dict[str, str]] = {}
    for item in (data or {}).get("verdicts", []):
        if not isinstance(item, dict) or "title" not in item:
            continue
        verdict = str(item.get("verdict", "")).strip().casefold()
        if verdict not in VERDICTS:
            verdict = "minor" if verdict else "error"
        verdicts[str(item["title"])] = {"verdict": verdict, "reason": str(item.get("reason", "")).strip()}
    return verdicts


async def _sweep(args: argparse.Namespace) -> None:
    from tagger2.providers.client import VisionProvider  # noqa: F401  (typing only)

    cfg = ProviderConfig(
        kind=ProviderKind.LM_STUDIO,
        base_url=args.base_url,
        model=args.model,
        temperature=0.1,
        top_p=0.9,
        max_output_tokens=2048,
        timeout_seconds=300.0,
        max_concurrency=args.concurrency,
        max_retries=1,
        allow_local=True,
        json_mode=False,
        id="verify-judge",
    )
    provider = create_provider(cfg)

    out_path = Path(args.out)
    done = _load_done(out_path)
    entries: list[dict[str, str]] = []
    for profile in args.profiles.split(","):
        entries.extend(_collect_entries(profile.strip(), DATABASES[profile.strip()]))
    todo = [e for e in entries if f"{e['profile']}:{e['title']}" not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(
        json.dumps({"total": len(entries), "already_checked": len(entries) - len(todo), "todo": len(todo)}, ensure_ascii=False),
        flush=True,
    )

    batches = [todo[i : i + args.batch_size] for i in range(0, len(todo), args.batch_size)]
    lock = asyncio.Lock()
    stats = {"accurate": 0, "minor": 0, "inaccurate": 0, "error": 0}
    started = time.monotonic()
    processed = 0

    async def worker(queue: asyncio.Queue[list[dict[str, str]] | None]) -> None:
        nonlocal processed
        out_path = Path(args.out)
        out_handle = open(out_path, "a", encoding="utf-8")
        try:
            while True:
                batch = await queue.get()
                if batch is None:
                    return
                verdicts: dict[str, dict[str, str]] = {}
                try:
                    verdicts = await _judge_batch(provider, batch)
                except Exception as exc:  # noqa: BLE001 - record and continue
                    if len(batch) > 1:
                        for entry in batch:
                            await queue.put([entry])
                        queue.task_done()
                        continue
                    for entry in batch:
                        verdicts[entry["title"]] = {"verdict": "error", "reason": str(exc)[:200]}
                lines = []
                for entry in batch:
                    verdict = verdicts.get(entry["title"], {"verdict": "error", "reason": "missing in judge reply"})
                    stats[verdict["verdict"]] = stats.get(verdict["verdict"], 0) + 1
                    lines.append(
                        json.dumps(
                            {"profile": entry["profile"], "title": entry["title"], **verdict},
                            ensure_ascii=False,
                        )
                    )
                async with lock:
                    out_handle.write("\n".join(lines) + "\n")
                    out_handle.flush()
                    processed += len(batch)
                    if processed % 40 < len(batch):
                        rate = processed / max(time.monotonic() - started, 1e-6) * 60
                        eta = (len(todo) - processed) / max(rate, 1e-6) / 60
                        print(
                            f"[verify] {processed}/{len(todo)} | "
                            f"accurate={stats['accurate']} minor={stats['minor']} inaccurate={stats['inaccurate']} error={stats['error']} "
                            f"| {rate:.0f}/min, ETA {eta:.1f}h",
                            flush=True,
                        )
                queue.task_done()
        finally:
            out_handle.close()

    queue: asyncio.Queue[list[dict[str, str]] | None] = asyncio.Queue()
    for batch in batches:
        queue.put_nowait(batch)
    workers = [asyncio.create_task(worker(queue)) for _ in range(max(1, args.concurrency))]
    for _ in workers:
        queue.put_nowait(None)
    await asyncio.gather(*workers)
    print(
        json.dumps({"finished": True, "processed": processed, **stats}, ensure_ascii=False),
        flush=True,
    )


def _aggregate(out_path: Path) -> None:
    rows: dict[str, dict[str, str]] = {}
    if out_path.is_file():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows[f"{row.get('profile')}:{row.get('title')}"] = row
    by_profile: dict[str, dict[str, int]] = {}
    flagged: list[dict[str, str]] = []
    for key, row in rows.items():
        profile = key.split(":", 1)[0]
        stats = by_profile.setdefault(profile, {})
        stats[row.get("verdict", "error")] = stats.get(row.get("verdict", "error"), 0) + 1
        if row.get("verdict") in {"inaccurate", "error"}:
            flagged.append(row)
    print(json.dumps({"checked": len(rows), "by_profile": by_profile}, ensure_ascii=False, indent=1))
    for row in flagged[:50]:
        print(f"- [{row['profile']}] {row['title']}: {row.get('verdict')} — {row.get('reason', '')[:120]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profiles", default="e621,danbooru", help="comma-separated profiles to audit")
    parser.add_argument("--provider", default="lmstudio")
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--model", default="gemma-4-31b-jang_4m-crack")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--out", default="data/tag_wiki/verify_results.jsonl")
    parser.add_argument("--limit", type=int, default=None, help="cap entries (smoke test)")
    parser.add_argument("--summary", action="store_true", help="aggregate the results file and exit")
    args = parser.parse_args()
    if args.summary:
        _aggregate(Path(args.out))
        return
    try:
        raise SystemExit(asyncio.run(_sweep(args)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
