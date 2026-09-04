"""Export retranslate-list entries as slices for the GLM arbitration judge.

The three-party re-judge needs every retranslated entry reviewed by the
session model (GLM) as well as the Gemma and CPA judges. GLM runs inside
ZCode subagents, so this script pre-dumps the judging material — the wiki
page text plus the stored Chinese summary — into small slice files a
subagent can read directly. Each slice is judged to a sibling
``out_NN.jsonl`` and closed with an ``out_NN.done`` marker.

Read-only with respect to the wiki databases.

Usage::

    python scripts/dump_arbiter_slices.py \
        --titles-file e621=data/tag_wiki/retranslate_e621.txt \
        --titles-file danbooru=data/tag_wiki/retranslate_danbooru.txt \
        --slice-size 80
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

DATABASES = {
    "e621": project_root / "data" / "tag_wiki" / "tag_wiki.sqlite3",
    "danbooru": project_root / "data" / "tag_wiki" / "tag_wiki_danbooru.sqlite3",
}


def _load_titles_filters(items: list[str]) -> dict[str, list[str]]:
    filters: dict[str, list[str]] = {}
    for item in items:
        profile, sep, path = item.partition("=")
        if not sep:
            raise SystemExit(f"--titles-file expects profile=path, got {item!r}")
        titles = [
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        filters.setdefault(profile.strip(), []).extend(titles)
    return filters


def _collect(profile: str, titles: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    from tagger2.tag_wiki.translator import page_context_text
    from tagger2.tag_wiki.wiki_store import WikiStore

    store = WikiStore(DATABASES[profile])
    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    for title in titles:
        with store.connection() as conn:
            row = conn.execute(
                "SELECT meaning, usage, pairing, notes, tags FROM summaries WHERE page_title=?",
                (title,),
            ).fetchone()
        page = store.get_page(title)
        text = page_context_text(page) if page is not None else ""
        if row is None:
            missing.append(title)
            continue
        meaning, usage, pairing, notes, tags_json = row
        entries.append(
            {
                "profile": profile,
                "title": title,
                "text": text,
                "summary": {
                    "meaning": meaning,
                    "usage": usage,
                    "pairing": pairing,
                    "notes": notes,
                    "tags": tags_json,
                },
            }
        )
    store.close()
    return entries, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--titles-file", action="append", required=True, metavar="PROFILE=PATH")
    parser.add_argument("--slice-size", type=int, default=80)
    parser.add_argument("--out-dir", default="data/tag_wiki/arbiter_glm")
    parser.add_argument(
        "--force", action="store_true", help="re-dump even if some slices are already judged"
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    slices_dir = out_dir / "slices"
    existing_done = list(slices_dir.glob("out_*.done"))
    if existing_done and not args.force:
        raise SystemExit(
            f"{len(existing_done)} slices already judged in {slices_dir}; pass --force to re-dump (would invalidate finished work)"
        )

    filters = _load_titles_filters(args.titles_file)
    entries: list[dict[str, Any]] = []
    missing: list[str] = []
    for profile, titles in filters.items():
        if profile not in DATABASES:
            raise SystemExit(f"unknown profile: {profile}")
        got, miss = _collect(profile, titles)
        entries.extend(got)
        missing.extend(miss)

    slices_dir.mkdir(parents=True, exist_ok=True)
    for old in slices_dir.glob("slice_*.json"):
        old.unlink()

    slice_size = max(1, args.slice_size)
    manifest = {
        "total": len(entries),
        "missing_no_summary": missing,
        "slice_size": slice_size,
        "slices": [],
    }
    for i in range(0, len(entries), slice_size):
        chunk = entries[i : i + slice_size]
        name = f"slice_{i // slice_size:02d}"
        (slices_dir / f"{name}.json").write_text(
            json.dumps({"entries": chunk}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        manifest["slices"].append(
            {"name": name, "file": str(slices_dir / f"{name}.json"), "entries": len(chunk)}
        )

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "dumped": len(entries),
                "slices": len(manifest["slices"]),
                "missing_no_summary": len(missing),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
