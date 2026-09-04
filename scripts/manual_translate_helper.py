"""Helpers for translating wiki pages with the assistant session's own model.

Two subcommands keep the worker loop mechanical (a worker agent never needs
to touch SQLite or the store API itself):

- ``next``: dump the first ``--count`` not-yet-summarized titles of a slice
  file into a JSON file (title -> {display_title, text}) using the exact page
  text the regular pipeline feeds the model. Pages whose rendered text is
  empty are unsummarizable; they are dropped from the slice and reported.
- ``apply``: validate a worker's output JSON (title -> summary fields),
  persist valid entries through :class:`WikiStore`, remove them from the
  slice file and print what remains. Entries without any usable text field
  are rejected and stay in the slice for a retry.

Slice files shrink as work completes, so interrupted runs resume without
duplicating pages.

Usage::

    python scripts/manual_translate_helper.py next --slice s1.txt --count 15 --out in.json
    python scripts/manual_translate_helper.py apply --slice s1.txt --in out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.tag_wiki.translator import _clean_field, _clean_tags  # noqa: E402
from tagger2.tag_wiki.wiki_store import WikiStore, default_tag_wiki_database_path  # noqa: E402
from tagger2.tag_wiki.translator import page_context_text  # noqa: E402

TEXT_FIELDS = ("meaning", "usage", "pairing", "notes")


def _load_slice(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _save_slice(path: Path, titles: list[str]) -> None:
    path.write_text("\n".join(titles) + ("\n" if titles else ""), encoding="utf-8")


def _cmd_next(args: argparse.Namespace) -> None:
    store = WikiStore(default_tag_wiki_database_path())
    slice_titles = _load_slice(Path(args.slice))
    pages: dict[str, dict[str, str]] = {}
    skipped_empty: list[str] = []
    for title in slice_titles:
        if len(pages) >= args.count:
            break
        page = store.get_page(title)
        text = page_context_text(page) if page is not None else ""
        if not text:
            skipped_empty.append(title)
            continue
        pages[title] = {
            "display_title": str(page.get("display_title") or page.get("title", title)),
            "text": text,
        }
    store.close()
    done = set(pages) | set(skipped_empty)
    remaining = [t for t in slice_titles if t not in done]
    _save_slice(Path(args.slice), remaining)
    Path(args.out).write_text(json.dumps(pages, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        json.dumps(
            {"dumped": len(pages), "skipped_empty": skipped_empty, "remaining_in_slice": len(remaining)},
            ensure_ascii=False,
        )
    )


def _cmd_apply(args: argparse.Namespace) -> None:
    store = WikiStore(default_tag_wiki_database_path())
    slice_titles = _load_slice(Path(args.slice))
    worker = json.loads(Path(args.in_file).read_text(encoding="utf-8"))

    applied: list[str] = []
    rejected: dict[str, str] = {}
    for title, entry in worker.items():
        if not isinstance(entry, dict):
            rejected[title] = "entry is not an object"
            continue
        summary = {name: _clean_field(entry.get(name)) for name in TEXT_FIELDS}
        tags = _clean_tags(entry.get("tags"))
        if not any(summary[name] for name in TEXT_FIELDS):
            rejected[title] = "all text fields empty"
            continue
        store.upsert_summary(
            title,
            {
                **summary,
                "tags": tags,
                "provider_id": args.provider_id,
                "model": args.model,
            },
        )
        applied.append(title)

    remaining = [t for t in slice_titles if t not in set(applied)]
    store.close()
    _save_slice(Path(args.slice), remaining)
    print(
        json.dumps(
            {"applied": applied, "rejected": rejected, "remaining_in_slice": len(remaining)},
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_next = sub.add_parser("next", help="dump the next unsummarized pages of a slice")
    p_next.add_argument("--slice", required=True)
    p_next.add_argument("--count", type=int, default=15)
    p_next.add_argument("--out", required=True)
    p_next.add_argument("--profile", choices=["e621", "danbooru"], default="e621")
    p_next.set_defaults(func=_cmd_next)

    p_apply = sub.add_parser("apply", help="persist a worker's summaries and shrink the slice")
    p_apply.add_argument("--slice", required=True)
    p_apply.add_argument("--in", dest="in_file", required=True)
    p_apply.add_argument("--provider-id", default="glm-5.3-flash-session")
    p_apply.add_argument("--model", default="glm-5.3-flash")
    p_apply.set_defaults(func=_cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
