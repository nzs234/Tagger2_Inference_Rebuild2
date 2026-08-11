# Ported from the e621-standard-caption-workflow project
# (workers/token_budget/src/anima_token_budget_worker/budget.py).
# Only the caption-format import path is adapted to this package; the trim
# order (quality, environment, tags, appearance) and the largest-fitting-prefix
# search are byte-identical, so the same budget yields the same result.
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from ..caption_format import normalize_annotation, serialize_flat_txt
from ..caption_format.normalizer import CaptionDisplayPolicy


TRIMMABLE_FIELDS = ("quality", "environment", "tags", "appearance")
TOKENIZER_BATCH_SIZE = 64


class TokenBudgetError(ValueError):
    pass


@dataclass(frozen=True)
class FitResult:
    status: str
    original_tokens: int
    final_tokens: int
    removed: dict[str, list[str]]
    annotation: dict[str, object] | None


def normalized_annotation(annotation: object, caption_format: Mapping[str, object]) -> tuple[dict[str, object], CaptionDisplayPolicy]:
    try:
        policy = CaptionDisplayPolicy.from_mapping(caption_format)
        raw = json.dumps(annotation, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError) as exc:
        raise TokenBudgetError("annotation or caption format is invalid") from exc
    result = normalize_annotation(raw, policy, export_format="both")
    if not result.valid or result.payload is None:
        raise TokenBudgetError("annotation cannot be flattened for token counting")
    return result.payload, policy


def tokenizer_count_many(tokenizer: object, texts: Sequence[bytes]) -> list[int]:
    counts: list[int] = []
    for start in range(0, len(texts), TOKENIZER_BATCH_SIZE):
        for text in texts[start:start + TOKENIZER_BATCH_SIZE]:
            if not isinstance(text, bytes):
                raise TokenBudgetError("flattened caption must be UTF-8 bytes")
            try:
                encoding = tokenizer.encode(text.decode("utf-8"), add_special_tokens=False)
                ids = encoding.ids
            except (AttributeError, TypeError, UnicodeError, ValueError) as exc:
                raise TokenBudgetError("tokenizer failed to count a flattened caption") from exc
            count = len(ids)
            if type(count) is not int or count < 0:
                raise TokenBudgetError("tokenizer returned an invalid token count")
            counts.append(count)
    return counts


def _counts(count_many: Callable[[list[bytes]], Sequence[int]], texts: list[bytes]) -> list[int]:
    values = list(count_many(texts))
    if len(values) != len(texts) or any(type(value) is not int or value < 0 for value in values):
        raise TokenBudgetError("tokenizer count batch is invalid")
    return values


def fit(
    annotation: object,
    caption_format: Mapping[str, object],
    max_tokens: int,
    count_many: Callable[[list[bytes]], Sequence[int]],
) -> FitResult:
    if type(max_tokens) is not int or max_tokens < 1:
        raise TokenBudgetError("maxTokens is invalid")
    current, policy = normalized_annotation(annotation, caption_format)
    original = _counts(count_many, [serialize_flat_txt(current, policy)])[0]
    removed = {field: [] for field in TRIMMABLE_FIELDS}
    if original <= max_tokens:
        return FitResult("within_budget", original, original, removed, current)
    for field in TRIMMABLE_FIELDS:
        values = list(current[field])
        candidates: list[tuple[int, dict[str, object], bytes]] = []
        for keep in range(len(values), -1, -1):
            candidate = {**current, field: values[:keep]}
            candidates.append((keep, candidate, serialize_flat_txt(candidate, policy)))
        counts = _counts(count_many, [candidate[2] for candidate in candidates])
        fitting = [(candidate, count) for candidate, count in zip(candidates, counts) if count <= max_tokens]
        if fitting:
            (keep, current, _), final = max(fitting, key=lambda value: value[0][0])
            removed[field] = values[keep:]
            return FitResult("trimmed", original, final, removed, current)
        current = {**current, field: []}
        removed[field] = values
    final = _counts(count_many, [serialize_flat_txt(current, policy)])[0]
    return FitResult("overflow", original, final, removed, None)
