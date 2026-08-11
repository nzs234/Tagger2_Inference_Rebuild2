"""Dataset import: scan a source tree into an immutable sample manifest.

Only the source project's compatible image set is accepted. Each image is paired
with its optional ``.txt`` / ``.json`` sidecars, and the annotation format is
classified strictly: a corrupt document raises a blocking issue instead of
silently falling back to another path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from PIL import Image, ImageOps

from .raw_e621 import RawE621JsonError, parse_raw_e621_annotation

# Frozen to the source project's compatible set; GIF/TIFF/AVIF are intentionally excluded.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".bmp": "bmp",
}
ALPHA_BACKGROUND = "#FFFFFF"

AnnotationKind = Literal[
    "none",
    "tag_txt",
    "nl_txt",
    "standard_json",
    "raw_e621_json",
]


class ImportError_(ValueError):
    """Raised when a dataset entry cannot be imported."""


@dataclass(frozen=True)
class ImportedSample:
    """One immutable manifest row."""

    sample_id: int
    relative_image_path: str
    annotation_key: str
    image_format: str
    annotation_kind: AnnotationKind
    txt_present: bool
    json_present: bool
    tags: tuple[str, ...] = ()
    nl: str = ""
    artist: str = ""
    character: str = ""
    skip_caption: bool = False


@dataclass(frozen=True)
class ImportIssue:
    """A blocking or informational problem found during import."""

    relative_image_path: str
    code: str
    message: str
    severity: str = "error"
    blocking: bool = True


@dataclass(frozen=True)
class ImportResult:
    samples: tuple[ImportedSample, ...]
    issues: tuple[ImportIssue, ...]
    skipped_files: tuple[str, ...]


def _iter_images(root: Path, recursive: bool) -> Iterator[Path]:
    if recursive:
        for current, directories, files in os.walk(root):
            directories.sort()
            for name in sorted(files):
                yield Path(current) / name
    else:
        for entry in sorted(root.iterdir()):
            if entry.is_file():
                yield entry


def probe_image(path: Path) -> str:
    """Return the normalized image format, rejecting multi-frame images.

    EXIF transposition and white alpha compositing are applied by the caption
    stage; this only validates decodability and frame count so a bad file is
    reported at import time.
    """

    try:
        with Image.open(path) as image:
            fmt = (image.format or "").lower()
            if fmt == "mpo":
                fmt = "jpeg"
            if fmt not in {"jpeg", "png", "webp", "bmp"}:
                raise ImportError_(f"unsupported image format: {image.format!r}")
            if getattr(image, "n_frames", 1) > 1:
                raise ImportError_("multi-frame images are rejected")
            image.verify()
            return fmt
    except ImportError_:
        raise
    except Exception as exc:
        raise ImportError_(f"image cannot be decoded: {exc}") from exc


def load_normalized_image(path: Path) -> Image.Image:
    """Open an image with EXIF transpose and white alpha compositing applied."""

    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened)
        if image.mode in {"RGBA", "LA", "P"}:
            converted = image.convert("RGBA")
            background = Image.new("RGBA", converted.size, ALPHA_BACKGROUND)
            image = Image.alpha_composite(background, converted)
        result = image.convert("RGB")
        result.load()
        return result


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ImportError_("annotation TXT contains NUL")
    if len(raw) > 16 * 1024:
        raise ImportError_("annotation TXT exceeds 16 KiB")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportError_("annotation TXT is not UTF-8") from exc


def _parse_tag_txt(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    seen: set[str] = set()
    for part in text.replace("\n", ",").split(","):
        tag = part.strip()
        if not tag:
            continue
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tuple(tags)


def classify_annotation(
    txt_path: Path,
    json_path: Path,
    *,
    input_txt_mode: str,
) -> tuple[AnnotationKind, dict[str, object]]:
    """Determine the annotation format for one sample.

    Precedence matches the source project: a raw e621 grouped JSON wins and
    skips the tagger, a standard JSON is reused, otherwise a non-blank TXT is
    read as tags or as NL depending on the configured mode.
    """

    import json

    payload: dict[str, object] = {}

    if json_path.is_file():
        raw = json_path.read_bytes()
        try:
            annotation = parse_raw_e621_annotation(raw)
        except RawE621JsonError as exc:
            raise ImportError_(f"raw e621 JSON is invalid: {exc}") from exc
        if annotation is not None:
            payload["artist"] = annotation.artist
            payload["character"] = annotation.character
            payload["tags"] = annotation.classify_tags
            return "raw_e621_json", payload
        if raw.strip():
            try:
                document = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImportError_(f"annotation JSON is invalid: {exc}") from exc
            if not isinstance(document, dict):
                raise ImportError_("annotation JSON root is not an object")
            payload["document"] = document
            return "standard_json", payload

    if txt_path.is_file():
        text = _read_text(txt_path)
        if text.strip():
            if input_txt_mode == "nl":
                payload["nl"] = " ".join(text.split())
                return "nl_txt", payload
            payload["tags"] = _parse_tag_txt(text)
            return "tag_txt", payload

    return "none", payload


def import_dataset(
    source_root: Path,
    *,
    recursive: bool = False,
    input_txt_mode: str = "tag",
) -> ImportResult:
    """Scan ``source_root`` into an ordered, immutable sample manifest."""

    if input_txt_mode not in {"tag", "nl"}:
        raise ValueError("input_txt_mode must be 'tag' or 'nl'")

    samples: list[ImportedSample] = []
    issues: list[ImportIssue] = []
    skipped: list[str] = []
    next_id = 0

    for path in _iter_images(source_root, recursive):
        suffix = path.suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            if suffix not in {".txt", ".json"}:
                skipped.append(path.relative_to(source_root).as_posix())
            continue

        relative = path.relative_to(source_root).as_posix()
        annotation_key = relative[: -len(path.suffix)]

        try:
            image_format = probe_image(path)
        except ImportError_ as exc:
            issues.append(
                ImportIssue(
                    relative_image_path=relative,
                    code="image_invalid",
                    message=str(exc),
                )
            )
            continue

        txt_path = path.with_suffix(".txt")
        json_path = path.with_suffix(".json")

        try:
            kind, payload = classify_annotation(
                txt_path, json_path, input_txt_mode=input_txt_mode
            )
        except ImportError_ as exc:
            issues.append(
                ImportIssue(
                    relative_image_path=relative,
                    code="annotation_invalid",
                    message=str(exc),
                )
            )
            continue

        samples.append(
            ImportedSample(
                sample_id=next_id,
                relative_image_path=relative,
                annotation_key=annotation_key,
                image_format=image_format,
                annotation_kind=kind,
                txt_present=txt_path.is_file(),
                json_present=json_path.is_file(),
                tags=tuple(payload.get("tags", ())),  # type: ignore[arg-type]
                nl=str(payload.get("nl", "")),
                artist=str(payload.get("artist", "")),
                character=str(payload.get("character", "")),
                # Raw e621 JSON is authoritative, so the tagger is skipped.
                skip_caption=kind in {"raw_e621_json", "tag_txt"},
            )
        )
        next_id += 1

    return ImportResult(
        samples=tuple(samples),
        issues=tuple(issues),
        skipped_files=tuple(skipped),
    )


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "AnnotationKind",
    "ImportError_",
    "ImportedSample",
    "ImportIssue",
    "ImportResult",
    "classify_annotation",
    "import_dataset",
    "load_normalized_image",
    "probe_image",
]
