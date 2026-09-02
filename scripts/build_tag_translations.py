"""Build the offline Chinese tag-translation dictionaries.

The tag manager renders every booru tag bilingually, so it needs a local
English -> Chinese table for both profiles. Nothing here translates anything:
the script downloads published community dictionaries and merges them into one
deterministic file per profile.

Sources, in descending precedence (an earlier source wins a conflict):

1. ``amenorira/danbooru-tags-data-zh`` (MIT) -- hand-curated, carries aliases
   and per-tag notes; currently covers artist / copyright / meta.
2. ``GuWuW/danbooru-dict`` (no license declared) -- the dictionary the NAI
   autocomplete extensions ship, refreshed daily, covers the common tags.
3. ``ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table`` (no license
   declared) -- the widest coverage (325k tags, ``post_count >= 10``), machine
   translated and then proofread.

Alias names are emitted too, pointing at their canonical tag's translation, but
always below a canonical entry so a real translation is never shadowed.

e621 publishes no Chinese dictionary. Its output is therefore the subset of the
merged Danbooru table whose names exist in the registered e621 classification
snapshot, which covers the general tags the two sites share.

Usage::

    .\\runtime\\python.exe scripts/build_tag_translations.py
    .\\runtime\\python.exe scripts/build_tag_translations.py --offline
    .\\runtime\\python.exe scripts/build_tag_translations.py --sources amenorira,guwuw

Output (committed so the app works with no network):
    resources/tag_translations/danbooru-zh.csv.gz
    resources/tag_translations/e621-zh.csv.gz
    resources/tag_translations/MANIFEST.json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "backend"))

OUTPUT_DIR = project_root / "resources" / "tag_translations"
MANIFEST_FORMAT = "tag-translations-v1"

# A translation longer than this is a description rather than a name; the
# source dictionaries occasionally leak one and it only bloats the pill.
MAX_TRANSLATION_LENGTH = 64

GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class Source:
    """One upstream dictionary, its license and the blobs to fetch."""

    key: str
    repo: str
    branch: str
    paths: tuple[str, ...]
    license_id: str
    license_url: str
    note: str


SOURCES: tuple[Source, ...] = (
    Source(
        key="amenorira",
        repo="amenorira/danbooru-tags-data-zh",
        branch="main",
        paths=("tags/artist.csv", "tags/copyright.csv", "tags/meta.csv"),
        license_id="MIT",
        license_url="https://github.com/amenorira/danbooru-tags-data-zh/blob/main/LICENSE",
        note="人工整理，含别名；当前覆盖 artist/copyright/meta",
    ),
    Source(
        key="guwuw",
        repo="GuWuW/danbooru-dict",
        branch="main",
        paths=("tags.csv",),
        license_id="NOASSERTION",
        license_url="https://github.com/GuWuW/danbooru-dict",
        note="社区词典，每日更新，覆盖常用标签；仓库未声明许可",
    ),
    Source(
        key="ffdkj",
        repo="ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table",
        branch="main",
        paths=("tag.sqlite",),
        license_id="NOASSERTION",
        license_url="https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table",
        note="覆盖最广（post_count>=10），机器翻译后人工校对；仓库未声明许可",
    ),
)


def normalize_tag(value: str) -> str:
    """Return the dictionary key form: lowercase, underscores, no padding."""

    tag = value.strip().replace("\ufeff", "").replace(" ", "_").casefold()
    if not tag or any(character in tag for character in "\x00\r\n,"):
        return ""
    return tag


def normalize_translation(tag: str, value: str) -> str:
    """Clean one Chinese name, rejecting echoes of the English tag."""

    text = " ".join(str(value).replace("\ufeff", "").split())
    if not text or len(text) > MAX_TRANSLATION_LENGTH:
        return ""
    # Untranslated rows (common for artists) repeat the English name; keeping
    # them would render "canid_kaiba · canid kaiba" in the UI.
    if text.replace(" ", "_").casefold() == tag:
        return ""
    return text


def _api_json(url: str) -> dict[str, object]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "tagger2-build", "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected GitHub API payload for {url}")
    return payload


def download_source(source: Source, cache_dir: Path, *, offline: bool) -> dict[str, Path]:
    """Fetch (or reuse from cache) every blob of one source.

    GitHub's blob API is used rather than ``raw.githubusercontent.com`` because
    the raw host is unreliable behind some networks and the blob endpoint serves
    files well past the 1 MiB contents-API cap.
    """

    cache_dir.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, Path] = {}
    tree: list[dict[str, object]] | None = None
    for path in source.paths:
        destination = cache_dir / f"{source.key}-{path.replace('/', '-')}"
        resolved[path] = destination
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        if offline:
            raise RuntimeError(f"--offline was requested but {destination} is missing")
        if tree is None:
            document = _api_json(
                f"{GITHUB_API}/repos/{source.repo}/git/trees/{source.branch}?recursive=1"
            )
            entries = document.get("tree")
            tree = list(entries) if isinstance(entries, list) else []
        blob = next((item for item in tree if item.get("path") == path), None)
        if blob is None:
            raise RuntimeError(f"{source.repo} has no blob at {path}")
        request = urllib.request.Request(
            f"{GITHUB_API}/repos/{source.repo}/git/blobs/{blob['sha']}",
            headers={"User-Agent": "tagger2-build", "Accept": "application/vnd.github.raw"},
        )
        with urllib.request.urlopen(request, timeout=600) as response:
            data = response.read()
        destination.write_bytes(data)
        print(f"    downloaded {path} ({len(data):,} bytes)")
    return resolved


def read_amenorira(paths: dict[str, Path]) -> Iterator[tuple[str, str, bool]]:
    """``tag,category,aliases,zh,count,notes`` with a UTF-8 BOM, aliases on ``|``."""

    for path in paths.values():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                tag = normalize_tag(row.get("tag") or "")
                translation = normalize_translation(tag, row.get("zh") or "")
                if not tag or not translation:
                    continue
                yield tag, translation, False
                for alias in (row.get("aliases") or "").split("|"):
                    alias_key = normalize_tag(alias)
                    if alias_key and alias_key != tag:
                        yield alias_key, translation, True


def read_guwuw(paths: dict[str, Path]) -> Iterator[tuple[str, str, bool]]:
    """Headerless ``name,category,post_count,"aliases",zh``."""

    path = next(iter(paths.values()))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) < 5:
                continue
            tag = normalize_tag(row[0])
            translation = normalize_translation(tag, row[4])
            if not tag or not translation:
                continue
            yield tag, translation, False
            for alias in row[3].split(","):
                # Leading-slash entries are the autocomplete extension's own
                # keyboard shortcuts, not booru aliases.
                if alias.strip().startswith("/"):
                    continue
                alias_key = normalize_tag(alias)
                if alias_key and alias_key != tag:
                    yield alias_key, translation, True


def read_ffdkj(paths: dict[str, Path]) -> Iterator[tuple[str, str, bool]]:
    """SQLite table ``tags(name, category, cn_name, post_count)``."""

    path = next(iter(paths.values()))
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for name, cn_name in connection.execute("SELECT name, cn_name FROM tags"):
            tag = normalize_tag(str(name or ""))
            translation = normalize_translation(tag, str(cn_name or ""))
            if tag and translation:
                yield tag, translation, False
    finally:
        connection.close()


READERS = {
    "amenorira": read_amenorira,
    "guwuw": read_guwuw,
    "ffdkj": read_ffdkj,
}

# e621's own vocabulary (species, anatomy, aspect ratios) has no published
# Chinese dictionary, and several names mean something different than on
# Danbooru -- "female" is 雌性 rather than a girl count.  This editable glossary
# is kept in the repository and always wins for the e621 profile.
E621_SUPPLEMENT = OUTPUT_DIR / "e621-supplement-zh.csv"


def read_supplement(path: Path) -> dict[str, str]:
    """Read the curated ``tag,zh`` glossary; a missing file is not an error."""

    if not path.is_file():
        return {}
    entries: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            tag = normalize_tag(row.get("tag") or "")
            translation = normalize_translation(tag, row.get("zh") or "")
            if tag and translation:
                entries[tag] = translation
    return entries


def write_csv_gz(path: Path, rows: dict[str, str]) -> tuple[int, str]:
    """Write the sorted ``tag,zh`` table; returns (entries, sha256).

    ``mtime=0`` keeps the gzip container byte-identical across rebuilds so the
    committed file only changes when the data does.
    """

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["tag", "zh"])
    for tag in sorted(rows):
        writer.writerow([tag, rows[tag]])
    payload = buffer.getvalue().encode("utf-8")

    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
        compressed.write(payload)
    data = raw.getvalue()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(rows), hashlib.sha256(data).hexdigest()


def e621_tag_namespace() -> set[str]:
    """Every canonical name and alias antecedent in the e621 snapshot."""

    from tagger2.workflow.resources import CLASSIFY_RESOURCE_CATEGORY, WorkflowResourceCatalog

    catalog = WorkflowResourceCatalog(project_root / "data" / "workflows" / "resources")
    candidates = [
        manifest
        for manifest in catalog.list_resources(CLASSIFY_RESOURCE_CATEGORY)
        if manifest.profile == "e621"
    ]
    if not candidates:
        return set()
    newest = sorted(candidates, key=lambda manifest: manifest.created_at, reverse=True)[0]
    path = catalog.get_resource_path(newest.resource_id)
    if path is None:
        return set()
    document = json.loads(path.read_text(encoding="utf-8"))
    names = {
        normalize_tag(str(row.get("name", "")))
        for row in document.get("tags", ())
        if isinstance(row, dict)
    }
    names |= {
        normalize_tag(str(row.get("antecedent_name", "")))
        for row in document.get("aliases", ())
        if isinstance(row, dict)
    }
    names.discard("")
    return names


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        default=",".join(source.key for source in SOURCES),
        help="comma-separated subset of: " + ", ".join(source.key for source in SOURCES),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=project_root / ".tmp-tag-translations",
        help="where downloaded upstream files are kept between runs",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="fail instead of downloading; reuse whatever the cache holds",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=OUTPUT_DIR,
        help="where the generated dictionaries are written",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = [key.strip() for key in str(args.sources).split(",") if key.strip()]
    unknown = [key for key in selected if key not in READERS]
    if unknown:
        print(f"ERROR: unknown source(s): {', '.join(unknown)}")
        return 1

    canonical: dict[str, str] = {}
    alias_only: dict[str, str] = {}
    used: list[dict[str, object]] = []

    for source in SOURCES:
        if source.key not in selected:
            continue
        print(f"Source {source.key} ({source.repo}, {source.license_id})")
        try:
            paths = download_source(source, Path(args.cache_dir), offline=args.offline)
        except (OSError, RuntimeError) as exc:
            print(f"ERROR: {source.key}: {exc}")
            return 1
        added = 0
        alias_added = 0
        for tag, translation, is_alias in READERS[source.key](paths):
            if is_alias:
                if tag in canonical or tag in alias_only:
                    continue
                alias_only[tag] = translation
                alias_added += 1
                continue
            if tag in canonical:
                continue
            canonical[tag] = translation
            alias_only.pop(tag, None)
            added += 1
        print(f"    canonical +{added:,}   alias +{alias_added:,}")
        used.append(
            {
                "name": source.repo,
                "url": f"https://github.com/{source.repo}",
                "license": source.license_id,
                "license_url": source.license_url,
                "entries_used": added,
                "aliases_used": alias_added,
                "note": source.note,
            }
        )

    if not canonical:
        print("ERROR: no translations were collected")
        return 1

    danbooru = {**alias_only, **canonical}
    print(f"Merged danbooru: {len(danbooru):,} entries")

    e621_names = e621_tag_namespace()
    if e621_names:
        e621 = {tag: text for tag, text in danbooru.items() if tag in e621_names}
    else:
        print("  WARNING: no e621 classification snapshot is registered;"
              " the e621 dictionary falls back to the full Danbooru table")
        e621 = dict(danbooru)
    supplement = read_supplement(E621_SUPPLEMENT)
    if supplement:
        e621.update(supplement)
        used.append(
            {
                "name": "resources/tag_translations/e621-supplement-zh.csv",
                "url": "",
                "license": "in-repo",
                "license_url": "",
                "entries_used": len(supplement),
                "aliases_used": 0,
                "note": "仓库内维护的 e621 专有词汇表（物种/解剖/画幅），对 e621 优先生效",
            }
        )
    print(f"Merged e621:     {len(e621):,} entries"
          f" (curated supplement: {len(supplement):,})")

    out_dir = Path(args.out_dir)
    danbooru_count, danbooru_hash = write_csv_gz(out_dir / "danbooru-zh.csv.gz", danbooru)
    e621_count, e621_hash = write_csv_gz(out_dir / "e621-zh.csv.gz", e621)

    manifest = {
        "format": MANIFEST_FORMAT,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "profiles": {
            "danbooru": {
                "file": "danbooru-zh.csv.gz",
                "entries": danbooru_count,
                "sha256": danbooru_hash,
            },
            "e621": {
                "file": "e621-zh.csv.gz",
                "entries": e621_count,
                "sha256": e621_hash,
                "note": "Danbooru 词条与 e621 标签命名空间的交集",
            },
        },
        "sources": used,
    }
    (out_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print("Written:")
    for profile, info in manifest["profiles"].items():  # type: ignore[union-attr]
        path = out_dir / str(info["file"])  # type: ignore[index]
        print(f"  {profile:<9} {path.name}  {path.stat().st_size:,} bytes"
              f"  {info['entries']:,} entries")  # type: ignore[index]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
