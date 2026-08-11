"""Online vision provider package."""

from .base import (
    APIKeyPool,
    ProviderConfig,
    ProviderError,
    ProviderKind,
    ProviderProtocol,
    RETRYABLE_STATUS_CODES,
    backoff_seconds,
    load_api_keys_from_file,
    normalize_base_url,
    parse_retry_after,
    validate_base_url,
)
from .client import (
    AntigravityProvider,
    ClaudeProvider,
    GeminiProvider,
    LMStudioProvider,
    OpenAICompatibleProvider,
    VisionProvider,
    create_provider,
)
from .image import PreparedImage, encode_image, prepare_image

OpenAIProvider = OpenAICompatibleProvider

__all__ = [
    "APIKeyPool",
    "AntigravityProvider",
    "ClaudeProvider",
    "GeminiProvider",
    "LMStudioProvider",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "PreparedImage",
    "ProviderConfig",
    "ProviderError",
    "ProviderKind",
    "ProviderProtocol",
    "RETRYABLE_STATUS_CODES",
    "VisionProvider",
    "backoff_seconds",
    "create_provider",
    "encode_image",
    "load_api_keys_from_file",
    "normalize_base_url",
    "parse_retry_after",
    "prepare_image",
    "validate_base_url",
]
