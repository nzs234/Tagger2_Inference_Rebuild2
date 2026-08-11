# Ported verbatim from the e621-standard-caption-workflow project
# (workers/policy/src/anima_policy_worker/policy.py). The seeded dropout draw,
# artist directory parsing, quality banding and the protected-field guarantees
# are kept byte-identical so the same seed reproduces the same dataset.
# Note: quality banding needs an aesthetic score. The LSE14-5k scorer is not
# obtainable, so quality dropout stays disabled unless a score is supplied.
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal


COUNT_VALUES = frozenset({"", "solo", "duo", "trio", "group"})
NON_SOLO_COUNTS = frozenset({"duo", "trio", "group"})
ARTIST_DIRECTORY = re.compile(r"^\d+_(?P<artist>.+)$")
POLICY_VERSION = "dataset-batch-policy-v1"


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class CoupledProbabilities:
    dropNl: float
    dropAppearance: float

    def __post_init__(self) -> None:
        for name, value in (("dropNl", self.dropNl), ("dropAppearance", self.dropAppearance)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PolicyError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise PolicyError(f"{name} must be between 0 and 1")
        if self.dropNl + self.dropAppearance > 1.0 + 1e-12:
            raise PolicyError("dropNl + dropAppearance must not exceed 1")


@dataclass(frozen=True)
class PolicyConfig:
    seed: str
    artistEnabled: bool
    artistDropoutProbability: float
    qualityEnabled: bool
    qualityDropoutProbability: float
    appearanceNlEnabled: bool
    solo: CoupledProbabilities
    nonSolo: CoupledProbabilities
    unknown: CoupledProbabilities
    policyVersion: Literal["dataset-batch-policy-v1"] = POLICY_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.seed, str) or not self.seed.strip() or len(self.seed.encode("utf-8")) > 256:
            raise PolicyError("seed must be a non-blank string of at most 256 UTF-8 bytes")
        for name, value in (
            ("artistEnabled", self.artistEnabled),
            ("qualityEnabled", self.qualityEnabled),
            ("appearanceNlEnabled", self.appearanceNlEnabled),
        ):
            if type(value) is not bool:
                raise PolicyError(f"{name} must be boolean")
        for name, value in (
            ("artistDropoutProbability", self.artistDropoutProbability),
            ("qualityDropoutProbability", self.qualityDropoutProbability),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise PolicyError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
                raise PolicyError(f"{name} must be between 0 and 1")


@dataclass(frozen=True)
class PolicyDecision:
    artistDropped: bool
    qualityDropped: bool
    appearanceNlAction: Literal["drop_nl", "drop_appearance", "keep_both", "unchanged"]


def artist_from_image_path(relative_image_path: str) -> str:
    if not isinstance(relative_image_path, str) or not relative_image_path:
        raise PolicyError("relative image path is empty")
    path = PureWindowsPath(relative_image_path.replace("/", "\\"))
    if path.is_absolute() or path.drive or any(part in {"", ".", ".."} for part in path.parts):
        raise PolicyError("relative image path is unsafe")
    if len(path.parts) < 2:
        raise PolicyError("image is not inside a first-level artist directory")
    match = ARTIST_DIRECTORY.fullmatch(path.parts[0])
    if match is None:
        raise PolicyError("first-level directory must use the number_artist form")
    artist = match.group("artist")
    if artist == "noartname":
        return ""
    if not artist or artist != artist.strip() or any(character in artist for character in "\r\n\x00"):
        raise PolicyError("artist directory suffix is invalid")
    return f"@{artist}"


def merge_artists(existing: str, appended: str) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for value in (*existing.split(","), appended):
        artist = value.strip()
        if artist and artist.casefold() not in seen:
            seen.add(artist.casefold())
            values.append(artist)
    return ", ".join(values)


def quality_for_score(score: float) -> list[str]:
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise PolicyError("aesthetic score must be numeric")
    value = float(score)
    if not math.isfinite(value) or not 1.0 <= value <= 5.0:
        raise PolicyError("aesthetic score must be finite and between 1 and 5")
    quantized = math.ceil(value * 2.0)
    if quantized <= 4:
        return ["low quality"]
    if quantized <= 6:
        return ["normal quality"]
    if quantized <= 8:
        return ["good quality"]
    return ["masterpiece", "best quality"]


def stable_random(config: PolicyConfig, annotation_key: str, decision_name: str) -> float:
    if not isinstance(annotation_key, str) or not annotation_key:
        raise PolicyError("annotation key must be non-empty")
    if not isinstance(decision_name, str) or not decision_name:
        raise PolicyError("decision name must be non-empty")
    identity = annotation_key.replace("/", "\\").casefold()
    source = "\0".join((config.policyVersion, config.seed, identity, decision_name)).encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(source).digest()[:8], "big")
    return integer / 2**64


def _validate_business_json(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PolicyError("business JSON must be an object")
    required = {"quality", "count", "character", "series", "artist", "appearance", "tags", "environment", "nl"}
    if set(value) != required:
        raise PolicyError("business JSON must contain exactly the nine standard fields")
    for field in ("quality", "appearance", "tags", "environment"):
        items = value[field]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise PolicyError(f"{field} must be an array of strings")
    for field in ("count", "character", "series", "artist", "nl"):
        if not isinstance(value[field], str):
            raise PolicyError(f"{field} must be a string")
    if value["count"] not in COUNT_VALUES:
        raise PolicyError("count must already be normalized")
    return dict(value)


def _coupled_policy(config: PolicyConfig, count: str) -> CoupledProbabilities:
    if count == "solo":
        return config.solo
    if count in NON_SOLO_COUNTS:
        return config.nonSolo
    return config.unknown


def apply_policy(
    payload: object,
    *,
    annotation_key: str,
    relative_image_path: str,
    config: PolicyConfig,
    aesthetic_score: float | None,
) -> tuple[dict[str, object], PolicyDecision]:
    result = _validate_business_json(payload)
    original_protected = {
        field: result[field]
        for field in ("count", "character", "series", "tags", "environment")
    }

    artist_dropped = False
    if config.artistEnabled:
        result["artist"] = merge_artists(str(result["artist"]), artist_from_image_path(relative_image_path))
        artist_dropped = stable_random(config, annotation_key, "artist") < config.artistDropoutProbability
        if artist_dropped:
            result["artist"] = ""

    quality_dropped = False
    if config.qualityEnabled:
        if aesthetic_score is None:
            raise PolicyError("quality is enabled but no aesthetic score was supplied")
        result["quality"] = quality_for_score(aesthetic_score)
        quality_dropped = stable_random(config, annotation_key, "quality") < config.qualityDropoutProbability
        if quality_dropped:
            result["quality"] = []

    action: Literal["drop_nl", "drop_appearance", "keep_both", "unchanged"] = "unchanged"
    if config.appearanceNlEnabled:
        appearance = result["appearance"]
        nl = result["nl"]
        assert isinstance(appearance, list) and isinstance(nl, str)
        has_appearance = any(item.strip() for item in appearance)
        has_nl = bool(nl.strip())
        if has_appearance and has_nl:
            probabilities = _coupled_policy(config, str(result["count"]))
            draw = stable_random(config, annotation_key, "appearance-nl")
            if draw < probabilities.dropNl:
                result["nl"] = ""
                action = "drop_nl"
            elif draw < probabilities.dropNl + probabilities.dropAppearance:
                result["appearance"] = []
                action = "drop_appearance"
            else:
                action = "keep_both"
        else:
            # A pre-existing empty side protects the remaining non-empty side.
            action = "unchanged"

    for field, original in original_protected.items():
        if result[field] != original:
            raise PolicyError(f"protected field changed unexpectedly: {field}")
    appearance = result["appearance"]
    nl = result["nl"]
    assert isinstance(appearance, list) and isinstance(nl, str)
    original = _validate_business_json(payload)
    original_had_signal = any(item.strip() for item in original["appearance"]) or bool(str(original["nl"]).strip())
    if original_had_signal and not (any(item.strip() for item in appearance) or nl.strip()):
        raise PolicyError("appearance and nl cannot both be removed by policy")
    return result, PolicyDecision(artist_dropped, quality_dropped, action)
