"""Keep this project's backend ahead of stale embedded-Python source paths."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def normalized(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value)))


def is_backend_source_path(value: str) -> bool:
    """Identify absolute backend source entries left by another checkout."""

    stripped = value.strip()
    return bool(stripped) and Path(stripped).name.casefold() == "backend"


def update_pth(pth_path: Path, backend_path: Path) -> None:
    target = str(backend_path.resolve(strict=True))
    target_key = normalized(target)
    lines = pth_path.read_text(encoding="utf-8-sig").splitlines()
    filtered = [
        line
        for line in lines
        if normalized(line.strip()) != target_key and not is_backend_source_path(line)
    ]

    try:
        insert_at = next(index + 1 for index, line in enumerate(filtered) if line.strip() == ".")
    except StopIteration:
        insert_at = 0
    filtered.insert(insert_at, target)
    pth_path.write_text("\n".join(filtered).rstrip() + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pth", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    args = parser.parse_args()
    update_pth(args.pth.resolve(strict=True), args.backend)


if __name__ == "__main__":
    main()
