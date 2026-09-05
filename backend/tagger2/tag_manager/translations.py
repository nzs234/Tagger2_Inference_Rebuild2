"""Offline Chinese tag translations for the tag manager.

Booru tags are English, so the manager shows every tag bilingually. The
translations are shipped as committed gzip CSV dictionaries under
``resources/tag_translations`` (see that directory's README for the sources and
their licenses) and are never fetched at runtime, so the feature works offline.

Each profile's table is large (300k+ Danbooru rows), so it is parsed once per
process on first use and shared by every :class:`TagTranslations` instance
pointing at the same directory; loading is serialized by a module-level lock.
The mapping is immutable afterwards, so reads need no lock. A missing directory
is not an error: lookups simply return ``None`` and the UI stays English-only.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..security import atomic_write_bytes
from ..tag_text import canonical_tag_key

TRANSLATION_FORMAT = "tag-translations-v1"
SUPPORTED_PROFILES = ("e621", "danbooru")

# A saved translation longer than this is a description rather than a name.
MAX_USER_TRANSLATION_LENGTH = 64

# Reading a 300k-row dictionary is a one-off cost per profile per process.
_CACHE: dict[tuple[str, str, str], "_ProfileTable"] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class TranslationInfo:
    """What the API reports about one profile's dictionary."""

    entries: int
    loaded: bool
    source: str | None
    updated: str | None
    user_entries: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": self.entries,
            "loaded": self.loaded,
            "source": self.source,
            "updated": self.updated,
            "user_entries": self.user_entries,
        }


class _ProfileTable:
    """One loaded dictionary plus the manifest metadata describing it."""

    __slots__ = ("entries", "source", "updated", "user_entries")

    def __init__(
        self,
        entries: dict[str, str],
        source: str | None,
        updated: str | None,
        user_entries: int = 0,
    ) -> None:
        self.entries = entries
        self.source = source
        self.updated = updated
        self.user_entries = user_entries


def default_translation_dir() -> Path:
    """Return the committed dictionary directory under the project root."""

    from ..config import get_settings

    return get_settings().project_root / "resources" / "tag_translations"


def default_user_translation_dir() -> Path:
    """Return the writable directory for translations saved at runtime.

    This lives under the data directory (not the read-only shipped resources)
    so on-demand online translations survive restarts and dictionary rebuilds.
    """

    from ..config import get_settings

    settings = get_settings()
    base = settings.data_dir or settings.project_root / "data"
    return base / "tag_manager" / "translations"


def normalize_lookup_key(tag: str) -> str:
    """Return the dictionary key for one tag.

    The dictionaries are keyed on the lowercase underscore spelling, so a tag
    the user typed or stored with spaces still resolves.  This is the shared
    :func:`tagger2.tag_text.canonical_tag_key` rule; the alias is kept so the
    long-standing public name keeps working.
    """

    return canonical_tag_key(tag)


class TagTranslations:
    """Process-level English -> Chinese tag dictionary per profile.

    Each profile's table is the shipped gzip dictionary merged with the
    writable user dictionary (``user_dir / f"{profile}-zh.csv"``) that on-demand
    online translations are saved into. User entries win over shipped ones.
    """

    def __init__(self, directory: Path | None = None, user_dir: Path | None = None) -> None:
        self._directory = Path(directory) if directory is not None else default_translation_dir()
        self._user_dir = Path(user_dir) if user_dir is not None else default_user_translation_dir()

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def user_dir(self) -> Path:
        return self._user_dir

    # -- loading -----------------------------------------------------------

    def _cache_key(self, profile: str) -> tuple[str, str, str]:
        return (
            os.path.normcase(str(self._directory)),
            os.path.normcase(str(self._user_dir)),
            profile,
        )

    def _user_file(self, profile: str) -> Path:
        return self._user_dir / f"{profile}-zh.csv"

    def is_loaded(self, profile: str) -> bool:
        return self._cache_key(profile) in _CACHE

    def _table(self, profile: str) -> _ProfileTable:
        key = self._cache_key(profile)
        with _CACHE_LOCK:
            table = _CACHE.get(key)
            if table is None:
                table = self._load(profile)
                _CACHE[key] = table
        return table

    def _load(self, profile: str) -> _ProfileTable:
        """Read one dictionary; any problem degrades to an empty table.

        A missing or damaged dictionary must not break tag editing, so this
        never raises: the profile is simply reported as having 0 entries.
        """

        manifest = self._manifest()
        profiles = manifest.get("profiles") if isinstance(manifest, dict) else None
        info = profiles.get(profile) if isinstance(profiles, dict) else None
        file_name = None
        if isinstance(info, dict) and isinstance(info.get("file"), str):
            file_name = str(info["file"])
        path = self._directory / (file_name or f"{profile}-zh.csv.gz")
        updated = manifest.get("generated_at") if isinstance(manifest, dict) else None
        entries: dict[str, str] = {}
        if path.is_file():
            try:
                entries = _read_gzip_dictionary(path)
            except (OSError, UnicodeDecodeError, csv.Error, EOFError, gzip.BadGzipFile):
                entries = {}
        user_entries = 0
        user_file = self._user_file(profile)
        if user_file.is_file():
            try:
                user_table = _read_user_dictionary(user_file)
                entries.update(user_table)
                user_entries = len(user_table)
            except (OSError, UnicodeDecodeError, csv.Error, EOFError):
                user_entries = 0
        return _ProfileTable(
            entries,
            source=path.name if entries else None,
            updated=str(updated) if isinstance(updated, str) and entries else None,
            user_entries=user_entries,
        )

    def _manifest(self) -> dict[str, object]:
        path = self._directory / "MANIFEST.json"
        if not path.is_file():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(document, dict) or document.get("format") != TRANSLATION_FORMAT:
            return {}
        return document

    # -- lookups -----------------------------------------------------------

    def translate(self, profile: str, tag: str) -> str | None:
        """Return the Chinese name for one tag, or None when unknown."""

        if profile not in SUPPORTED_PROFILES:
            return None
        return self._table(profile).entries.get(normalize_lookup_key(tag))

    def translate_many(self, profile: str, tags: object) -> dict[str, str]:
        """Map each given tag (verbatim, as passed) to its translation.

        Only found tags appear in the result, so the caller can tell a missing
        translation from an empty one.
        """

        if profile not in SUPPORTED_PROFILES:
            return {}
        table = self._table(profile).entries
        result: dict[str, str] = {}
        for tag in tags if isinstance(tags, (list, tuple, set, frozenset)) else ():
            text = str(tag)
            translation = table.get(normalize_lookup_key(text))
            if translation is not None:
                result[text] = translation
        return result

    def info(self) -> dict[str, dict[str, object]]:
        """Report per-profile dictionary availability without forcing a load."""

        report: dict[str, dict[str, object]] = {}
        for profile in SUPPORTED_PROFILES:
            table = self._table(profile)
            report[profile] = TranslationInfo(
                entries=len(table.entries),
                loaded=True,
                source=table.source,
                updated=table.updated,
                user_entries=table.user_entries,
            ).as_dict()
        return report

    # -- saving ------------------------------------------------------------

    def ingest(self, profile: str, pairs: Mapping[str, str]) -> int:
        """Persist runtime translations into the profile's user dictionary.

        Entries are cleaned (empty, oversized and English-echo pairs dropped),
        merged over the existing user file, written atomically, and applied to
        the already-loaded in-process table so subsequent lookups resolve
        without a reload. Returns the number of pairs actually saved.
        """

        if profile not in SUPPORTED_PROFILES:
            return 0
        cleaned: dict[str, str] = {}
        for tag, zh in pairs.items():
            key = normalize_lookup_key(str(tag))
            value = str(zh).strip()
            if not key or not value or len(value) > MAX_USER_TRANSLATION_LENGTH:
                continue
            if key == normalize_lookup_key(value):
                continue
            cleaned[key] = value
        if not cleaned:
            return 0
        user_file = self._user_file(profile)
        merged: dict[str, str] = {}
        if user_file.is_file():
            try:
                merged = _read_user_dictionary(user_file)
            except (OSError, UnicodeDecodeError, csv.Error, EOFError):
                merged = {}
        merged.update(cleaned)
        _write_user_dictionary(user_file, merged)
        with _CACHE_LOCK:
            table = _CACHE.get(self._cache_key(profile))
            if table is not None:
                table.entries.update(cleaned)
                table.user_entries = len(merged)
        return len(cleaned)


def _read_gzip_dictionary(path: Path) -> dict[str, str]:
    """Parse one ``tag,zh`` gzip CSV into a lookup table."""

    entries: dict[str, str] = {}
    with gzip.open(path, "rb") as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        entries = _parse_dictionary_rows(csv.reader(stream))
    return entries


def _read_user_dictionary(path: Path) -> dict[str, str]:
    """Parse the writable ``tag,zh`` CSV for one profile.

    ``utf-8-sig`` tolerates a hand-edited file saved with a BOM; a wrong
    header raises :class:`csv.Error` so the caller can degrade gracefully.
    """

    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return _parse_dictionary_rows(csv.reader(stream))


def _parse_dictionary_rows(reader: Iterator[list[str]]) -> dict[str, str]:
    header = next(reader, None)
    if header is None:
        return {}
    if [column.lstrip("\ufeff").strip() for column in header] != ["tag", "zh"]:
        raise csv.Error(f"header must be 'tag,zh', found {header!r}")
    entries: dict[str, str] = {}
    for row in reader:
        if len(row) < 2:
            continue
        key = normalize_lookup_key(row[0])
        value = row[1].strip()
        if key and value:
            entries[key] = value
    return entries


def _write_user_dictionary(path: Path, rows: Mapping[str, str]) -> None:
    """Write the user dictionary deterministically (sorted by tag)."""

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["tag", "zh"])
    for tag in sorted(rows):
        writer.writerow([tag, rows[tag]])
    atomic_write_bytes(path, buffer.getvalue().encode("utf-8"))


def reset_translation_cache() -> None:
    """Drop the process-level cache (tests staging their own dictionaries)."""

    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "MAX_USER_TRANSLATION_LENGTH",
    "SUPPORTED_PROFILES",
    "TRANSLATION_FORMAT",
    "TagTranslations",
    "TranslationInfo",
    "default_translation_dir",
    "default_user_translation_dir",
    "normalize_lookup_key",
    "reset_translation_cache",
]
