"""Release version consistency across the repository.

The version lives in four places that must never drift apart (pyproject.toml,
``tagger2.__version__``, frontend ``package.json`` and the lockfile root), and
release notes must exist for the current version because
``scripts/build_release.ps1`` copies them by literal path and README links
them. This module guards both invariants offline; the display-version mapping
mirrors the PowerShell logic (``1.10.0`` -> ``V1.10``, anything else keeps its
patch segment).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None, "pyproject.toml is missing the project version"
    return match.group(1)


def _release_notes_name(version: str) -> str:
    display = version
    if re.fullmatch(r"\d+\.\d+\.0", display):
        display = display[: -len(".0")]
    return f"V{display}_RELEASE_NOTES.md"


def test_version_is_identical_across_all_manifests() -> None:
    version = _pyproject_version()

    init_text = (ROOT / "backend" / "tagger2" / "__init__.py").read_text(encoding="utf-8")
    init_match = re.search(r'^__version__\s*=\s*"([^"]+)"', init_text, re.MULTILINE)
    assert init_match is not None, "backend/tagger2/__init__.py is missing __version__"
    assert init_match.group(1) == version

    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == version

    lockfile = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    assert lockfile["version"] == version
    assert lockfile["packages"][""]["version"] == version


def test_release_notes_exist_and_are_wired_into_packaging_and_readme() -> None:
    version = _pyproject_version()
    notes_name = _release_notes_name(version)
    notes = ROOT / "docs" / notes_name
    assert notes.is_file(), f"missing release notes for {version}: {notes}"

    packaging = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
    assert notes_name in packaging, "build_release.ps1 must copy the current release notes"

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert notes_name in readme, "README must link the current release notes"


def test_display_version_mapping_matches_release_script() -> None:
    """Spot-check the x.y.0 -> Vx.y mapping used by build_release.ps1."""
    assert _release_notes_name("1.10.0") == "V1.10_RELEASE_NOTES.md"
    assert _release_notes_name("1.06.1") == "V1.06.1_RELEASE_NOTES.md"
    assert _release_notes_name("1.10.5") == "V1.10.5_RELEASE_NOTES.md"
