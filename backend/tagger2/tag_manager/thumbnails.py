"""On-disk JPEG thumbnail cache for the tag manager image grid.

Thumbnails are keyed by (source path identity, mtime, size) so an edited file
automatically gets a fresh thumbnail while repeated grid renders hit the cache.
Generation is synchronous and blocking; the API layer runs it through
``asyncio.to_thread``. Two threads requesting the same missing thumbnail share
one per-cache-key lock, so the file is generated exactly once and the loser
returns the winner's result.
"""

from __future__ import annotations

import hashlib
import io
import threading
from pathlib import Path

from PIL import Image, ImageOps

from ..security import atomic_write_bytes, validate_image_bytes

MIN_THUMBNAIL_SIZE = 32
MAX_THUMBNAIL_SIZE = 512
JPEG_QUALITY = 82

# Decode budget applied before any pixel data is materialised; stricter than
# but consistent with the project's upload posture (``security.open_image_secure``).
MAX_SOURCE_PIXELS = 64_000_000

# Same white-alpha compositing as ``workflow.dataset_import.load_normalized_image``.
_ALPHA_BACKGROUND = "#FFFFFF"
_ALPHA_MODES = {"RGBA", "LA", "P"}


class ThumbnailError(RuntimeError):
    """Raised when a thumbnail cannot be generated or cached."""


class ThumbnailService:
    """On-disk JPEG thumbnail cache keyed by (source path identity, mtime, size)."""

    def __init__(self, cache_dir: Path, *, max_source_bytes: int = 64 * 1024 * 1024) -> None:
        if max_source_bytes <= 0:
            raise ValueError("max_source_bytes must be positive")
        self.cache_dir = Path(cache_dir)
        self.max_source_bytes = max_source_bytes
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def ensure_thumbnail(self, source: Path, *, size: int = 256, mtime: float) -> Path:
        """Return the cached thumbnail path, generating it if missing.

        Sync + blocking; the API layer runs it via ``asyncio.to_thread``.
        Raises :class:`ThumbnailError` when the source cannot be read or
        decoded; raises ``ValueError`` when ``size`` is out of bounds.
        """

        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError(f"thumbnail size must be an integer, found {size!r}")
        if not MIN_THUMBNAIL_SIZE <= size <= MAX_THUMBNAIL_SIZE:
            raise ValueError(
                f"thumbnail size must be between {MIN_THUMBNAIL_SIZE}"
                f" and {MAX_THUMBNAIL_SIZE}, found {size!r}"
            )
        source = Path(source)
        target = self.cache_dir / f"{self._cache_key(source, mtime, size)}.jpg"
        if self._cached_is_readable(target):
            return target
        with self._per_key_lock(target.name):
            # The losing thread waits above and then reuses the winner's file;
            # a corrupt leftover is regenerated exactly once here.
            if self._cached_is_readable(target):
                return target
            atomic_write_bytes(target, self._render(source, size))
        return target

    # -- internals ----------------------------------------------------------

    def _cache_key(self, source: Path, mtime: float, size: int) -> str:
        identity = f"{source.resolve(strict=False)}::{mtime}::{size}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _per_key_lock(self, name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._locks[name] = lock
            return lock

    @staticmethod
    def _cached_is_readable(target: Path) -> bool:
        """Whether a cached thumbnail exists and PIL can still decode it."""

        try:
            stat = target.stat()
        except OSError:
            return False
        if stat.st_size == 0:
            return False
        try:
            with Image.open(target) as image:
                image.verify()
        except Exception:
            return False
        return True

    def _render(self, source: Path, size: int) -> bytes:
        """Decode, normalize and encode one thumbnail as JPEG bytes."""

        try:
            stat = source.stat()
        except OSError as exc:
            raise ThumbnailError("source image could not be accessed") from exc
        if stat.st_size > self.max_source_bytes:
            raise ThumbnailError("source image exceeds the thumbnail size limit")
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise ThumbnailError("source image could not be read") from exc
        try:
            # Checks byte size, edge length and the pixel budget before the
            # decoder ever touches pixel data (decompression-bomb posture).
            validate_image_bytes(raw, max_bytes=self.max_source_bytes, max_pixels=MAX_SOURCE_PIXELS)
        except Exception as exc:
            raise ThumbnailError("source image is not a decodable image") from exc

        try:
            with Image.open(io.BytesIO(raw)) as opened:
                image = ImageOps.exif_transpose(opened)
                if image.mode in _ALPHA_MODES:
                    converted = image.convert("RGBA")
                    background = Image.new("RGBA", converted.size, _ALPHA_BACKGROUND)
                    image = Image.alpha_composite(background, converted)
                result = image.convert("RGB")
                result.load()
        except ThumbnailError:
            raise
        except Exception as exc:
            raise ThumbnailError("source image could not be decoded") from exc

        try:
            thumbnail = ImageOps.fit(result, (size, size), method=Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            thumbnail.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        except Exception as exc:
            raise ThumbnailError("thumbnail could not be encoded") from exc
        return buffer.getvalue()


__all__ = [
    "MAX_SOURCE_PIXELS",
    "MAX_THUMBNAIL_SIZE",
    "MIN_THUMBNAIL_SIZE",
    "ThumbnailError",
    "ThumbnailService",
]
