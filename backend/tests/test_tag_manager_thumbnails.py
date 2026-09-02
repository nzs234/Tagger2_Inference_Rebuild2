"""Tests for the tag manager on-disk JPEG thumbnail cache."""

import threading
from pathlib import Path

import pytest
from PIL import Image

from tagger2.tag_manager.thumbnails import ThumbnailError, ThumbnailService


def _write_png(
    path: Path,
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (64, 48),
    color: tuple[int, ...] = (180, 40, 60),
) -> Path:
    if mode == "RGBA":
        image = Image.new("RGBA", size, (0, 0, 0, 0))
    else:
        image = Image.new(mode, size, color)
    image.save(path, format="PNG")
    return path


def test_creates_jpeg_thumbnail(tmp_path: Path):
    """A generated thumbnail is a square JPEG under the cache directory."""

    cache_dir = tmp_path / "thumbs"
    service = ThumbnailService(cache_dir)
    source = _write_png(tmp_path / "picture.png")

    thumbnail = service.ensure_thumbnail(source, size=32, mtime=source.stat().st_mtime)

    assert thumbnail.parent == cache_dir
    assert thumbnail.suffix == ".jpg"
    with Image.open(thumbnail) as image:
        assert image.format == "JPEG"
        assert image.size == (32, 32)


def test_second_call_returns_cached_file_untouched(tmp_path: Path):
    """A second request for the same key reuses the file without rewriting."""

    service = ThumbnailService(tmp_path / "thumbs")
    source = _write_png(tmp_path / "picture.png")
    mtime = source.stat().st_mtime
    first = service.ensure_thumbnail(source, size=32, mtime=mtime)
    before = first.stat()

    second = service.ensure_thumbnail(source, size=32, mtime=mtime)

    assert second == first
    after = first.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_size == before.st_size


def test_mtime_change_produces_new_cache_file(tmp_path: Path):
    """A changed source mtime keys a fresh thumbnail instead of the stale one."""

    service = ThumbnailService(tmp_path / "thumbs")
    source = _write_png(tmp_path / "picture.png")

    stale = service.ensure_thumbnail(source, size=32, mtime=100.0)
    fresh = service.ensure_thumbnail(source, size=32, mtime=200.0)

    assert stale != fresh
    assert stale.exists() and fresh.exists()


def test_size_bounds_are_rejected(tmp_path: Path):
    """Sizes outside 32..512 are refused before any file access."""

    service = ThumbnailService(tmp_path / "thumbs")
    source = _write_png(tmp_path / "picture.png")

    for size in (31, 513, 0):
        with pytest.raises(ValueError):
            service.ensure_thumbnail(source, size=size, mtime=1.0)


def test_oversized_source_is_rejected(tmp_path: Path):
    """A source larger than the configured byte cap never reaches the decoder."""

    service = ThumbnailService(tmp_path / "thumbs", max_source_bytes=256)
    source = tmp_path / "big.png"
    source.write_bytes(b"\x00" * 4096)

    with pytest.raises(ThumbnailError) as excinfo:
        service.ensure_thumbnail(source, size=32, mtime=1.0)
    # Error messages stay path-free.
    assert "big" not in str(excinfo.value)


def test_undecodable_source_is_rejected(tmp_path: Path):
    """Bytes under the cap that are not an image still fail cleanly."""

    service = ThumbnailService(tmp_path / "thumbs", max_source_bytes=64 * 1024 * 1024)
    source = tmp_path / "garbage.png"
    source.write_bytes(b"this is not an image at all" * 4)

    with pytest.raises(ThumbnailError):
        service.ensure_thumbnail(source, size=32, mtime=1.0)


def test_missing_source_is_rejected(tmp_path: Path):
    """A source that does not exist raises ThumbnailError, not OSError."""

    service = ThumbnailService(tmp_path / "thumbs")

    with pytest.raises(ThumbnailError):
        service.ensure_thumbnail(tmp_path / "missing.png", size=32, mtime=1.0)


def test_corrupt_cached_file_is_regenerated(tmp_path: Path):
    """A zero-byte or undecodable cached file is rebuilt on the next call."""

    service = ThumbnailService(tmp_path / "thumbs")
    source = _write_png(tmp_path / "picture.png")
    thumbnail = service.ensure_thumbnail(source, size=32, mtime=1.0)

    thumbnail.write_bytes(b"")
    regenerated = service.ensure_thumbnail(source, size=32, mtime=1.0)
    assert regenerated == thumbnail
    with Image.open(regenerated) as image:
        assert image.format == "JPEG"

    thumbnail.write_bytes(b"clearly not a jpeg")
    regenerated = service.ensure_thumbnail(source, size=32, mtime=1.0)
    with Image.open(regenerated) as image:
        assert image.format == "JPEG"
        assert image.size == (32, 32)


def test_transparent_pixels_composite_onto_white(tmp_path: Path):
    """Alpha is composited like the dataset importer, not dropped."""

    service = ThumbnailService(tmp_path / "thumbs")
    source = _write_png(tmp_path / "transparent.png", mode="RGBA")

    thumbnail = service.ensure_thumbnail(source, size=32, mtime=1.0)
    with Image.open(thumbnail) as image:
        red, green, blue = image.convert("RGB").getpixel((0, 0))[:3]
    assert red >= 250 and green >= 250 and blue >= 250


def test_concurrent_requests_generate_once(tmp_path: Path):
    """Two threads racing on one missing thumbnail produce a single file."""

    service = ThumbnailService(tmp_path / "thumbs")
    source = _write_png(tmp_path / "picture.png")
    barrier = threading.Barrier(2)
    results: list[Path] = []
    failures: list[Exception] = []
    guard = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        try:
            thumbnail = service.ensure_thumbnail(source, size=32, mtime=1.0)
            with guard:
                results.append(thumbnail)
        except Exception as exc:  # pragma: no cover - collected below
            with guard:
                failures.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert len(list((tmp_path / "thumbs").glob("*.jpg"))) == 1
