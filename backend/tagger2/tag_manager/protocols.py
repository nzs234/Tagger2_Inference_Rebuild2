"""Structural interfaces for the tag manager's pluggable collaborators.

The service is assembled in ``main.py`` from the concrete
:class:`~tagger2.tag_manager.thumbnails.ThumbnailService` and
:class:`~tagger2.tag_manager.tag_db.TagDatabase`, while tests substitute
lightweight fakes.  The protocols below pin down the minimal surface the
service actually touches, so implementations and fakes stay interchangeable
structurally rather than through inheritance.  ``TagTranslations`` is small
and already fully annotated, so the service takes the real class directly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol


class ThumbnailProvider(Protocol):
    """Minimal on-demand thumbnail cache used by the image grid."""

    def ensure_thumbnail(self, source: Path, *, size: int = ..., mtime: float) -> Path:
        """Return the cached thumbnail path, generating it if missing."""
        ...


class TagDatabaseClient(Protocol):
    """Minimal autocomplete/category surface of the booru tag database."""

    def is_loaded(self, profile: str) -> bool:
        """Return whether ``profile`` already has a loaded index."""
        ...

    def ensure_loaded(self, profile: str, *, resource_id: str | None = None) -> None:
        """Load (once) the newest snapshot for ``profile`` or the given resource."""
        ...

    def lookup(
        self, profile: str, tag: str, *, resolve_alias: bool = ...
    ) -> Mapping[str, Any] | None:
        """Case-insensitively look up one tag; unknown tags return ``None``."""
        ...

    def autocomplete(
        self, profile: str, query: str, *, limit: int = ...
    ) -> Sequence[Mapping[str, Any]]:
        """Prefix search over canonical names, best post_count first."""
        ...

    def available_profiles(self) -> dict[str, list[str]]:
        """Map profile -> available classify resource ids, newest first."""
        ...


class TranslationProvider(Protocol):
    """Minimal online model surface used for NL and tag translations."""

    model: str

    async def generate(
        self,
        image: Any,
        prompt: str,
        *,
        model: str | None = ...,
        system_prompt: str | None = ...,
    ) -> str:
        """Generate one completion for ``prompt`` with the configured model."""
        ...


ProviderFactory = Callable[[str], TranslationProvider]
ProviderIds = Callable[[], list[str]]

__all__ = [
    "ProviderFactory",
    "ProviderIds",
    "TagDatabaseClient",
    "ThumbnailProvider",
    "TranslationProvider",
]
