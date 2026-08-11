"""
OCR stage for Dataset Workflow.

Ported from: Anima Dataset Workflow (source project)
License: Proprietary - authorized reuse with attribution

This module provides optical character recognition for images using PaddleOCR
in an isolated runtime environment. OCR results are written to separate sidecar
files and do not block the main pipeline.
"""

from __future__ import annotations

import json
import subprocess
import sys
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
    ):
        """
        Initialize PaddleOCR engine.

        Args:
            runtime_python: Path to Python interpreter with PaddleOCR installed
            model_dir: Path to PaddleOCR model directory
        """
        self.runtime_python = runtime_python or self._find_paddle_runtime()
        self.model_dir = model_dir
        self._validate_runtime()

    def _find_paddle_runtime(self) -> Path:
        """Find PaddleOCR runtime Python interpreter."""
        # Look for common locations
        candidates = [
            Path("runtime_ocr/Scripts/python.exe"),  # Windows
            Path("runtime_ocr/bin/python"),  # Linux/Mac
            Path("../runtime_ocr/Scripts/python.exe"),
            Path("../runtime_ocr/bin/python"),
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()

        # Fall back to system Python (may not have PaddleOCR)
        return Path(sys.executable)

    def _validate_runtime(self) -> None:
        """Validate that the runtime has PaddleOCR installed."""
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
            result = subprocess.run(
                [str(self.runtime_python), "-c", script],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            if result.returncode != 0:
                raise RuntimeError(f"OCR process failed: {result.stderr}")

            response = json.loads(result.stdout)
            if not response.get("success"):
                raise RuntimeError(response.get("error", "Unknown error"))

            return response.get("regions", [])

        except subprocess.TimeoutExpired:
            raise RuntimeError("OCR process timed out after 30 seconds")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid OCR response: {e}")


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
            ocr_engine = PaddleOCREngine()
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

        if sidecar_path.exists() and not force_reprocess:
            # Load existing OCR result
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
