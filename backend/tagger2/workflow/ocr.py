"""
OCR stage for Dataset Workflow.

Ported from: Anima Dataset Workflow (source project)
License: Proprietary - authorized reuse with attribution

This module provides optical character recognition for images using PaddleOCR
in an isolated runtime environment. OCR results are written to separate sidecar
files and do not block the main pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class OCREngine(Protocol):
    """Protocol for OCR engines."""

    def recognize_text(
        self, image_path: Path, min_confidence: float = 0.5
    ) -> list[dict[str, Any]]:
        """
        Recognize text in an image.

        Args:
            image_path: Path to the image file
            min_confidence: Minimum confidence threshold (0.0-1.0)

        Returns:
            List of detected text regions with boxes and confidence scores
            Format: [{"text": str, "box": [[x,y], ...], "confidence": float}, ...]
        """
        ...


@dataclass
class OCRIssue:
    """A non-blocking OCR problem, translated by the caller into a StageIssue."""

    relative_image_path: str | None
    code: str
    message: str


@dataclass
class OCRResult:
    """Result of OCR processing."""

    image_path: str
    detected_regions: list[dict[str, Any]]
    success: bool
    error: str | None = None


class PaddleOCREngine:
    """
    PaddleOCR engine using isolated runtime.

    This engine runs PaddleOCR in a separate Python process to avoid
    dependency conflicts with the main application.
    """

    def __init__(
        self,
        runtime_python: Path | None = None,
        model_dir: Path | None = None,
        timeout_seconds: float = 30.0,
    ):
        """
        Initialize PaddleOCR engine.

        Args:
            runtime_python: Path to Python interpreter with PaddleOCR installed
            model_dir: Path to PaddleOCR model directory
        """
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("OCR timeout_seconds must be a positive finite number")

        # The OCR dependency is deliberately isolated from the application
        # runtime.  An explicit interpreter is useful for tests and for a
        # future profile-specific runtime, but the implicit path is always the
        # project-local ``runtime_ocr`` environment.  Never silently execute
        # the main application's Python interpreter: that can make a machine
        # appear OCR-ready while loading a different Paddle/numpy ABI.
        self.runtime_python = (
            Path(runtime_python).expanduser().resolve()
            if runtime_python is not None
            else self._find_paddle_runtime()
        )
        self.model_dir = model_dir
        self.timeout_seconds = timeout_seconds
        self._validate_runtime()

    def _find_paddle_runtime(self) -> Path:
        """Find PaddleOCR runtime Python interpreter."""
        project_root = Path(__file__).resolve().parents[3]
        runtime_root = project_root / "runtime_ocr"
        candidates = (
            runtime_root / "Scripts" / "python.exe",
            runtime_root / "bin" / "python",
        )

        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()

        # Keep the expected path in the error even when the environment has
        # not been provisioned.  This is consumed by preflight and makes the
        # missing-runtime condition deterministic and actionable.
        preferred = candidates[0] if os.name == "nt" else candidates[1]
        raise RuntimeError(
            "OCR runtime not found; expected the project-local isolated "
            f"interpreter at {preferred} (run scripts/setup_ocr_runtime.ps1)"
        )

    def _validate_runtime(self) -> None:
        """Validate that the runtime has PaddleOCR installed."""
        if not self.runtime_python.is_file():
            raise RuntimeError(f"OCR runtime interpreter not found: {self.runtime_python}")
        try:
            result = subprocess.run(
                [str(self.runtime_python), "-c", "import paddleocr"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"PaddleOCR not available in runtime: {result.stderr}"
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise RuntimeError(f"Failed to validate OCR runtime: {e}")

    @property
    def cache_key(self) -> str:
        """Stable identity for sidecar cache invalidation.

        The executable path and metadata are included so replacing the
        isolated runtime invalidates old sidecars.  Model configuration is
        included when a profile supplies a model directory.
        """

        digest = hashlib.sha256()
        digest.update(str(self.runtime_python).encode("utf-8"))
        try:
            stat = self.runtime_python.stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
        except OSError:
            # Validation normally catches this first.  Keeping the key
            # computable makes diagnostics and tests deterministic.
            digest.update(b"missing")
        digest.update(platform.python_implementation().encode("ascii"))
        digest.update(str(self.model_dir or "").encode("utf-8"))
        digest.update(b"paddleocr:v1:lang=en:angle_cls=true")
        return digest.hexdigest()

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """Terminate a timed-out OCR process and descendants."""

        if process.poll() is not None:
            return

        if os.name == "nt":
            # ``PaddleOCR`` may create helper processes.  Terminating only the
            # Python parent leaves those behind and can exhaust resources over
            # a long dataset run.
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            killpg = getattr(os, "killpg", None)
            try:
                if killpg is None:
                    process.kill()
                else:
                    killpg(process.pid, 9)
            except (OSError, ProcessLookupError):
                process.kill()

        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def _run_isolated(self, script: str) -> tuple[int, str, str]:
        """Run one OCR request with a hard deadline and process-tree cleanup."""

        creationflags = 0
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(
            [str(self.runtime_python), "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
            **popen_kwargs,
        )
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            self._terminate_process_tree(process)
            raise RuntimeError(
                f"OCR process timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        return process.returncode, stdout, stderr

    def recognize_text(
        self, image_path: Path, min_confidence: float = 0.5
    ) -> list[dict[str, Any]]:
        """
        Recognize text in an image using PaddleOCR.

        Args:
            image_path: Path to the image file
            min_confidence: Minimum confidence threshold

        Returns:
            List of detected text regions
        """
        # Create a JSONL protocol script for PaddleOCR
        script = f'''
import json
import sys
from paddleocr import PaddleOCR

def main():
    ocr = PaddleOCR(
        use_angle_cls=True,
        lang="en",
        show_log=False,
    )
    
    image_path = {json.dumps(str(image_path))}
    min_conf = {min_confidence}
    
    try:
        result = ocr.ocr(image_path, cls=True)
        
        if not result or not result[0]:
            print(json.dumps({{"success": True, "regions": []}}))
            return
        
        regions = []
        for line in result[0]:
            if line:
                box, (text, confidence) = line
                if confidence >= min_conf:
                    regions.append({{
                        "text": text,
                        "box": box,
                        "confidence": float(confidence),
                    }})
        
        print(json.dumps({{"success": True, "regions": regions}}))
    
    except Exception as e:
        print(json.dumps({{"success": False, "error": str(e)}}), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
'''

        try:
            returncode, stdout, stderr = self._run_isolated(script)

            if returncode != 0:
                raise RuntimeError(f"OCR process failed: {stderr}")

            response = json.loads(stdout)
            if not response.get("success"):
                raise RuntimeError(response.get("error", "Unknown error"))

            return response.get("regions", [])

        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid OCR response: {e}")


def _sha256_file(path: Path) -> str:
    """Return a content digest used to invalidate stale OCR sidecars."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _engine_cache_key(engine: OCREngine) -> str:
    """Get a stable runtime key without constraining lightweight test engines."""

    key = getattr(engine, "cache_key", None)
    if isinstance(key, str) and key:
        return key
    # Custom engines are intentionally supported by the stage protocol.  A
    # qualified class name prevents a sidecar produced by one implementation
    # from being mistaken for another while keeping existing test doubles
    # compatible.
    return f"engine:{type(engine).__module__}.{type(engine).__qualname__}"


def _sidecar_cache_keys(
    image_path: Path,
    relative_path: str,
    min_confidence: float,
    engine: OCREngine,
) -> tuple[str, str, str]:
    """Build image, configuration and combined sidecar cache keys."""

    image_key = _sha256_file(image_path)
    config_key = hashlib.sha256(
        json.dumps(
            {
                "relative_path": relative_path,
                "min_confidence": float(min_confidence),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    combined = hashlib.sha256(
        f"{image_key}:{config_key}:{_engine_cache_key(engine)}".encode()
    ).hexdigest()
    return image_key, config_key, combined


def _cached_sidecar_matches(
    data: dict[str, Any],
    *,
    image_key: str,
    config_key: str,
    cache_key: str,
) -> bool:
    """Check a v2 cache record; retain the v1 compatibility behavior."""

    # Existing workspaces contain v1 sidecars without fingerprints.  They are
    # still readable and reusable, preserving the public sidecar format.  All
    # sidecars written by this version carry the three keys below and are
    # invalidated when the image/config/runtime changes.
    stored_cache_key = data.get("cache_key")
    if stored_cache_key is None:
        return data.get("version", "v1") == "v1"
    return (
        stored_cache_key == cache_key
        and data.get("image_sha256") == image_key
        and data.get("config_key") == config_key
    )


def run_ocr_stage(
    workspace: Path,
    samples: dict[str, Path],
    ocr_config: dict[str, Any],
    ocr_engine: OCREngine | None = None,
) -> tuple[dict[str, OCRResult], list[OCRIssue]]:
    """
    Run OCR stage on all samples.

    Args:
        workspace: Workspace directory
        samples: Map of relative_path -> absolute image path
        ocr_config: OCR configuration from job config
        ocr_engine: OCR engine instance (optional, will create default if None)

    Returns:
        Tuple of (results dict, issues list)
    """
    if not ocr_config.get("enabled", False):
        return {}, []

    if ocr_engine is None:
        try:
            ocr_engine = PaddleOCREngine(
                timeout_seconds=float(ocr_config.get("timeout_seconds", 30.0))
            )
        except RuntimeError as e:
            # OCR runtime not available - create a non-blocking issue
            issue = OCRIssue(
                relative_image_path=None,
                code="ocr_unavailable",
                message=f"OCR runtime not available: {e}",
            )
            return {}, [issue]

    min_confidence = ocr_config.get("min_confidence", 0.5)
    force_reprocess = ocr_config.get("force_reprocess", False)
    
    ocr_dir = workspace / "ocr_sidecars"
    ocr_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, OCRResult] = {}
    issues: list[OCRIssue] = []

    for relative_path, image_path in samples.items():
        # Check if sidecar already exists
        sidecar_path = ocr_dir / f"{relative_path}.ocr.json"
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            image_key, config_key, cache_key = _sidecar_cache_keys(
                image_path,
                relative_path,
                min_confidence,
                ocr_engine,
            )
        except OSError as exc:
            results[relative_path] = OCRResult(
                image_path=relative_path,
                detected_regions=[],
                success=False,
                error=f"Unable to fingerprint image for OCR: {exc}",
            )
            issues.append(
                OCRIssue(
                    relative_image_path=relative_path,
                    code="ocr_failed",
                    message=f"OCR processing failed: {exc}",
                )
            )
            continue

        if sidecar_path.exists() and not force_reprocess:
            # Load existing OCR result
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if _cached_sidecar_matches(
                        data,
                        image_key=image_key,
                        config_key=config_key,
                        cache_key=cache_key,
                    ):
                        results[relative_path] = OCRResult(
                            image_path=relative_path,
                            detected_regions=data.get("regions", []),
                            success=True,
                        )
                        continue
            except (OSError, json.JSONDecodeError):
                # A corrupt or unreadable sidecar is re-processed below rather
                # than failing the run; the fresh result overwrites it.
                pass

        # Run OCR
        try:
            regions = ocr_engine.recognize_text(image_path, min_confidence)
            
            result = OCRResult(
                image_path=relative_path,
                detected_regions=regions,
                success=True,
            )
            results[relative_path] = result

            # Write sidecar file
            sidecar_data = {
                "version": "v1",
                "image": relative_path,
                "min_confidence": min_confidence,
                "image_sha256": image_key,
                "config_key": config_key,
                "runtime_key": _engine_cache_key(ocr_engine),
                "cache_key": cache_key,
                "regions": regions,
                "region_count": len(regions),
            }
            
            with open(sidecar_path, "w", encoding="utf-8") as f:
                json.dump(sidecar_data, f, ensure_ascii=False, indent=2)

        except Exception as e:  # noqa: BLE001 - any engine error degrades to a warning
            # OCR failure is non-blocking
            result = OCRResult(
                image_path=relative_path,
                detected_regions=[],
                success=False,
                error=str(e),
            )
            results[relative_path] = result

            issue = OCRIssue(
                relative_image_path=relative_path,
                code="ocr_failed",
                message=f"OCR processing failed: {e}",
            )
            issues.append(issue)

    return results, issues


def load_ocr_sidecar(workspace: Path, relative_path: str) -> dict[str, Any] | None:
    """
    Load OCR sidecar file for a sample.

    Args:
        workspace: Workspace directory
        relative_path: Relative path to the sample

    Returns:
        OCR data dict or None if not found
    """
    ocr_dir = workspace / "ocr_sidecars"
    sidecar_path = ocr_dir / f"{relative_path}.ocr.json"

    if not sidecar_path.exists():
        return None

    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
