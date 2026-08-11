"""Safe image preparation for vision provider requests."""

from __future__ import annotations

import base64
import hashlib
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_DIMENSION = 4096


@dataclass(frozen=True, slots=True)
class PreparedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    sha256: str

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{self.base64}"


def _read_source(source: str | Path | bytes | bytearray | BinaryIO, *, max_source_bytes: int) -> bytes:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > max_source_bytes:
            raise ValueError(f"image exceeds source limit ({max_source_bytes} bytes)")
        data = path.read_bytes()
        return data
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if len(data) > max_source_bytes:
            raise ValueError(f"image exceeds source limit ({max_source_bytes} bytes)")
        return data
    if hasattr(source, "read"):
        data = source.read(max_source_bytes + 1)
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("image stream must return bytes")
        data = bytes(data)
        if len(data) > max_source_bytes:
            raise ValueError(f"image exceeds source limit ({max_source_bytes} bytes)")
        return data
    raise TypeError("image must be a path, bytes, or binary stream")


def prepare_image(
    source: str | Path | bytes | bytearray | BinaryIO,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    max_dimension: int = DEFAULT_MAX_DIMENSION,
    jpeg_quality: int = 90,
) -> PreparedImage:
    """Decode, orient, RGB-convert and boundedly compress an image.

    Re-encoding all images avoids carrying EXIF or an untrusted ICC profile to
    a remote provider.  Transparency is composited onto white before JPEG
    encoding.  The original bytes are never modified.
    """

    if max_bytes <= 0 or max_source_bytes <= 0 or max_pixels <= 0 or max_dimension <= 0:
        raise ValueError("image limits must be positive")
    raw = _read_source(source, max_source_bytes=max_source_bytes)
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            # Accessing size before loading lets us reject decompression bombs
            # without allocating the full pixel buffer.
            width, height = opened.size
            if width <= 0 or height <= 0 or width * height > max_pixels:
                raise ValueError("image exceeds pixel limit")
            image = ImageOps.exif_transpose(opened).convert("RGBA")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"unable to decode image: {exc}") from exc

    width, height = image.size
    scale = min(1.0, max_dimension / max(width, height))
    if scale < 1.0:
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.LANCZOS)
    # Alpha is composited explicitly, rather than relying on Pillow's implicit
    # conversion which can produce black backgrounds for transparent PNGs.
    background = Image.new("RGB", image.size, (255, 255, 255))
    background.paste(image, mask=image.getchannel("A"))

    quality = max(35, min(95, int(jpeg_quality)))
    encoded: bytes = b""
    for _resize_attempt in range(7):
        for current_quality in (quality, max(35, quality - 10), 35):
            buffer = io.BytesIO()
            background.save(buffer, format="JPEG", quality=current_quality, optimize=True, progressive=True)
            encoded = buffer.getvalue()
            if len(encoded) <= max_bytes:
                break
        if len(encoded) <= max_bytes:
            break
        if min(background.size) <= 64:
            break
        ratio = max(0.5, min(0.9, math.sqrt(max_bytes / len(encoded)) * 0.9))
        next_size = (max(64, round(background.width * ratio)), max(64, round(background.height * ratio)))
        if next_size == background.size:
            break
        background = background.resize(next_size, Image.Resampling.LANCZOS)
    if len(encoded) > max_bytes:
        raise ValueError("image cannot be compressed below upload limit")
    return PreparedImage(
        data=encoded,
        mime_type="image/jpeg",
        width=background.width,
        height=background.height,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def encode_image(source: str | Path | bytes | bytearray | BinaryIO, **kwargs: object) -> tuple[str, str]:
    """Compatibility helper returning ``(base64, mime_type)``."""

    prepared = prepare_image(source, **kwargs)  # type: ignore[arg-type]
    return prepared.base64, prepared.mime_type


__all__ = ["PreparedImage", "encode_image", "prepare_image"]
