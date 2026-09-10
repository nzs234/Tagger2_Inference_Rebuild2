"""Benchmark the tag manager scan and its SQLite-backed grid queries.

Generates a synthetic dataset of small PNGs (a configurable share of them with
tag_txt sidecars built from a small fixed vocabulary, so one tag is guaranteed
to be highly frequent), indexes it through ``TagManagerService`` and reports
the scan throughput plus the three hot queries.  Fully offline: the tag
database is a stub answering "general" for every tag and the dictionaries are
empty stand-ins, so no classify snapshot or translation resource is needed.

The full scan is followed by an incremental rescan over the unchanged dataset,
so the two timings show how much the mtime-based skip saves on a refresh.
"""

from __future__ import annotations

import argparse
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Fixed vocabulary: tags are sampled from it per sidecar, so the top tag lands
# on roughly one sidecar in three and the include-filter has real work to do.
VOCAB = (
    "solo",
    "long_hair",
    "blue_eyes",
    "smile",
    "outdoors",
    "forest",
    "wolf",
    "day",
    "standing",
    "simple_background",
    "white_background",
    "looking_at_viewer",
)

# One deterministic dataset per vocabulary/version, so runs are comparable.
_SEED = 20260909


class _StubTagDatabase:
    """Offline stand-in: every tag resolves to 'general' without resources."""

    def is_loaded(self, profile: str) -> bool:
        return False

    def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
        return None

    def lookup(self, profile: str, tag: str) -> None:
        return None

    def autocomplete(self, profile: str, query: str, *, limit: int = 20) -> list[dict[str, str]]:
        return []

    def available_profiles(self) -> dict[str, list[str]]:
        return {}


class _StubThumbnails:
    """Never called by this benchmark; the service requires the interface."""

    def ensure_thumbnail(self, source: Path, *, size: int, mtime: float) -> Path:
        return source.with_suffix(".thumb.jpg")


def _generate_dataset(directory: Path, count: int, sidecar_ratio: float) -> int:
    """Write ``count`` 8x8 PNGs, tagging ``sidecar_ratio`` of them; return the count."""

    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(_SEED)
    sidecars = 0
    for index in range(count):
        name = f"image_{index:05d}.png"
        color = (index % 255, (index * 7) % 255, (index * 13) % 255)
        Image.new("RGB", (8, 8), color=color).save(directory / name)
        if rng.random() < sidecar_ratio:
            tags = rng.sample(VOCAB, rng.randint(3, 6))
            (directory / name.replace(".png", ".txt")).write_text(
                ", ".join(tags) + "\n", encoding="utf-8"
            )
            sidecars += 1
    return sidecars


def _print_row(label: str, seconds: float, note: str) -> None:
    print(f"  {label:<38} {seconds:>9.3f} s   {note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=int, default=2000, help="number of images to generate")
    parser.add_argument(
        "--sidecar-ratio",
        type=float,
        default=0.5,
        help="probability that an image carries a tag_txt sidecar",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the generated dataset and database instead of deleting them",
    )
    args = parser.parse_args()
    if args.images <= 0:
        raise SystemExit("--images must be positive")
    if not 0.0 <= args.sidecar_ratio <= 1.0:
        raise SystemExit("--sidecar-ratio must be between 0.0 and 1.0")

    from tagger2.security import PathAllowlist
    from tagger2.tag_manager.contracts import CreateDatasetRequest, ImageFilter
    from tagger2.tag_manager.service import TagManagerService
    from tagger2.tag_manager.storage import TagManagerStore
    from tagger2.tag_manager.translations import TagTranslations

    workspace = Path(tempfile.mkdtemp(prefix="tag-manager-benchmark-"))
    dataset_dir = workspace / "dataset"
    try:
        print(f"generating {args.images} images in {dataset_dir} ...")
        started = time.perf_counter()
        sidecars = _generate_dataset(dataset_dir, args.images, args.sidecar_ratio)
        print(f"  generated {sidecars} sidecars in {time.perf_counter() - started:.3f} s")

        allowlist = PathAllowlist()
        allowlist.register(workspace, root_id="benchmark-root", kind="input", writable=False)
        store = TagManagerStore(workspace / "tag_manager.sqlite3")
        service = TagManagerService(
            store=store,
            allowlist=allowlist,
            thumbnails=_StubThumbnails(),
            tag_database=_StubTagDatabase(),
            # Empty offline dictionaries: lookups answer None without any
            # settings or shipped resources.
            translations=TagTranslations(workspace / "dicts", workspace / "user-dicts"),
        )
        session = service.create_session(
            CreateDatasetRequest(root_id="benchmark-root", relative_path="dataset", profile="e621")
        )
        session_id = str(session["id"])

        started = time.perf_counter()
        service.index_session(session_id)
        index_seconds = time.perf_counter() - started
        indexed = service.get_session(session_id)
        if indexed["status"] != "ready":
            raise SystemExit(f"indexing failed: {indexed.get('error')}")

        # Second scan over the unchanged dataset: the incremental path skips
        # every file whose image and sidecar mtimes match the stored rows.
        started = time.perf_counter()
        service.index_session(session_id)
        incremental_seconds = time.perf_counter() - started
        rescanned = service.get_session(session_id)
        if rescanned["status"] != "ready":
            raise SystemExit(f"incremental indexing failed: {rescanned.get('error')}")

        started = time.perf_counter()
        listing = service.list_images(session_id, limit=60)
        list_seconds = time.perf_counter() - started

        started = time.perf_counter()
        stats = service.tag_stats(session_id, limit=50)
        stats_seconds = time.perf_counter() - started

        # The single most frequent tag gives the include-filter realistic work.
        top_tag = str(stats[0]["tag"]) if stats else ""
        started = time.perf_counter()
        filtered = service.list_images(
            session_id, image_filter=ImageFilter(include_tags=[top_tag]), limit=60
        )
        filter_seconds = time.perf_counter() - started

        print()
        print("tag manager index benchmark")
        print(f"  images: {args.images} ({sidecars} with sidecars, top tag '{top_tag}')")
        print()
        _print_row(
            "index_session (scan + upsert)",
            index_seconds,
            f"{args.images / index_seconds:.0f} images/s",
        )
        _print_row(
            "index_session (incremental rescan)",
            incremental_seconds,
            f"{args.images / incremental_seconds:.0f} images/s, unchanged files skipped",
        )
        _print_row(
            "list_images (no filter, limit 60)",
            list_seconds,
            f"{listing['total']} total",
        )
        _print_row(
            f"list_images (include '{top_tag}')",
            filter_seconds,
            f"{filtered['total']} matches",
        )
        _print_row("tag_stats (limit 50)", stats_seconds, f"{len(stats)} tags")
        if args.keep:
            print(f"\nkept dataset and database in {workspace}")
    finally:
        if not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    main()
