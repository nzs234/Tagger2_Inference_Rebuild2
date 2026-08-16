"""Tagger2 Inference backend core package."""

__version__ = "1.4.0"

from .config import AppConfig, Settings, get_settings
from .local_inference import (
    AdapterError,
    InferenceError,
    LocalInferenceEngine,
    ModelLoadError,
    TaggerInference,
    UnsafeModelError,
    merge_predictions,
)
from .model_registry import ModelBackend, ModelRecord, ModelRegistry
from .preprocessing import PreprocessProfile, load_preprocess_profile, preprocess_image
from .schemas import (
    AnimaPayload,
    ErrorEnvelope,
    ImageResult,
    ModelResult,
    JobEvent,
    JobMode,
    JobState,
    ProviderKind,
    TagItem,
)
from .security import PathAllowlist, PathNotAllowedError, SecurityError
from .secrets import CompositeSecretStore, SecretStore

__all__ = [
    "__version__",
    "AppConfig",
    "Settings",
    "get_settings",
    "InferenceError",
    "ModelLoadError",
    "UnsafeModelError",
    "AdapterError",
    "LocalInferenceEngine",
    "TaggerInference",
    "merge_predictions",
    "ModelBackend",
    "ModelRecord",
    "ModelRegistry",
    "PreprocessProfile",
    "load_preprocess_profile",
    "preprocess_image",
    "AnimaPayload",
    "ErrorEnvelope",
    "ImageResult",
    "ModelResult",
    "JobEvent",
    "JobMode",
    "JobState",
    "ProviderKind",
    "TagItem",
    "PathAllowlist",
    "PathNotAllowedError",
    "SecurityError",
    "CompositeSecretStore",
    "SecretStore",
]
