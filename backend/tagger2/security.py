"""Filesystem, image and network safety primitives.

The web layer should call these helpers before handing a path or URL to a
worker.  They are intentionally independent of FastAPI so batch workers and
CLI smoke tests receive the same protection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO, Iterable, Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from PIL import Image, ImageOps


class SecurityError(ValueError):
    """Raised when an untrusted path, URL, file or token is rejected."""

    code = "security_error"
    retryable = False

    def as_error(self, request_id: str) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "request_id": request_id,
            "retryable": self.retryable,
        }


class PathNotAllowedError(SecurityError):
    code = "path_not_allowed"


class UploadValidationError(SecurityError):
    code = "invalid_upload"


@dataclass(frozen=True, slots=True)
class PathRoot:
    root_id: str
    path: Path
    label: str
    kind: str
    writable: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "label": self.label,
            "kind": self.kind,
            "writable": self.writable,
        }


# Generated IDs are long opaque tokens, but administrators may deliberately
# choose a short stable ID in a local configuration file (``models``/``input``).
_ROOT_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def opaque_id(value: str | Path, *, prefix: str = "root") -> str:
    """Create a stable identifier that does not reveal ``value``."""

    canonical = str(Path(value).resolve(strict=False)).casefold().encode("utf-8")
    digest = hashlib.blake2b(canonical, digest_size=18).digest()
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{prefix}_{token}"


def _reject_relative_path(relative: str | os.PathLike[str]) -> str:
    text = os.fspath(relative)
    if "\x00" in text:
        raise PathNotAllowedError("path contains NUL")
    # Check both flavours because a Windows client can submit backslashes to a
    # POSIX worker and vice versa.
    if PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute():
        raise PathNotAllowedError("absolute paths are not accepted")
    normal = text.replace("\\", "/")
    parts = [part for part in normal.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise PathNotAllowedError("parent path traversal is not accepted")
    return "/".join(parts)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class PathAllowlist:
    """Registry of server-owned path roots.

    Clients refer only to ``root_id`` plus a relative path.  Every resolution
    follows symlinks and checks the final canonical path, including the parent
    of a new file, so a symlink cannot escape the registered root.
    """

    def __init__(self, roots: Iterable[PathRoot | Mapping[str, Any]] | None = None):
        self._roots: dict[str, PathRoot] = {}
        self._lock = __import__("threading").RLock()
        for root in roots or ():
            if isinstance(root, PathRoot):
                self.register(
                    root.path,
                    kind=root.kind,
                    root_id=root.root_id,
                    label=root.label,
                    writable=root.writable,
                )
            else:
                self.register(
                    root["path"],
                    kind=str(root.get("kind", "input")),
                    root_id=root.get("root_id"),
                    label=root.get("label"),
                    writable=bool(root.get("writable", False)),
                )

    def register(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str = "input",
        root_id: str | None = None,
        label: str | None = None,
        writable: bool = False,
        create: bool = False,
    ) -> PathRoot:
        if kind not in {"input", "output", "model", "upload"}:
            raise SecurityError(f"unsupported root kind: {kind}")
        raw = Path(path).expanduser()
        if create:
            raw.mkdir(parents=True, exist_ok=True)
        canonical = raw.resolve(strict=False)
        if not canonical.exists() or not canonical.is_dir():
            raise PathNotAllowedError(f"root directory does not exist: {canonical}")
        identifier = root_id or opaque_id(canonical, prefix="root")
        if not _ROOT_ID_RE.fullmatch(identifier):
            raise SecurityError("invalid root id")
        item = PathRoot(
            root_id=identifier,
            path=canonical,
            label=label or canonical.name or str(canonical),
            kind=kind,
            writable=bool(writable),
        )
        with self._lock:
            existing = self._roots.get(identifier)
            if existing is not None and existing.path != canonical:
                raise SecurityError("root id is already registered")
            self._roots[identifier] = item
        return item

    register_root = register

    def unregister(self, root_id: str) -> None:
        with self._lock:
            self._roots.pop(root_id, None)

    def get(self, root_id: str) -> PathRoot:
        with self._lock:
            root = self._roots.get(root_id)
        if root is None:
            raise PathNotAllowedError("unknown path root")
        return root

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            return [root.public() for root in self._roots.values()]

    def find_root_for_path(
        self,
        path: str | os.PathLike[str],
        *,
        kind: str | None = None,
        writable: bool | None = None,
    ) -> tuple[PathRoot, str] | None:
        """Return the most specific registered root containing ``path``.

        Manual-path workflow clients use this at the boundary where an
        absolute user-entered directory is converted to the internal
        ``root_id`` + relative-path contract.  The returned relative path is
        always POSIX-normalised and is never a filesystem path sent back to a
        client.
        """

        candidate = Path(path).expanduser().resolve(strict=False)
        with self._lock:
            roots = list(self._roots.values())
        matches: list[tuple[int, PathRoot, str]] = []
        for root in roots:
            if kind is not None and root.kind != kind:
                continue
            if writable is not None and root.writable != writable:
                continue
            if not _within(candidate, root.path):
                continue
            relative = candidate.relative_to(root.path).as_posix()
            matches.append((len(root.path.parts), root, relative))
        if not matches:
            return None
        _depth, root, relative = max(matches, key=lambda item: item[0])
        return root, relative

    def resolve(
        self,
        root_id: str,
        relative: str | os.PathLike[str] = "",
        *,
        must_exist: bool = False,
        for_write: bool = False,
        expect: str | None = None,
    ) -> Path:
        root = self.get(root_id)
        if for_write and not root.writable:
            raise PathNotAllowedError("root is read-only")
        clean = _reject_relative_path(relative)
        candidate = root.path if not clean else root.path.joinpath(*clean.split("/"))
        # resolve(strict=False) resolves existing symlinks while permitting a
        # new final file.  Check the nearest existing parent in either case.
        resolved = candidate.resolve(strict=False)
        check = resolved if resolved.exists() else resolved.parent
        if not _within(check, root.path):
            raise PathNotAllowedError("path escapes registered root")
        if must_exist and not resolved.exists():
            raise PathNotAllowedError("path does not exist")
        if expect == "file" and resolved.exists() and not resolved.is_file():
            raise PathNotAllowedError("path is not a file")
        if expect == "dir" and resolved.exists() and not resolved.is_dir():
            raise PathNotAllowedError("path is not a directory")
        return resolved

    resolve_path = resolve

    def relative_path(self, root_id: str, path: str | os.PathLike[str]) -> str:
        root = self.get(root_id)
        candidate = Path(path).resolve(strict=False)
        if not _within(candidate, root.path):
            raise PathNotAllowedError("path escapes registered root")
        return candidate.relative_to(root.path).as_posix()

    def assert_allowed(
        self,
        path: str | os.PathLike[str],
        *,
        root_id: str | None = None,
        for_write: bool = False,
        expect: str | None = None,
    ) -> Path:
        candidate = Path(path).resolve(strict=False)
        roots = [self.get(root_id)] if root_id else list(self._roots.values())
        for root in roots:
            check = candidate if candidate.exists() else candidate.parent
            if _within(check, root.path):
                if for_write and not root.writable:
                    continue
                if expect == "file" and candidate.exists() and not candidate.is_file():
                    continue
                if expect == "dir" and candidate.exists() and not candidate.is_dir():
                    continue
                return candidate
        raise PathNotAllowedError("path is not inside an allowed root")


def sanitize_filename(filename: str, *, fallback: str = "upload") -> str:
    """Return a safe basename suitable for an upload/artifact file."""

    if not isinstance(filename, str):
        raise SecurityError("filename must be text")
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        name = fallback
    if "\x00" in name or any(ord(char) < 32 for char in name):
        raise SecurityError("filename contains control characters")
    stem = name.rsplit(".", 1)[0].rstrip(" .")
    if stem.upper() in _WINDOWS_RESERVED:
        raise SecurityError("reserved filename")
    if any(char in name for char in '<>:"/\\|?*'):
        raise SecurityError("filename contains unsafe characters")
    if len(name) > 240:
        suffix = Path(name).suffix[:32]
        name = name[: 240 - len(suffix)] + suffix
    return name or fallback


def safe_resolve(
    allowlist: PathAllowlist,
    root_id: str,
    relative: str | os.PathLike[str] = "",
    *,
    must_exist: bool = False,
    for_write: bool = False,
    expect: str | None = None,
) -> Path:
    return allowlist.resolve(
        root_id,
        relative,
        must_exist=must_exist,
        for_write=for_write,
        expect=expect,
    )


def validate_provider_url(
    url: str,
    *,
    allow_local: bool = False,
    resolve_dns: bool = False,
) -> str:
    """Validate and canonicalise an HTTP provider URL.

    Local/private destinations are blocked unless explicitly enabled.  DNS
    resolution is opt-in so merely configuring a URL never performs network IO.
    """

    if not isinstance(url, str) or len(url) > 2048:
        raise SecurityError("invalid provider URL")
    parsed = urlsplit(url.strip())
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise SecurityError("provider URL must use http or https")
    if parsed.username or parsed.password:
        raise SecurityError("provider URL must not contain credentials")
    if parsed.query:
        # Credentials in query strings are routinely copied into browser,
        # proxy, and server logs.  Providers receive secrets through the
        # credential store and request headers instead.
        raise SecurityError("provider URL query parameters are not allowed")
    host = parsed.hostname
    if not host:
        raise SecurityError("provider URL has no hostname")
    host = host.rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SecurityError("invalid provider URL port") from exc
    local = host.casefold() in {"localhost", "localhost.localdomain"} or host.casefold().endswith(
        (".local", ".lan", ".internal")
    )
    try:
        address = ipaddress.ip_address(host)
        local = local or (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_unspecified
        )
    except ValueError:
        if host.replace(".", "").isdigit():
            raise SecurityError("ambiguous numeric provider hostname is not accepted")
        if resolve_dns:
            try:
                addresses = {
                    info[4][0]
                    for info in socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80))
                }
            except OSError as exc:
                raise SecurityError("provider hostname could not be resolved") from exc
            for value in addresses:
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if address.is_private or address.is_loopback or address.is_link_local:
                    local = True
                    break
    if local and not allow_local:
        raise SecurityError("local provider URLs require explicit enablement")
    sensitive_query_keys = {
        "key",
        "api_key",
        "apikey",
        "access_token",
        "token",
        "authorization",
    }
    if any(key.casefold() in sensitive_query_keys for key, _ in parse_qsl(parsed.query)):
        raise SecurityError("provider credentials must not be placed in the URL")
    # Drop fragments (they are never sent to a server) and normalise whitespace
    # without changing path/query semantics used by provider APIs.
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def constant_time_equal(expected: str | bytes, supplied: str | bytes) -> bool:
    if isinstance(expected, str):
        expected = expected.encode("utf-8")
    if isinstance(supplied, str):
        supplied = supplied.encode("utf-8")
    return hmac.compare_digest(expected, supplied)


def validate_bearer_token(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    value = supplied.strip()
    if value.casefold().startswith("bearer "):
        value = value[7:].strip()
    return constant_time_equal(expected, value)


def validate_image_bytes(
    data: bytes | bytearray | memoryview,
    *,
    max_bytes: int = 32 * 1024 * 1024,
    max_pixels: int = 80_000_000,
    max_edge: int = 16_384,
) -> dict[str, Any]:
    """Validate image bytes without retaining a decoder object.

    ``Image.verify`` catches truncated/malformed files and the explicit pixel
    checks protect against decompression bombs before a worker decodes them.
    """

    raw = bytes(data)
    if len(raw) > max_bytes:
        raise UploadValidationError("image exceeds upload size limit")
    if not raw:
        raise UploadValidationError("empty image")
    try:
        with Image.open(__import__("io").BytesIO(raw)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width > max_edge or height > max_edge:
                raise UploadValidationError("image dimensions exceed limit")
            if width * height > max_pixels:
                raise UploadValidationError("image pixel count exceeds limit")
            image.verify()
            return {
                "format": image.format or "unknown",
                "mime": Image.MIME.get(image.format or "", "application/octet-stream"),
                "width": width,
                "height": height,
                "bytes": len(raw),
            }
    except UploadValidationError:
        raise
    except Exception as exc:
        raise UploadValidationError("invalid image data") from exc


def open_image_secure(
    source: str | os.PathLike[str] | bytes | BinaryIO,
    *,
    max_bytes: int = 32 * 1024 * 1024,
    max_pixels: int = 80_000_000,
    max_edge: int = 16_384,
) -> Image.Image:
    """Open, orient and RGB-normalise an image after validating its bounds."""

    import io

    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if path.stat().st_size > max_bytes:
            raise UploadValidationError("image exceeds upload size limit")
        raw = path.read_bytes()
    elif hasattr(source, "read"):
        raw = source.read()
    else:
        raw = bytes(source)
    validate_image_bytes(raw, max_bytes=max_bytes, max_pixels=max_pixels, max_edge=max_edge)
    try:
        opened = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.load()
        opened.close()
        return image
    except Exception as exc:
        raise UploadValidationError("unable to decode image") from exc


def atomic_write_bytes(
    target: str | os.PathLike[str], data: bytes, *, mode: int = 0o600
) -> Path:
    """Write bytes durably and replace the target in one atomic operation."""

    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temp_path = Path(temp_name)
    try:
        os.chmod(temp_path, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, destination)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return destination


def atomic_write_json(
    target: str | os.PathLike[str], value: Any, *, ensure_ascii: bool = False, indent: int = 2
) -> Path:
    return atomic_write_bytes(
        target,
        json.dumps(value, ensure_ascii=ensure_ascii, indent=indent, sort_keys=False).encode("utf-8"),
    )


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def redact_secret(value: str | None, *, keep: int = 4) -> str | None:
    if value is None:
        return None
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * max(4, len(value) - keep) + value[-keep:]


__all__ = [
    "SecurityError",
    "PathNotAllowedError",
    "UploadValidationError",
    "PathRoot",
    "opaque_id",
    "PathAllowlist",
    "sanitize_filename",
    "safe_resolve",
    "validate_provider_url",
    "constant_time_equal",
    "validate_bearer_token",
    "validate_image_bytes",
    "open_image_secure",
    "atomic_write_bytes",
    "atomic_write_json",
    "sha256_file",
    "redact_secret",
]
