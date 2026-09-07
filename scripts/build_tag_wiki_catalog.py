"""Build the read-only tag-wiki catalog (booru-style tag directory) from the CLI.

The Tag Wiki front page is a browsable directory of high-frequency tags:
official category first, then a deterministic semantic group, with fuzzy
search over canonical names and aliases. The data behind it lives in the
per-profile wiki SQLite database (``catalog_tags`` / ``catalog_relations`` /
``catalog_meta`` tables, schema v2) and is maintained exclusively by this
script — the running app and the frontend only ever read it.

Usage::

    runtime\\python.exe scripts/build_tag_wiki_catalog.py --profile all
    runtime\\python.exe scripts/build_tag_wiki_catalog.py --profile e621 --min-post-count 200
    runtime\\python.exe scripts/build_tag_wiki_catalog.py --status

Per profile the builder:

1. Loads the runtime classification snapshot through :class:`TagDatabase`
   (the same snapshot the tagger/classifier already uses — no new downloads)
   and takes every canonical tag with ``post_count >= --min-post-count``
   (default 100).
2. Groups each tag via the deterministic taxonomy in
   ``tagger2/tag_wiki/taxonomy.py`` (no model calls) and marks whether a
   local wiki page exists for it.
3. Combines tag-database implications (forward rows; the reverse direction
   is answered by the store at query time) with the wiki ``page_links``
   graph into catalog relations. Only relations whose BOTH endpoints made
   the catalog are kept, so every related tag the UI shows is clickable and
   high-frequency. No online co-occurrence scraping happens here.
4. Writes everything in one atomic transaction (``WikiStore.replace_catalog``)
   together with meta (threshold, taxonomy version, generation time, counts).

Re-running is safe: the catalog is rebuilt wholesale, wiki pages/chunks/
summaries are never touched. Exit codes: 0 success, 1 build error, 2 setup
error (missing classification snapshot for a requested profile).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

from tagger2.tag_manager.tag_db import TagDatabase, TagDatabaseError  # noqa: E402
from tagger2.tag_wiki.contracts import CATALOG_MIN_POST_COUNT  # noqa: E402
from tagger2.tag_wiki.taxonomy import TAXONOMY_VERSION, group_for_tag  # noqa: E402
from tagger2.tag_wiki.wiki_store import WikiStore, normalize_title  # noqa: E402


def default_store_for_profile(profile: str, data_dir: Path | None = None) -> WikiStore:
    """The per-profile wiki database the app serves at runtime."""

    if data_dir is None:
        from tagger2.config import get_settings

        settings = get_settings()
        data_dir = settings.data_dir or settings.project_root / "data"
    if profile == "e621":
        return WikiStore(data_dir / "tag_wiki" / "tag_wiki.sqlite3")
    if profile == "danbooru":
        return WikiStore(data_dir / "tag_wiki" / "tag_wiki_danbooru.sqlite3")
    raise ValueError(f"unsupported tag wiki profile: {profile!r}")


def build_catalog(
    profile: str,
    *,
    store: WikiStore,
    tag_database: TagDatabase,
    min_post_count: int = CATALOG_MIN_POST_COUNT,
    dry_run: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """Compute (and unless ``dry_run`` store) one profile's catalog.

    Returns a statistics document: tag/relation counts, per-category and
    per-group breakdowns, and how many relation endpoints were dropped for
    being below the threshold (kept out of the catalog on purpose).
    """

    started = time.monotonic()
    tag_database.ensure_loaded(profile)

    # 1. Canonical high-frequency tags (sorted post_count desc, name asc).
    infos = tag_database.top_tags(profile, min_post_count=min_post_count)

    wiki_titles = set(store.iter_page_titles())

    tags: list[dict[str, Any]] = []
    post_counts: dict[str, float] = {}
    category_counts: dict[str, int] = {}
    group_counts: dict[str, int] = {}
    wiki_count = 0
    for info in infos:
        name = normalize_title(str(info["name"]))
        if not name or name in post_counts:
            continue
        category = str(info["category"] or "")
        group = group_for_tag(profile, category, name)
        has_wiki = name in wiki_titles
        post_count = int(info["post_count"] or 0)
        tags.append(
            {
                "name": name,
                "category": category,
                "group_key": group,
                "post_count": post_count,
                "has_wiki": has_wiki,
                "alias_of": info.get("alias_of"),
            }
        )
        post_counts[name] = float(post_count)
        wiki_count += 1 if has_wiki else 0
        category_counts[category] = category_counts.get(category, 0) + 1
        group_counts[group] = group_counts.get(group, 0) + 1

    catalog_names = frozenset(post_counts)

    # 2. Relations: forward implications + wiki page links. Both endpoints
    # must be catalog members, otherwise the UI could link below-threshold
    # (or missing) tags.
    relations: list[dict[str, Any]] = []
    dropped_relation_endpoints = 0
    relation_type_counts: dict[str, int] = {}
    for tag in tags:
        name = tag["name"]
        try:
            implied = tag_database.implications_of(profile, name)
        except TagDatabaseError:
            implied = []
        for target in implied:
            target_name = normalize_title(str(target["name"]))
            if target_name == name:
                continue
            if target_name not in catalog_names:
                dropped_relation_endpoints += 1
                continue
            relations.append(
                {
                    "tag_name": name,
                    "related_name": target_name,
                    "relation_type": "implication",
                    "score": post_counts.get(target_name, 0.0),
                    "source": "tag_implications",
                }
            )
            relation_type_counts["implication"] = relation_type_counts.get("implication", 0) + 1

    for page_title, link_title in store.iter_page_links():
        if page_title not in catalog_names or link_title not in catalog_names:
            dropped_relation_endpoints += 1
            continue
        relations.append(
            {
                "tag_name": page_title,
                "related_name": link_title,
                "relation_type": "wiki_link",
                "score": post_counts.get(link_title, 0.0),
                "source": "page_links",
            }
        )
        relation_type_counts["wiki_link"] = relation_type_counts.get("wiki_link", 0) + 1

    generated_at = now  # caller-stampable for tests; real runs stamp below
    if generated_at is None:
        from tagger2.workflow.contracts import utc_now

        generated_at = utc_now()

    meta: dict[str, Any] = {
        "profile": profile,
        "generated_at": generated_at,
        "taxonomy_version": TAXONOMY_VERSION,
        "min_post_count": min_post_count,
        "tag_count": len(tags),
        "relation_count": len(relations),
    }

    stats: dict[str, Any] = {
        **meta,
        "with_wiki_page": wiki_count,
        "category_counts": dict(sorted(category_counts.items())),
        "group_counts": dict(sorted(group_counts.items())),
        "relation_type_counts": dict(sorted(relation_type_counts.items())),
        "dropped_relation_endpoints": dropped_relation_endpoints,
        "seconds": round(time.monotonic() - started, 2),
    }

    if not dry_run:
        store.replace_catalog(tags, relations, {k: str(v) for k, v in meta.items()})
    return stats


def _status(profiles: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for profile in profiles:
        store = default_store_for_profile(profile)
        meta = store.catalog_meta() if store.catalog_built() else {}
        result[profile] = {
            "built": store.catalog_built(),
            "tag_count": store.catalog_tag_count(),
            "relation_count": store.catalog_relation_count(),
            **meta,
        }
        store.close()
    return result


def _print_stats(stats: Mapping[str, Any]) -> None:
    print(f"[{stats['profile']}] catalog built: {stats['tag_count']} tags, "
          f"{stats['relation_count']} relations ({stats['seconds']}s)")
    print(f"  threshold: post_count >= {stats['min_post_count']}  "
          f"taxonomy v{stats['taxonomy_version']}  generated {stats['generated_at']}")
    print(f"  with wiki page: {stats['with_wiki_page']}")
    print(f"  dropped relation endpoints (below threshold): {stats['dropped_relation_endpoints']}")
    print("  tags by category:")
    for category, count in stats["category_counts"].items():
        print(f"    {category or '(uncategorized)'}: {count}")
    print("  tags by group:")
    for group, count in stats["group_counts"].items():
        print(f"    {group}: {count}")
    print("  relations by type:")
    for relation_type, count in stats["relation_type_counts"].items():
        print(f"    {relation_type}: {count}")


def _profiles_for_arg(value: str) -> list[str]:
    if value == "all":
        return ["e621", "danbooru"]
    return [value]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--profile",
        choices=["e621", "danbooru", "all"],
        default="all",
        help="which wiki mirror's catalog to (re)build (default: all)",
    )
    parser.add_argument(
        "--min-post-count",
        type=int,
        default=CATALOG_MIN_POST_COUNT,
        help="only canonical tags with at least this many posts enter the catalog (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print the statistics without writing the database",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="print the current catalog meta per profile and exit",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    profiles = _profiles_for_arg(args.profile)

    if args.status:
        print(json.dumps(_status(profiles), ensure_ascii=False, indent=2))
        return

    tag_database = TagDatabase()
    exit_code = 0
    for profile in profiles:
        try:
            store = default_store_for_profile(profile)
        except ValueError as exc:
            print(f"[{profile}] {exc}", file=sys.stderr)
            exit_code = 2
            continue
        try:
            stats = build_catalog(
                profile,
                store=store,
                tag_database=tag_database,
                min_post_count=args.min_post_count,
                dry_run=args.dry_run,
            )
        except TagDatabaseError as exc:
            print(
                f"[{profile}] classification snapshot unavailable: {exc}",
                file=sys.stderr,
            )
            exit_code = 2
            continue
        finally:
            store.close()
        _print_stats(stats)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
