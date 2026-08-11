from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from verify_lock import LockValidationError, parse_lock  # noqa: E402


@pytest.mark.parametrize(
    ("name", "runtime_package", "excluded_package"),
    [
        ("requirements-cpu.lock", "onnxruntime", "onnxruntime-gpu"),
        ("requirements-gpu.lock", "onnxruntime-gpu", "onnxruntime"),
    ],
)
def test_runtime_locks_are_hashed_and_variant_specific(
    name: str, runtime_package: str, excluded_package: str
) -> None:
    pins = parse_lock(PROJECT_ROOT / name)

    assert runtime_package in pins
    assert excluded_package not in pins
    for required in (
        "fastapi",
        "httpx",
        "torch",
        "torchvision",
        "timm",
        "transformers",
        "peft",
        "lycoris-lora",
    ):
        assert required in pins


def test_development_lock_is_hashed_and_includes_lock_tooling() -> None:
    pins = parse_lock(PROJECT_ROOT / "requirements-dev.lock")

    assert pins["pip-tools"][1] == "7.6.0"
    assert "pytest" in pins
    assert "ruff" in pins
    assert "mypy" in pins


def test_lock_validation_rejects_local_and_unhashed_requirements(tmp_path: Path) -> None:
    local = tmp_path / "local.lock"
    local.write_text("demo @ file:///C:/private/demo.whl\n", encoding="utf-8")
    with pytest.raises(LockValidationError, match="local filesystem"):
        parse_lock(local)

    unhashed = tmp_path / "unhashed.lock"
    unhashed.write_text("demo==1.0\n", encoding="utf-8")
    with pytest.raises(LockValidationError, match="no SHA-256"):
        parse_lock(unhashed)


def test_startup_uses_versioned_hashed_locks_and_integrity_modules() -> None:
    script = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

    assert "requirements-gpu.lock" in script
    assert "requirements-cpu.lock" in script
    assert "--require-hashes" in script
    assert ".tagger2-runtime-v2-!RUNTIME_VARIANT!" in script
    assert "scripts\\verify_lock.py" in script
    assert "--module transformers" in script
    assert "--module peft" in script
    assert "--module lycoris" in script
