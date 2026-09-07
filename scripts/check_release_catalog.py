"""Validate the tag-wiki catalog inside release-staged wiki databases.

Used by ``scripts/build_release.ps1`` right after the wiki database snapshot:
the staged ``tag_wiki.sqlite3`` / ``tag_wiki_danbooru.sqlite3`` must carry a
non-empty, consistent high-frequency tag catalog, otherwise the packaged
Tag Wiki front page would answer 409 ``wiki_catalog_missing`` for every
user. A maintainer who rebuilt the wiki corpus (or the classification
snapshot) but skipped ``scripts/build_tag_wiki_catalog.py`` is stopped here
instead of shipping a broken directory.

Checks per database:

- the three ``catalog_*`` tables exist (schema v2);
- the catalog is not empty;
- no tag sits below the hard floor of 100 posts;
- ``catalog_meta.min_post_count`` is present and at least 100, and no tag
  falls below that recorded threshold (mixed-generation corruption);
- no relation endpoint points outside the catalog.

Exit codes: 0 ok, 1 validation failure (one line per failure on stderr).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# The hard floor from the user-facing contract (contracts.CATALOG_MIN_POST_COUNT
# is not imported on purpose: the release check must run before the packaged
# backend is importable, and the value is a product decision this gate guards).
REQUIRED_MIN_POST_COUNT = 100

REQUIRED_TABLES = ("catalog_tags", "catalog_relations", "catalog_meta")


def check_database(path: Path) -> list[str]:
    """Return the list of validation failures for one wiki database."""

    failures: list[str] = []
    name = path.name
    conn = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [table for table in REQUIRED_TABLES if table not in tables]
        if missing:
            failures.append(
                f"{name}: catalog tables missing ({', '.join(missing)});"
                " run scripts/build_tag_wiki_catalog.py before packaging"
            )
            return failures

        tag_count = int(conn.execute("SELECT COUNT(*) FROM catalog_tags").fetchone()[0])
        if tag_count == 0:
            failures.append(
                f"{name}: catalog is empty; run scripts/build_tag_wiki_catalog.py"
            )
            return failures

        below_floor = int(
            conn.execute(
                f"SELECT COUNT(*) FROM catalog_tags WHERE post_count < {REQUIRED_MIN_POST_COUNT}"  # noqa: S608
            ).fetchone()[0]
        )
        if below_floor:
            failures.append(
                f"{name}: {below_floor} catalog tags below post_count"
                f" {REQUIRED_MIN_POST_COUNT}"
            )

        meta = {
            str(key): str(value)
            for key, value in conn.execute("SELECT key, value FROM catalog_meta")
        }
        threshold = int(meta.get("min_post_count") or 0)
        if threshold < REQUIRED_MIN_POST_COUNT:
            failures.append(
                f"{name}: catalog min_post_count is {threshold},"
                f" below the required {REQUIRED_MIN_POST_COUNT}"
            )
        below_meta = int(
            conn.execute(
                "SELECT COUNT(*) FROM catalog_tags WHERE post_count < ?", (threshold,)
            ).fetchone()[0]
        )
        if below_meta:
            failures.append(
                f"{name}: {below_meta} catalog tags below their own threshold"
                f" {threshold}"
            )

        dangling = int(
            conn.execute(
                "SELECT COUNT(*) FROM catalog_relations r"
                " WHERE r.tag_name NOT IN (SELECT name FROM catalog_tags)"
                " OR r.related_name NOT IN (SELECT name FROM catalog_tags)"
            ).fetchone()[0]
        )
        if dangling:
            failures.append(
                f"{name}: {dangling} catalog relations point outside the catalog"
            )

        print(f"release catalog check: {name}: {tag_count} tags, threshold {threshold}")
    finally:
        conn.close()
    return failures


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: check_release_catalog.py <wiki-data-dir>", file=sys.stderr)
        raise SystemExit(2)
    data_dir = Path(sys.argv[1])
    failures: list[str] = []
    for filename in ("tag_wiki.sqlite3", "tag_wiki_danbooru.sqlite3"):
        path = data_dir / filename
        if not path.is_file():
            failures.append(f"{filename}: staged wiki database is missing")
            continue
        failures.extend(check_database(path))
    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
