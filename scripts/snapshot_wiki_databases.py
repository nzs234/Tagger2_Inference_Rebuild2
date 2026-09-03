"""Produce clean, compacted copies of the local wiki databases for packaging.

The release ships the fully built wiki databases (pages, chunks, embeddings
and the generated Chinese summaries) so end users never have to rebuild the
corpus. The copies are produced with SQLite ``VACUUM INTO``: self-contained
(no ``-wal``/``-shm`` sidecar files), defragmented and consistent even when
the application currently holds the databases open.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

# Profile -> database file name under <data>/tag_wiki/.
WIKI_DATABASES = {
    "e621": "tag_wiki.sqlite3",
    "danbooru": "tag_wiki_danbooru.sqlite3",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        required=True,
        help="project data directory containing tag_wiki/ (usually <root>/data)",
    )
    parser.add_argument(
        "--dest",
        required=True,
        help="destination directory that receives the copied databases",
    )
    args = parser.parse_args()

    source_dir = Path(args.data_dir) / "tag_wiki"
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    shipped = 0
    for profile, name in WIKI_DATABASES.items():
        source = source_dir / name
        if not source.is_file():
            print(f"[wiki-db] {profile}: not present, skipping ({source})")
            continue
        target = dest / name
        if target.exists():
            target.unlink()
        with sqlite3.connect(source) as conn:
            conn.execute("VACUUM INTO ?", (str(target),))
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"[wiki-db] {profile}: {target.name} ({size_mb:.1f} MB)")
        shipped += 1

    if shipped == 0:
        raise SystemExit(
            "no wiki databases found to ship: build at least one wiki profile first"
        )


if __name__ == "__main__":
    main()
