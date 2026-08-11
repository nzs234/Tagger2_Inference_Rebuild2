"""Repeatable local-inference benchmark for the rebuilt engine."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


class _GpuMemorySampler:
    """Sample whole-device VRAM without adding a runtime Python dependency."""

    def __init__(self, device: str, interval_ms: int = 100):
        suffix = device.partition(":")[2]
        self.gpu_index = int(suffix) if suffix.isdigit() else 0
        self.interval_ms = max(50, int(interval_ms))
        self.baseline_mb: float | None = None
        self.peak_mb: float | None = None
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._first_sample = threading.Event()

    def start(self) -> None:
        executable = shutil.which("nvidia-smi")
        if executable is None:
            return
        creation_flags = 0x08000000 if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                [
                    executable,
                    f"--id={self.gpu_index}",
                    "--query-gpu=memory.used",
                    "--format=csv,noheader,nounits",
                    f"--loop-ms={self.interval_ms}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )
        except OSError:
            return
        self._thread = threading.Thread(
            target=self._read_samples,
            name="tagger2-gpu-memory",
            daemon=True,
        )
        self._thread.start()
        self._first_sample.wait(timeout=2.0)

    def _read_samples(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                value = float(line.strip().split()[0])
            except (IndexError, ValueError):
                continue
            if self.baseline_mb is None:
                self.baseline_mb = value
                self._first_sample.set()
            self.peak_mb = value if self.peak_mb is None else max(self.peak_mb, value)

    def stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def delta_mb(self) -> float | None:
        if self.baseline_mb is None or self.peak_mb is None:
            return None
        return max(0.0, self.peak_mb - self.baseline_mb)


def _percentile(values: Sequence[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percent * len(ordered)) - 1))
    return ordered[index]


def _resolve_model(registry: Any, selector: str) -> Any:
    records = registry.discover()
    supplied = Path(selector).expanduser()
    if supplied.exists():
        supplied = supplied.resolve(strict=False)
        try:
            return registry.register(supplied)
        except Exception as exc:
            raise SystemExit(f"Cannot register model path: {exc}") from exc

    folded = selector.casefold()
    matches = [
        record
        for record in records
        if folded in {record.model_id.casefold(), record.name.casefold(), record.path.name.casefold()}
    ]
    if len(matches) == 1:
        return matches[0]
    choices = ", ".join(f"{record.name} ({record.model_id})" for record in records)
    if not matches:
        raise SystemExit(f"Unknown model '{selector}'. Available models: {choices or 'none'}")
    raise SystemExit(f"Model selector '{selector}' is ambiguous: {choices}")


def _cuda_peak_mb() -> float | None:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.max_memory_allocated() / (1024 * 1024)
    except (ImportError, RuntimeError):
        pass
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="Opaque model ID, display name, or model path")
    parser.add_argument("--images", required=True, help="Directory containing benchmark images")
    parser.add_argument("--models-dir", default=str(PROJECT_ROOT / "models"))
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, or cuda:N")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--single-sample", type=int, default=20)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--gpu-sample-ms", type=int, default=100)
    parser.add_argument("--no-gpu-memory-sample", action="store_true")
    parser.add_argument("--output", help="Optional JSON report path")
    args = parser.parse_args()

    from tagger2.local_inference import LocalInferenceEngine
    from tagger2.model_registry import ModelRegistry

    image_root = Path(args.images).expanduser().resolve(strict=False)
    model_root = Path(args.models_dir).expanduser().resolve(strict=False)
    extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
    paths = [
        path
        for path in sorted(image_root.rglob("*"), key=lambda item: str(item).casefold())
        if path.is_file() and path.suffix.casefold() in extensions
    ][: max(1, args.limit)]
    if not paths:
        raise SystemExit("No supported images found")
    if not model_root.is_dir():
        raise SystemExit(f"Model directory does not exist: {model_root}")

    registry = ModelRegistry([model_root])
    record = _resolve_model(registry, args.model)
    engine = LocalInferenceEngine(registry, device=args.device, max_loaded_models=1)
    memory_sampler = _GpuMemorySampler(engine.device, args.gpu_sample_ms)
    if engine.device.startswith("cuda") and not args.no_gpu_memory_sample:
        memory_sampler.start()
    try:
        engine.load(record.model_id)
        # Warm up preprocessing, runtime graph selection and GPU kernels before
        # either latency or throughput measurements begin.
        engine.predict(record.model_id, paths[0], threshold=args.threshold)
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats()
        except (ImportError, RuntimeError):
            pass

        start = time.perf_counter()
        results = engine.predict_batch(
            record.model_id,
            paths,
            threshold=args.threshold,
            batch_size=max(1, args.batch_size),
        )
        elapsed = time.perf_counter() - start

        latencies_ms: list[float] = []
        for path in paths[: max(0, args.single_sample)]:
            single_start = time.perf_counter()
            engine.predict(record.model_id, path, threshold=args.threshold)
            latencies_ms.append((time.perf_counter() - single_start) * 1000.0)

        payload = {
            "schema_version": 1,
            "model_id": record.model_id,
            "model_name": record.name,
            "backend": record.backend.value,
            "device": engine.device,
            "images": len(paths),
            "batch_size": max(1, args.batch_size),
            "elapsed_seconds": round(elapsed, 4),
            "images_per_second": round(len(paths) / max(elapsed, 1e-9), 4),
            "single_samples": len(latencies_ms),
            "single_p50_ms": round(_percentile(latencies_ms, 0.50) or 0.0, 3),
            "single_p95_ms": round(_percentile(latencies_ms, 0.95) or 0.0, 3),
            "peak_torch_cuda_mb": (
                round(peak, 2) if (peak := _cuda_peak_mb()) is not None else None
            ),
            "results": len(results),
        }
    finally:
        engine.unload_all()
        memory_sampler.stop()

    payload.update(
        {
            # Windows WDDM does not expose reliable per-process VRAM through
            # nvidia-smi, so these values are whole-device samples. The delta
            # remains useful on an otherwise idle benchmark machine.
            "gpu_memory_scope": "whole_device" if memory_sampler.baseline_mb is not None else None,
            "gpu_memory_baseline_mb": memory_sampler.baseline_mb,
            "peak_gpu_used_mb": memory_sampler.peak_mb,
            "peak_gpu_delta_mb": memory_sampler.delta_mb,
        }
    )

    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = Path(args.output).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
