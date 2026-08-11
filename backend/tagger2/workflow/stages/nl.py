"""NL stage: generate natural-language captions through a chat completion backend.

The frozen v4 prompt is assembled from the ported fragment files rather than
prose kept in Python, and every response passes through the ported strict
validator, so refusals, truncation and wrapped output are rejected instead of
being written into a dataset.

The stage talks to a narrow :class:`NlClient` protocol. In the application this
is backed by the existing provider clients and ``SecretStore``; in tests it is a
double, so NL behaviour is verifiable without any network access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

from .nl_validation import NlValidationError, validate_nl

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_VERSION = "nl-default-prompt-v4"
PRESETS = ("general", "style", "character")
LENGTHS = ("short", "medium", "long")
# Sentence budgets are frozen by the prompt fragments; kept here for display only.
LENGTH_SENTENCES = {"short": "2-3", "medium": "4-5", "long": "6-8"}
MAX_NL_BYTES = 16 * 1024


class NlError(RuntimeError):
    """Raised when NL generation cannot produce a usable caption."""


@lru_cache(maxsize=16)
def _fragment(name: str) -> str:
    path = PROMPT_DIR / f"{PROMPT_VERSION}-{name}.txt"
    if not path.is_file():
        raise NlError(f"frozen NL prompt fragment is missing: {name}")
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        raise NlError(f"NL prompt fragment must not contain a BOM: {name}")
    text = data.decode("utf-8").replace("\r\n", "\n").strip()
    if not text or "\x00" in text:
        raise NlError(f"NL prompt fragment is empty or contains NUL: {name}")
    return text


def build_system_prompt(preset: str, length: str) -> str:
    """Assemble the frozen v4 prompt for one preset and length.

    Layer order is fixed: base rules, then the preset, then the length budget.
    """

    if preset not in PRESETS:
        raise NlError(f"unsupported NL preset: {preset!r}")
    if length not in LENGTHS:
        raise NlError(f"unsupported NL length: {length!r}")
    return "\n".join((_fragment("base"), _fragment(preset), _fragment(length)))


@dataclass(frozen=True)
class NlRequest:
    """One NL generation request."""

    relative_image_path: str
    system_prompt: str
    payload: dict[str, Any]
    image_path: Path | None = None


@dataclass
class NlResult:
    """NL output for one sample."""

    relative_image_path: str
    nl: str = ""
    observation: dict[str, Any] = field(default_factory=dict)
    reused: bool = False
    skipped: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@runtime_checkable
class NlClient(Protocol):
    """Minimal chat-completion surface the NL stage needs.

    Returns the raw response body so the ported validator, not the client, is the
    single place that decides whether a response is acceptable.
    """

    def complete(self, request: NlRequest) -> bytes: ...


@dataclass
class NlStageReport:
    generated: int = 0
    reused: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[NlResult] = field(default_factory=list)

    def by_path(self) -> dict[str, NlResult]:
        return {result.relative_image_path: result for result in self.results}


def build_payload(
    projection: dict[str, Any],
    *,
    use_full_json: bool,
    current_nl: str = "",
    ocr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the untrusted data payload sent alongside the frozen prompt.

    Only the fields the prompt declares are included. Everything here is data,
    never instructions, which the base prompt states explicitly.
    """

    payload: dict[str, Any] = {}
    if use_full_json:
        payload["businessJson"] = {
            key: projection.get(key)
            for key in (
                "quality",
                "count",
                "character",
                "series",
                "artist",
                "appearance",
                "tags",
                "environment",
            )
        }
    else:
        payload["tags"] = list(projection.get("tags", []))
    character = str(projection.get("character", "")).split(",")[0].strip()
    if character:
        payload["primaryCharacterName"] = character
    if current_nl:
        payload["currentNl"] = current_nl
    if ocr:
        payload["ocr"] = ocr
    return payload


def run_nl_stage(
    samples: Sequence[Any],
    projections: dict[str, dict[str, Any]],
    *,
    source_root: Path,
    client: NlClient,
    preset: str = "general",
    length: str = "medium",
    reuse_original_nl: bool = True,
    use_image: bool = True,
    use_full_json: bool = False,
) -> NlStageReport:
    """Generate NL for every sample that needs it.

    A sample that already carries NL is reused when ``reuse_original_nl`` is set,
    so an existing human caption is never silently replaced. Validation failures
    are recorded per sample and leave the existing NL untouched.
    """

    report = NlStageReport()
    source_root = Path(source_root)
    system_prompt = build_system_prompt(preset, length)

    for sample in samples:
        relative = sample.relative_image_path
        projection = projections.get(relative, {})
        existing = str(projection.get("nl", "") or getattr(sample, "nl", "") or "")

        if reuse_original_nl and existing:
            report.reused += 1
            report.results.append(
                NlResult(relative_image_path=relative, nl=existing, reused=True)
            )
            continue

        request = NlRequest(
            relative_image_path=relative,
            system_prompt=system_prompt,
            payload=build_payload(
                projection, use_full_json=use_full_json, current_nl=existing
            ),
            image_path=(source_root / relative) if use_image else None,
        )

        try:
            body = client.complete(request)
            nl, observation = _validate_response(body)
        except (NlError, NlValidationError) as exc:
            report.failed += 1
            report.results.append(
                NlResult(relative_image_path=relative, error=str(exc))
            )
            continue
        except Exception as exc:  # noqa: BLE001 - surfaced as a per-sample issue
            report.failed += 1
            report.results.append(
                NlResult(relative_image_path=relative, error=f"NL request failed: {exc}")
            )
            continue

        report.generated += 1
        report.results.append(
            NlResult(relative_image_path=relative, nl=nl, observation=observation)
        )

    return report


def _validate_response(body: bytes) -> tuple[str, dict[str, Any]]:
    """Validate a structured NL response through the ported validator.

    Falls back to plain-text NL validation when the model returned a bare
    caption, but a structured response with malformed observation fields still
    records ``count_observation_invalid`` rather than being treated as clean.
    """

    from .nl_validation import validate_completion_response_v2

    try:
        nl, observation, _request_id, _usage = validate_completion_response_v2(body)
        return nl, observation
    except NlValidationError:
        # Not a structured object: accept a plain caption but mark the
        # observation as not requested so Count Review does not trust it.
        from .nl_validation import validate_completion_response

        nl, _request_id, _usage = validate_completion_response(body)
        return nl, {
            "schemaVersion": 1,
            "status": "not_requested",
            "countValue": None,
            "layoutValue": None,
            "sameCharacterRepeated": None,
            "warningCodes": [],
            "notRequestedReason": "unstructured_response",
        }


def encode_openai_request(
    request: NlRequest,
    *,
    model: str,
    max_tokens: int = 512,
) -> dict[str, Any]:
    """Build an OpenAI-compatible chat completion body for one NL request.

    The image, when used, is attached as a data URL so the caller does not need
    to expose a filesystem path to a third-party endpoint.
    """

    import base64
    import mimetypes

    content: list[dict[str, Any]] = [
        {"type": "text", "text": json.dumps(request.payload, ensure_ascii=False, sort_keys=True)}
    ]
    if request.image_path is not None:
        path = Path(request.image_path)
        if not path.is_file():
            raise NlError(f"NL image is unavailable: {request.relative_image_path}")
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
        )

    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": content},
        ],
    }


__all__ = [
    "LENGTHS",
    "LENGTH_SENTENCES",
    "MAX_NL_BYTES",
    "PRESETS",
    "PROMPT_VERSION",
    "NlClient",
    "NlError",
    "NlRequest",
    "NlResult",
    "NlStageReport",
    "build_payload",
    "build_system_prompt",
    "encode_openai_request",
    "run_nl_stage",
    "validate_nl",
]
