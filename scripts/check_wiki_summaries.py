"""Quality audit for stored wiki Chinese summaries.

Scans the ``summaries`` table of one or both wiki profiles for three kinds of
model-output problems the translate job cannot catch on its own:

- refusal / "cannot translate" phrasing (the model refusing the task),
- truncation artifacts (fields that hit the 400-char cleaner and end in an
  ellipsis — cosmetic, but worth counting),
- echo / wrong-language output (prompt JSON leaked into fields, mostly-ASCII
  text, markdown fences).

Read-only: the script never writes to the databases.

Usage::

    python scripts/check_wiki_summaries.py                # both profiles
    python scripts/check_wiki_summaries.py --profile danbooru
    python scripts/check_wiki_summaries.py --samples 10   # samples per issue
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATABASES = {
    "e621": PROJECT_ROOT / "data" / "tag_wiki" / "tag_wiki.sqlite3",
    "danbooru": PROJECT_ROOT / "data" / "tag_wiki" / "tag_wiki_danbooru.sqlite3",
}

# Phrases that signal the model talked about the task instead of doing it.
# Some are legitimate wiki vocabulary (e.g. 「无法」 inside a usage note), so
# matches are listed as samples for eyeballing, not auto-deleted.
REFUSAL_PATTERNS = [
    "无法翻译",
    "不能翻译",
    "无法提供",
    "无法完成",
    "无法生成",
    "我无法",
    "无法满足",
    "无法处理该",
    "作为一个AI",
    "作为AI",
    "AI助手",
    "AI 模型",
    "语言模型",
    "抱歉",
    "对不起",
    "I cannot",
    "I can't",
    "I can not",
    "I'm sorry",
    "I am sorry",
    "cannot assist",
    "cannot provide",
    "unable to",
    "against my",
]

TRUNCATION_SUFFIX = "…"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ECHO_MARKERS = ['{"', '"tag"', '"text"', "wiki content", "```"]


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(CJK_RE.findall(text)) / len(text)


def check_profile(profile: str, path: Path, samples: int) -> None:
    if not path.is_file():
        print(f"[{profile}] 数据库不存在：{path}")
        return
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT page_title, meaning, usage, pairing, notes FROM summaries"
    ).fetchall()
    conn.close()

    fields = ("meaning", "usage", "pairing", "notes")
    refusals: list[tuple[str, str, str]] = []
    truncated: list[str] = []
    echoes: list[tuple[str, str]] = []
    ascii_like: list[tuple[str, str]] = []
    field_stats = {name: 0 for name in fields}

    for title, meaning, usage, pairing, notes in rows:
        values = {"meaning": meaning or "", "usage": usage or "", "pairing": pairing or "", "notes": notes or ""}
        for name in fields:
            if values[name]:
                field_stats[name] += 1
        for name, value in values.items():
            for phrase in REFUSAL_PATTERNS:
                if phrase in value:
                    pos = value.find(phrase)
                    snippet = value[max(0, pos - 20) : pos + 40]
                    refusals.append((title, name, snippet.replace("\n", " ")))
                    break
            if value.endswith(TRUNCATION_SUFFIX):
                truncated.append(f"{title}.{name}")
            for marker in ECHO_MARKERS:
                if marker in value:
                    echoes.append((f"{title}.{name}", marker))
                    break
            if name == "meaning" and len(value) >= 20 and _cjk_ratio(value) < 0.3:
                ascii_like.append((title, value[:60].replace("\n", " ")))

    total = len(rows)
    print(f"\n===== [{profile}] 摘要质量检查：共 {total} 条 =====")
    print(f"字段覆盖：meaning={field_stats['meaning']} usage={field_stats['usage']} "
          f"pairing={field_stats['pairing']} notes={field_stats['notes']}")
    print(f"1) 拒答/无法翻译类命中：{len(refusals)} 条")
    for title, field, snippet in refusals[:samples]:
        print(f"   - {title} [{field}] …{snippet}…")
    print(f"2) 字段被 400 字截断（以 … 结尾）：{len(truncated)} 条")
    for item in truncated[:samples]:
        print(f"   - {item}")
    print(f"3) 疑似提示词回声/围栏泄漏：{len(echoes)} 条")
    for title, marker in echoes[:samples]:
        print(f"   - {title} 含 {marker!r}")
    print(f"4) meaning 几乎非中文（CJK<30%）：{len(ascii_like)} 条")
    for title, snippet in ascii_like[:samples]:
        print(f"   - {title}: {snippet}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=["e621", "danbooru"], default=None, help="只检查一个库（默认两个都查）")
    parser.add_argument("--samples", type=int, default=8, help="每类问题展示的样例数")
    args = parser.parse_args()
    targets = {args.profile: DATABASES[args.profile]} if args.profile else DATABASES
    for profile, path in targets.items():
        check_profile(profile, path, args.samples)


if __name__ == "__main__":
    main()
