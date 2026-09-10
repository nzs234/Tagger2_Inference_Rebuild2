"""The nine-field standard JSON contract shared across subsystems.

The dataset workflow serializes nine-field documents (``tagger2.workflow.pipeline``
keeps its own copy of the tuple below) and the tag manager reads, filters and
re-renders them as sidecars (``tagger2.tag_manager.sidecar_io``).  The frozen
field order decides the key order of every serialized document and of the
editor payload, so both sides must stay in lockstep; the drift-guard test in
``backend/tests/test_tag_manager.py`` asserts the workflow's copy still equals
the one exported here.

The module lives at the package top level (like ``tag_text.py``) because both
subsystems import it; it must stay dependency-light and import from neither.
"""

from __future__ import annotations

NINE_FIELDS = (
    "quality",
    "count",
    "character",
    "series",
    "artist",
    "appearance",
    "tags",
    "environment",
    "nl",
)

__all__ = ["NINE_FIELDS"]
