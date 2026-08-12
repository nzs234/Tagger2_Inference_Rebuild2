"""Content-addressed tokenizer resources used by the token-budget stage.

The workflow stores a tokenizer JSON as an immutable catalog resource.  The
loader deliberately uses the ``tokenizers`` package directly: no model
weights, network access, or implicit Hugging Face cache lookup is involved at
execution time.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any


class TokenizerResourceError(ValueError):
    """Raised when a tokenizer resource is missing or cannot be loaded."""


class TokenizerCounter:
    """Callable token counter backed by a serialized ``tokenizer.json``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise TokenizerResourceError(f"tokenizer resource not found: {self.path}")
        try:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(self.path))
        except Exception as exc:  # noqa: BLE001 - normalize third-party errors
            raise TokenizerResourceError(
                f"tokenizer resource is not loadable: {self.path.name}"
            ) from exc

        # A deterministic probe catches an empty/corrupt tokenizer before the
        # first production batch.  It also ensures the loaded object exposes
        # the encoding API expected by the budget worker.
        try:
            probe = self._tokenizer.encode("workflow tokenizer probe")
            if not isinstance(probe.ids, list):
                raise TypeError("tokenizer encoding ids are not a list")
        except Exception as exc:  # noqa: BLE001 - normalize third-party errors
            raise TokenizerResourceError(
                f"tokenizer resource failed the execution probe: {self.path.name}"
            ) from exc

    def __call__(self, texts: Sequence[str | bytes]) -> list[int]:
        counts: list[int] = []
        for text in texts:
            if isinstance(text, bytes):
                try:
                    text = text.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise TokenizerResourceError("tokenizer input is not valid UTF-8") from exc
            if not isinstance(text, str):
                raise TokenizerResourceError("tokenizer input must be text or UTF-8 bytes")
            try:
                counts.append(len(self._tokenizer.encode(text).ids))
            except Exception as exc:  # noqa: BLE001 - normalize third-party errors
                raise TokenizerResourceError("tokenizer failed to encode input") from exc
        return counts


def load_tokenizer_counter(path: Path) -> TokenizerCounter:
    """Load and probe one catalog tokenizer resource."""

    return TokenizerCounter(path)


def validate_tokenizer_resource(path: Path) -> dict[str, Any]:
    """Return a catalog-compatible validation report for tokenizer JSON."""

    try:
        counter = TokenizerCounter(path)
        sample_count = len(counter(["validation probe"]))
        return {
            "valid": True,
            "errors": [],
            "line_count": 1,
            "sample_count": sample_count,
        }
    except (OSError, TokenizerResourceError) as exc:
        return {"valid": False, "errors": [str(exc)], "line_count": 0}


__all__ = [
    "TokenizerCounter",
    "TokenizerResourceError",
    "load_tokenizer_counter",
    "validate_tokenizer_resource",
]
