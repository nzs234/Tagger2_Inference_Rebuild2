"""Validate a hashed requirements lock and, optionally, the active environment."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import re
from pathlib import Path


PIN_PATTERN = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==([^\s\\;]+)"
)
LOCAL_REFERENCE_PATTERNS = (
    re.compile(r"(?i)\bfile://"),
    re.compile(r"(?im)^\s*(?:-e|--editable)(?:\s|=)"),
    re.compile(r"(?im)^\s*[A-Za-z]:[\\/]"),
    re.compile(r"(?i)@\s*(?:file:|[A-Za-z]:[\\/]|\\\\)"),
)


class LockValidationError(ValueError):
    pass


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def parse_lock(path: Path) -> dict[str, tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    for pattern in LOCAL_REFERENCE_PATTERNS:
        if pattern.search(text):
            raise LockValidationError("lock contains an editable or local filesystem reference")

    lines = text.splitlines()
    pins: dict[str, tuple[str, str]] = {}
    pin_indexes: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "--")):
            continue
        match = PIN_PATTERN.match(stripped)
        if match is None:
            if line == line.lstrip() and not stripped.startswith("\\"):
                raise LockValidationError(f"line {index + 1} is not an exact pin: {stripped}")
            continue
        package, version = match.groups()
        key = canonical_name(package)
        if key in pins:
            raise LockValidationError(f"duplicate package pin: {package}")
        pins[key] = (package, version)
        pin_indexes.append((index, key))

    if not pins:
        raise LockValidationError("lock contains no package pins")
    for position, (start, key) in enumerate(pin_indexes):
        stop = pin_indexes[position + 1][0] if position + 1 < len(pin_indexes) else len(lines)
        block = "\n".join(lines[start:stop])
        if "--hash=sha256:" not in block:
            raise LockValidationError(f"package {pins[key][0]} has no SHA-256 hash")
    return pins


def verify_installed(pins: dict[str, tuple[str, str]]) -> None:
    mismatches: list[str] = []
    for package, expected in pins.values():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            mismatches.append(f"{package}: missing (expected {expected})")
            continue
        if actual.casefold() != expected.casefold():
            mismatches.append(f"{package}: installed {actual}, expected {expected}")
    if mismatches:
        raise LockValidationError("environment does not match lock:\n  " + "\n  ".join(mismatches))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--check-installed", action="store_true")
    parser.add_argument("--module", action="append", default=[])
    args = parser.parse_args()

    lock = args.lock.expanduser().resolve(strict=True)
    pins = parse_lock(lock)
    if args.check_installed:
        verify_installed(pins)
    for module in args.module:
        importlib.import_module(module)
    print(f"lock verified: {lock.name} ({len(pins)} pinned packages)")


if __name__ == "__main__":
    main()
