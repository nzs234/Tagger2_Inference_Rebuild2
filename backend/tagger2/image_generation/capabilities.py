"""Versioned image-model capability registry.

The registry is deliberately conservative.  A model that is not recognised
gets only the common prompt/count/reference controls, so a proxy cannot be
sent a vendor-specific field by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


CAPABILITY_SCHEMA_VERSION = "image-capabilities-v1"
CAPABILITY_VERIFIED_AT = "2026-08-17"

GOOGLE_RATIOS = (
    "1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9",
    "21:9", "1:4", "4:1", "1:8", "8:1", "9:21",
)
GOOGLE_SIZES = ("512", "1K", "2K", "4K")
OPENAI_SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
OPENAI_QUALITIES = ("auto", "low", "medium", "high")
OPENAI_BACKGROUNDS = ("auto", "transparent", "opaque")
OPENAI_FORMATS = ("png", "jpeg", "webp")
OPENAI_MODERATION = ("auto", "low")
OPENAI_FIDELITY = ("low", "high")
RESPONSE_FORMATS = ("b64_json", "url")


@dataclass(frozen=True, slots=True)
class ImageCapability:
    family: str
    label: str
    known: bool
    operations: tuple[str, ...]
    parameters: tuple[str, ...]
    enums: Mapping[str, tuple[str, ...]]
    defaults: Mapping[str, Any]
    max_references: int
    max_outputs: int
    source_url: str
    notes: str

    def public(self, *, model: str, provider_id: str = "") -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "verified_at": CAPABILITY_VERIFIED_AT,
            "provider_id": provider_id,
            "model": model,
            "family": self.family,
            "label": self.label,
            "known": self.known,
            "operations": list(self.operations),
            "parameters": list(self.parameters),
            "enums": {key: list(value) for key, value in self.enums.items()},
            "defaults": dict(self.defaults),
            "max_references": self.max_references,
            "max_outputs": self.max_outputs,
            "source_url": self.source_url,
            "notes": self.notes,
        }


def capability_for_style(capability: ImageCapability, style: str) -> ImageCapability:
    """Project a model capability onto the selected wire protocol.

    A Gemini model behind an OpenAI-compatible gateway must not advertise its
    native ``generationConfig`` fields, and an image endpoint must not expose
    chat-only sampling controls.  Keeping this projection in one place makes
    the API response, request validation and persisted job snapshot agree.
    """

    normalized = str(style or "auto").strip().lower()
    if normalized in {"native", "gemini_native"}:
        return capability
    if normalized in {"openai_chat", "chat"}:
        if capability.family == "google_gemini":
            # Explicit compatible-chat mode preserves Gemini's image controls
            # in generation_config / extra_body.google. It is opt-in because
            # these extensions are not portable to arbitrary chat gateways.
            return capability
        return ImageCapability(
            family=capability.family,
            label=capability.label,
            known=capability.known,
            operations=capability.operations,
            parameters=("temperature", "top_p", "system_instruction"),
            enums={},
            defaults={"temperature": 0.7, "top_p": 0.95},
            max_references=capability.max_references,
            max_outputs=capability.max_outputs,
            source_url=capability.source_url,
            notes=capability.notes,
        )
    elif capability.family == "xai_grok_image":
        allowed = {"size", "quality", "aspect_ratio", "response_format"}
    elif capability.family == "google_gemini":
        # Explicit compatible-images mode carries these values in the Gemini
        # extension envelope in addition to the standard Images fields.
        allowed = {
            "aspect_ratio",
            "image_size",
            "include_text_modality",
            "temperature",
            "top_p",
            "top_k",
            "multi_image_strategy",
        }
    else:
        allowed = {
            "size", "quality", "background", "output_format",
            "output_compression", "moderation", "input_fidelity",
            "response_format",
        }
    parameters = tuple(name for name in capability.parameters if name in allowed)
    enums = {name: values for name, values in capability.enums.items() if name in parameters}
    defaults = {name: value for name, value in capability.defaults.items() if name in parameters}
    return ImageCapability(
        family=capability.family,
        label=capability.label,
        known=capability.known,
        operations=capability.operations,
        parameters=parameters,
        enums=enums,
        defaults=defaults,
        max_references=capability.max_references,
        max_outputs=capability.max_outputs,
        source_url=capability.source_url,
        notes=capability.notes,
    )


def capability_from_public(value: Mapping[str, Any]) -> ImageCapability:
    """Rebuild an immutable capability snapshot stored with a job."""

    enums = {
        str(key): tuple(str(item) for item in items)
        for key, items in dict(value.get("enums") or {}).items()
        if isinstance(items, (list, tuple))
    }
    return ImageCapability(
        family=str(value.get("family") or "unknown"),
        label=str(value.get("label") or "兼容模式"),
        known=bool(value.get("known", False)),
        operations=tuple(str(item) for item in value.get("operations", ())),
        parameters=tuple(str(item) for item in value.get("parameters", ())),
        enums=enums,
        defaults=dict(value.get("defaults") or {}),
        max_references=max(0, int(value.get("max_references", 4))),
        max_outputs=max(1, int(value.get("max_outputs", 4))),
        source_url=str(value.get("source_url") or ""),
        notes=str(value.get("notes") or ""),
    )


_GOOGLE = ImageCapability(
    family="google_gemini",
    label="Google Gemini / Nano Banana",
    known=True,
    operations=("generate", "edit"),
    parameters=(
        "aspect_ratio", "image_size", "include_text_modality", "system_instruction",
        "temperature", "top_p", "top_k", "multi_image_strategy",
    ),
    enums={
        "aspect_ratio": GOOGLE_RATIOS,
        "image_size": GOOGLE_SIZES,
        "multi_image_strategy": ("parallel", "candidate_count"),
    },
    defaults={
        "aspect_ratio": "1:1", "image_size": "1K", "temperature": 0.7,
        "top_p": 0.95, "top_k": 40, "include_text_modality": False,
        "multi_image_strategy": "parallel",
    },
    max_references=14,
    max_outputs=8,
    source_url="https://ai.google.dev/gemini-api/docs/image-generation",
    notes="Gemini native generateContent image output.",
)

_OPENAI = ImageCapability(
    family="openai_gpt_image",
    label="OpenAI GPT Image",
    known=True,
    operations=("generate", "edit"),
    parameters=(
        "size", "quality", "background", "output_format", "output_compression",
        "moderation", "input_fidelity", "response_format",
    ),
    enums={
        "size": OPENAI_SIZES,
        "quality": OPENAI_QUALITIES,
        "background": OPENAI_BACKGROUNDS,
        "output_format": OPENAI_FORMATS,
        "moderation": OPENAI_MODERATION,
        "input_fidelity": OPENAI_FIDELITY,
        "response_format": RESPONSE_FORMATS,
    },
    defaults={
        "size": "auto", "quality": "auto", "background": "auto",
        "output_format": "png", "moderation": "auto", "response_format": "b64_json",
    },
    max_references=16,
    max_outputs=8,
    source_url="https://platform.openai.com/docs/guides/images",
    notes="OpenAI Images generation/edit endpoints.",
)

_XAI = ImageCapability(
    family="xai_grok_image",
    label="xAI Grok Image",
    known=True,
    operations=("generate", "edit"),
    parameters=("size", "quality", "aspect_ratio", "response_format"),
    enums={
        "size": OPENAI_SIZES,
        "quality": OPENAI_QUALITIES,
        "aspect_ratio": GOOGLE_RATIOS,
        "response_format": RESPONSE_FORMATS,
    },
    defaults={"size": "auto", "quality": "auto", "response_format": "b64_json"},
    max_references=8,
    max_outputs=8,
    source_url="https://docs.x.ai/docs/guides/image-generations",
    notes="xAI image endpoints use OpenAI-shaped authentication and media fields.",
)

_UNKNOWN = ImageCapability(
    family="unknown",
    label="兼容模式",
    known=False,
    operations=("generate", "edit"),
    parameters=("response_format",),
    enums={"response_format": RESPONSE_FORMATS},
    defaults={"response_format": "b64_json"},
    max_references=4,
    max_outputs=4,
    source_url="",
    notes="仅发送通用 prompt、model、n 和参考图字段。",
)

_FAMILIES = {
    "google_gemini": _GOOGLE,
    "openai_gpt_image": _OPENAI,
    "xai_grok_image": _XAI,
    "unknown": _UNKNOWN,
}

_GOOGLE_MODEL_LIMITS: Mapping[str, tuple[tuple[str, ...], str, int, str]] = {
    "gemini-3-pro-image": (("1K", "2K", "4K"), "1K", 14, "Nano Banana Pro"),
    "gemini-3-pro-image-preview": (("1K", "2K", "4K"), "1K", 14, "Nano Banana Pro Preview"),
    "gemini-3.1-flash-image": (("512", "1K", "2K", "4K"), "1K", 10, "Nano Banana 2"),
    "gemini-3.1-flash-lite-image": (("512", "1K"), "1K", 10, "Nano Banana 2 Lite"),
    "gemini-2.5-flash-image": (("1K",), "1K", 3, "Nano Banana"),
}


def _specialize_model(capability: ImageCapability, model: str, *, kind: str) -> ImageCapability:
    normalized = model.casefold().removeprefix("models/")
    if capability.family == "google_gemini" and normalized in _GOOGLE_MODEL_LIMITS:
        sizes, default_size, max_references, label = _GOOGLE_MODEL_LIMITS[normalized]
        enums = dict(capability.enums)
        enums["image_size"] = sizes
        defaults = dict(capability.defaults)
        defaults["image_size"] = default_size
        return ImageCapability(
            family=capability.family,
            label=f"Google Gemini / {label}",
            known=capability.known,
            operations=capability.operations,
            parameters=capability.parameters,
            enums=enums,
            defaults=defaults,
            max_references=max_references,
            max_outputs=capability.max_outputs,
            source_url=capability.source_url,
            notes=capability.notes,
        )
    if capability.family == "xai_grok_image" and kind == "xai" and normalized == "grok-2-image-1212":
        return ImageCapability(
            family=capability.family,
            label="xAI Grok 2 Image",
            known=capability.known,
            operations=("generate",),
            parameters=("response_format",),
            enums={"response_format": RESPONSE_FORMATS},
            defaults={"response_format": "url"},
            max_references=0,
            max_outputs=capability.max_outputs,
            source_url=capability.source_url,
            notes="Legacy Grok image generation endpoint; editing and size controls are not assumed.",
        )
    if capability.family == "openai_gpt_image" and kind == "openai":
        parameters = tuple(name for name in capability.parameters if name != "response_format")
        enums = {name: values for name, values in capability.enums.items() if name != "response_format"}
        defaults = {name: value for name, value in capability.defaults.items() if name != "response_format"}
        return ImageCapability(
            family=capability.family,
            label=capability.label,
            known=capability.known,
            operations=capability.operations,
            parameters=parameters,
            enums=enums,
            defaults=defaults,
            max_references=capability.max_references,
            max_outputs=capability.max_outputs,
            source_url=capability.source_url,
            notes=capability.notes,
        )
    return capability


def _family_is_known(family: str, model: str, configured_family: str | None) -> bool:
    explicit = str(configured_family or "auto").strip().lower()
    if explicit == family and family != "unknown":
        return True
    normalized = model.casefold().removeprefix("models/")
    if family == "google_gemini":
        return normalized in _GOOGLE_MODEL_LIMITS or any(
            token in normalized for token in ("image", "nano-banana", "nanobanana")
        )
    if family == "openai_gpt_image":
        return normalized.startswith("gpt-image")
    if family == "xai_grok_image":
        return normalized == "grok-2-image-1212"
    return False


def capability_catalog() -> list[dict[str, Any]]:
    """Return the public family catalog without provider-specific secrets."""

    return [value.public(model="*") for value in (_GOOGLE, _OPENAI, _XAI, _UNKNOWN)]


def infer_family(
    *,
    kind: str,
    protocol: str,
    model: str,
    configured_family: str | None = None,
) -> str:
    explicit = (configured_family or "auto").strip().lower()
    if explicit in _FAMILIES and explicit != "unknown":
        return explicit
    normalized = model.casefold()
    if kind == "xai" or normalized.startswith(("grok", "xai-image")):
        return "xai_grok_image"
    if protocol == "gemini" or kind in {"gemini", "antigravity"} or "nano-banana" in normalized or "nanobanana" in normalized:
        return "google_gemini"
    if normalized.startswith("gpt-image"):
        return "openai_gpt_image"
    if explicit == "unknown":
        return "unknown"
    if protocol == "openai" and configured_family in {"openai_gpt_image", "xai_grok_image"}:
        return str(configured_family)
    return "unknown"


def capability_for_family(family: str, *, known: bool = True) -> ImageCapability:
    value = _FAMILIES.get(family, _UNKNOWN)
    if known or value is _UNKNOWN:
        return value
    return ImageCapability(
        family=value.family,
        label=value.label,
        known=False,
        operations=value.operations,
        parameters=("response_format",),
        enums={"response_format": RESPONSE_FORMATS},
        defaults={"response_format": "b64_json"},
        max_references=4,
        max_outputs=4,
        source_url=value.source_url,
        notes="模型未在注册表中确认，仅开放保守参数。",
    )


def capabilities_for(
    *,
    kind: str,
    protocol: str,
    model: str,
    configured_family: str | None = None,
    provider_id: str = "",
) -> dict[str, Any]:
    family = infer_family(kind=kind, protocol=protocol, model=model, configured_family=configured_family)
    known = _family_is_known(family, model, configured_family)
    capability = _specialize_model(capability_for_family(family, known=known), model, kind=kind)
    style = "native" if family == "google_gemini" else "openai_images"
    capability = capability_for_style(capability, style)
    return capability.public(model=model, provider_id=provider_id)


def capability_object(
    *, kind: str, protocol: str, model: str, configured_family: str | None = None
) -> ImageCapability:
    family = infer_family(kind=kind, protocol=protocol, model=model, configured_family=configured_family)
    known = _family_is_known(family, model, configured_family)
    return _specialize_model(capability_for_family(family, known=known), model, kind=kind)


__all__ = [
    "CAPABILITY_SCHEMA_VERSION",
    "ImageCapability",
    "capabilities_for",
    "capability_catalog",
    "capability_object",
    "capability_for_style",
    "capability_from_public",
    "infer_family",
]
