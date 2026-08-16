from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
UPDATE_SCRIPT = ROOT / "scripts" / "update_from_git.ps1"


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _git(cwd: Path, *arguments: str) -> str:
    result = _run(["git", *arguments], cwd=cwd)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("PowerShell is not available")
    return executable


def test_update_script_fast_forwards_and_rejects_tracked_changes(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"

    _git(tmp_path, "init", "--bare", str(origin))
    _git(tmp_path, "init", "-b", "main", str(seed))
    _git(seed, "config", "user.name", "Update Script Test")
    _git(seed, "config", "user.email", "update-script@example.invalid")
    (seed / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "tracked.txt")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(tmp_path, "clone", str(origin), str(checkout))

    (seed / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(seed, "commit", "-am", "second")
    _git(seed, "push", "origin", "main")

    result = _run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(UPDATE_SCRIPT),
            "-ProjectRoot",
            str(checkout),
            "-Remote",
            "origin",
            "-Branch",
            "main",
        ],
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == _git(seed, "rev-parse", "HEAD")
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "two\n"

    (checkout / "tracked.txt").write_text("local edit\n", encoding="utf-8")
    (seed / "tracked.txt").write_text("three\n", encoding="utf-8")
    _git(seed, "commit", "-am", "third")
    _git(seed, "push", "origin", "main")
    before = _git(checkout, "rev-parse", "HEAD")

    blocked = _run(
        [
            _powershell(),
            "-NoProfile",
            "-File",
            str(UPDATE_SCRIPT),
            "-ProjectRoot",
            str(checkout),
        ],
        cwd=ROOT,
    )
    assert blocked.returncode != 0
    assert "uncommitted tracked changes" in (blocked.stdout + blocked.stderr)
    assert _git(checkout, "rev-parse", "HEAD") == before
    assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "local edit\n"
