"""Read-only smoke test for single-image, single/multi-model local inference."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _resolve(records, selector: str):
    folded = selector.casefold()
    matches = [
        record
        for record in records
        if folded
        in {
            record.model_id.casefold(),
            record.name.casefold(),
            record.path.name.casefold(),
        }
    ]
    if len(matches) != 1:
        raise SystemExit(f"model selector must match exactly once: {selector}")
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        default=str(PROJECT_ROOT / "data" / "benchmark_100" / "image_000.jpg"),
    )
    parser.add_argument("--model", action="append", dest="models")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-loaded-models", type=int, default=2)
    args = parser.parse_args()

    from tagger2.local_inference import LocalInferenceEngine
    from tagger2.model_registry import ModelRegistry

    image = Path(args.image).expanduser().resolve(strict=True)
    registry = ModelRegistry([PROJECT_ROOT / "models"])
    records = [record for record in registry.discover() if record.tags]
    selected = (
        [_resolve(records, selector) for selector in args.models]
        if args.models
        else [record for record in records if record.backend.value == "onnx"][:2]
    )
    if len(selected) < 2:
        raise SystemExit("at least two tagged models are required")
    selected = selected[:2]

    engine = LocalInferenceEngine(
        registry,
        device=args.device,
        max_loaded_models=max(1, args.max_loaded_models),
    )
    try:
        started = time.perf_counter()
        single = engine.predict_multi_result(
            [selected[0].model_id],
            image,
            include_model_tags=True,
        )
        single_seconds = time.perf_counter() - started
        if list(single.model_tags) != [selected[0].model_id]:
            raise RuntimeError("single-model result did not preserve its model group")

        started = time.perf_counter()
        multiple = engine.predict_multi_result(
            [record.model_id for record in selected],
            image,
            include_model_tags=True,
        )
        multi_seconds = time.perf_counter() - started
        if list(multiple.model_tags) != [record.model_id for record in selected]:
            raise RuntimeError("multi-model result groups do not match the requested models")
        if not single.tags or any(not multiple.model_tags[record.model_id] for record in selected):
            raise RuntimeError("one or more real models returned no tags at their preset thresholds")

        payload = {
            "status": "ok",
            "device": engine.device,
            "image": image.name,
            "single": {
                "model": selected[0].name,
                "tags": len(single.tags),
                "seconds": round(single_seconds, 3),
            },
            "multiple": {
                "models": [record.name for record in selected],
                "merged_tags": len(multiple.tags),
                "model_tags": {
                    record.name: len(multiple.model_tags[record.model_id])
                    for record in selected
                },
                "seconds": round(multi_seconds, 3),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        engine.close()


if __name__ == "__main__":
    main()
