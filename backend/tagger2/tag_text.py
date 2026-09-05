"""Shared tag text canonicalization.

Tag strings arrive from sidecars, query parameters, translation dictionaries
and model outputs with inconsistent separators (``long_hair`` vs ``long
hair``) and casing.  Every subsystem that compares tags must derive the same
comparison key, so the two primitives below are the single source of truth:

- :func:`canonical_tag_key` — the lowercase underscore form used by the
  translation dictionaries and the tag manager's SQLite filter index.
- :func:`canonical_tag_name` — the lowercase space form used to merge local
  model predictions.

Keep both dependency-light and behaviour-stable: the SQLite mirror in
``tag_manager.storage`` (``REPLACE(LOWER(t.tag), ' ', '_')``) and the web
client's ``translationKey`` helper must stay in lockstep with them.
"""

from __future__ import annotations


def canonical_tag_key(tag: str) -> str:
    """Return the lowercase underscore comparison key for one tag.

    Dictionaries and the tag filter index are keyed on the lowercase
    underscore spelling, so a tag the user typed or stored with spaces still
    resolves.  Only literal spaces fold into underscores; other whitespace is
    preserved verbatim to match the SQLite mirror of this rule.
    """

    return tag.strip().replace(" ", "_").casefold()


def canonical_tag_name(tag: str) -> str:
    """Return the lowercase space-separated form of one tag.

    Underscores become spaces and any whitespace run collapses to a single
    space, so ``Long__Hair`` and ``long hair`` merge onto one prediction.
    """

    return " ".join(tag.replace("_", " ").strip().casefold().split())


__all__ = ["canonical_tag_key", "canonical_tag_name"]
