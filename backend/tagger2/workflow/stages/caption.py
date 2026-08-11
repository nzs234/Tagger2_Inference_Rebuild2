"""Caption stage: produce tag lists via the host local inference engine.

This stage does not reimplement inference. It adapts the existing
:class:`~tagger2.local_inference.LocalInferenceEngine` and
:class:`~tagger2.model_registry.ModelRegistry` so the workflow reuses model
loading, preprocessing profiles, threshold modes, adapters and device selection.

The frozen TXT display transform is ported from the source project's caption
worker, so a caption written here renders identically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

MAX_FORMATTED_TXT_BYTES = 262_144
# Escape backslashes and parentheses only; matches the source caption worker.
ESCAPE_PATTERN = re.compile(r"([\\()])")


class CaptionError(RuntimeError):
    """Raised when a caption cannot be produced or represented."""


@dataclass(frozen=True)
class CaptionDisplaySettings:
    """Display policy for the frozen caption TXT format."""

    replace_underscores_with_spaces: bool = True
    preserve_escapes: bool = True
    triggers_enabled: bool = False
    trigger_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptionTag:
    """One predicted tag, kept in model spelling."""

    raw_tag: str
    score: float | None = None
    category: str = "general"


@dataclass
class CaptionResult:
    """Caption output for one sample."""

    relative_image_path: str
    tags: tuple[CaptionTag, ...] = ()
    txt: str = ""
    model_id: str = ""
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@runtime_checkable
class TagPredictor(Protocol):
    """Minimal surface the caption stage needs from an inference backend.

    Implemented by :class:`EngineTagPredictor` over the host engine, and by test
    doubles, so caption behaviour is verifiable without loading a real model.
    """

    def predict_tags(self, image_path: Path) -> Sequence[CaptionTag]: ...


class EngineTagPredictor:
    """Adapter from the host :class:`LocalInferenceEngine` to :class:`TagPredictor`."""

    def __init__(
        self,
        engine: Any,
        model_id: str,
        *,
        threshold: float | None = None,
        category_thresholds: dict[str, float] | None = None,
        use_category_thresholds: bool = True,
    ):
        self.engine = engine
        self.model_id = model_id
        self.threshold = threshold
        self.category_thresholds = category_thresholds
        self.use_category_thresholds = use_category_thresholds

    def predict_tags(self, image_path: Path) -> Sequence[CaptionTag]:
        # `threshold=None` keeps the model's own default, which is the
        # `model_default` threshold mode in the workflow config.
        items = self.engine.predict(
            self.model_id,
            image_path,
            threshold=self.threshold,
            category_thresholds=self.category_thresholds,
            use_category_thresholds=self.use_category_thresholds,
        )
        return tuple(
            CaptionTag(
                raw_tag=str(item.text),
                score=getattr(item, "score", None),
                category=str(getattr(item, "category", "general") or "general"),
            )
            for item in items
        )


def display_tag(raw_tag: str, settings: CaptionDisplaySettings) -> str:
    """Render one tag into the frozen TXT display form.

    Ported from the source caption worker: a tag that cannot be represented is
    an error rather than being silently dropped or rewritten.
    """

    value = raw_tag.replace("_", " ") if settings.replace_underscores_with_spaces else raw_tag
    if settings.preserve_escapes:
        value = ESCAPE_PATTERN.sub(r"\\\1", value)
    if not value or value != value.strip() or any(ch in value for ch in ",\r\n\x00"):
        raise CaptionError(f"caption tag is not representable in the frozen TXT format: {raw_tag!r}")
    return value


def format_caption(tags: Sequence[CaptionTag], settings: CaptionDisplaySettings) -> str:
    """Serialize predicted tags into the frozen caption TXT line."""

    if not tags:
        raise CaptionError("caption formatting requires at least one tag")
    values: list[str] = []
    if settings.triggers_enabled:
        values.extend(display_tag(term, settings) for term in settings.trigger_terms)
    values.extend(display_tag(tag.raw_tag, settings) for tag in tags)
    result = ", ".join(values)
    if len(result.encode("utf-8")) > MAX_FORMATTED_TXT_BYTES:
        raise CaptionError("formatted caption exceeds 256 KiB")
    return result


@dataclass
class CaptionStageReport:
    """Aggregate outcome of the caption stage."""

    captioned: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[CaptionResult] = field(default_factory=list)

    def by_path(self) -> dict[str, CaptionResult]:
        return {result.relative_image_path: result for result in self.results}


def settings_from_config(caption_config: dict[str, Any]) -> CaptionDisplaySettings:
    """Build display settings from the workflow caption config section."""

    terms = caption_config.get("trigger_terms", ())
    if isinstance(terms, str):
        raise CaptionError("trigger_terms must be a list of strings, not a string")
    return CaptionDisplaySettings(
        replace_underscores_with_spaces=bool(
            caption_config.get("replace_underscores_with_spaces", True)
        ),
        preserve_escapes=bool(caption_config.get("preserve_escapes", True)),
        triggers_enabled=bool(caption_config.get("triggers_enabled", False)),
        trigger_terms=tuple(str(term) for term in terms),
    )


def run_caption_stage(
    samples: Sequence[Any],
    *,
    source_root: Path,
    predictor: TagPredictor,
    settings: CaptionDisplaySettings,
    model_id: str = "",
) -> CaptionStageReport:
    """Caption every sample that needs it.

    A sample whose annotation already supplies tags (raw e621 JSON or a non-blank
    tag TXT) is skipped, matching the source project: existing annotations are
    authoritative and are never regenerated here. Failures are recorded per
    sample so one bad image cannot abort the stage.
    """

    report = CaptionStageReport()
    source_root = Path(source_root)

    for sample in samples:
        relative = sample.relative_image_path
        if getattr(sample, "skip_caption", False):
            report.skipped += 1
            report.results.append(
                CaptionResult(
                    relative_image_path=relative,
                    skipped=True,
                    skip_reason=f"existing annotation: {sample.annotation_kind}",
                )
            )
            continue

        try:
            tags = tuple(predictor.predict_tags(source_root / relative))
            if not tags:
                raise CaptionError("model returned no tags above threshold")
            txt = format_caption(tags, settings)
        except CaptionError as exc:
            report.failed += 1
            report.results.append(
                CaptionResult(relative_image_path=relative, model_id=model_id, error=str(exc))
            )
            continue
        except Exception as exc:  # noqa: BLE001 - surfaced as a per-sample issue
            report.failed += 1
            report.results.append(
                CaptionResult(
                    relative_image_path=relative,
                    model_id=model_id,
                    error=f"inference failed: {exc}",
                )
            )
            continue

        report.captioned += 1
        report.results.append(
            CaptionResult(
                relative_image_path=relative,
                tags=tags,
                txt=txt,
                model_id=model_id,
            )
        )

    return report


__all__ = [
    "MAX_FORMATTED_TXT_BYTES",
    "CaptionDisplaySettings",
    "CaptionError",
    "CaptionResult",
    "CaptionStageReport",
    "CaptionTag",
    "EngineTagPredictor",
    "TagPredictor",
    "display_tag",
    "format_caption",
    "run_caption_stage",
    "settings_from_config",
]
