"""Stable nine-field caption normalization and flat TXT serialization.

Ported from the source workflow project so the pure rule stages produce
byte-identical output for the same input.
"""

from .flat_txt import FlatTextSerializationError, flat_txt_sha256, serialize_flat_txt
from .normalizer import (
    ARRAY_FIELDS,
    COUNT_VALUES,
    FIELDS,
    STRING_FIELDS,
    CaptionDisplayPolicy,
    FieldError,
    NormalizationResult,
    display_tag,
    flat_txt_representable,
    normalize_annotation,
    normalize_json_bytes,
)

__all__ = [
    "ARRAY_FIELDS",
    "COUNT_VALUES",
    "FIELDS",
    "STRING_FIELDS",
    "CaptionDisplayPolicy",
    "FieldError",
    "FlatTextSerializationError",
    "NormalizationResult",
    "display_tag",
    "flat_txt_representable",
    "flat_txt_sha256",
    "normalize_annotation",
    "normalize_json_bytes",
    "serialize_flat_txt",
]
