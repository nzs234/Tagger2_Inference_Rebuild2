"""Shared small utilities for hashing, timestamps, JSON encoding and OOM handling.

This module is deliberately dependency-light: importing it must not pull in
torch, PIL, httpx or any other heavy dependency.  Helpers that touch optional
heavy dependencies (``empty_cuda_cache`` imports torch) do so lazily inside the
function body so CPU-only and lightweight importers pay no import-time cost.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """Return the current UTC time as millisecond-precision ISO 8601 text."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    """Return a stable JSON encoding for hashing and persistence.

    Non-JSON-serializable values fall back to ``str`` so configuration payloads
    never fail to encode.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(value: bytes) -> str:
    """Return the hex SHA-256 digest of ``value``."""
    return hashlib.sha256(value).hexdigest()


def is_out_of_memory(exc: BaseException) -> bool:
    """Return True when ``exc`` looks like a device out-of-memory failure."""
    return "out of memory" in str(exc).casefold()


def empty_cuda_cache() -> None:
    """Release cached CUDA memory when torch exposes a usable device.

    Never raises: torch may be absent (CPU-only installs) or its CUDA backend
    may be in a broken state, and cache release is always best-effort.
    """
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


__all__ = [
    "canonical_json",
    "empty_cuda_cache",
    "is_out_of_memory",
    "sha256_bytes",
    "utc_now",
]
