"""Persistent user settings (``data/settings.json``) and registered roots.

Extracted verbatim from ``main.Runtime`` so the settings document format,
merge order and lock discipline live in one place.  The store owns:

- reading/writing the JSON document (atomic, ``ensure_ascii=False``, indent 2)
  under a re-entrant lock;
- the registration list of user roots that is folded back into every write
  under the ``roots`` key;
- re-registering persisted roots into the shared :class:`PathAllowlist` on
  startup.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from .security import (
    PathAllowlist,
    PathNotAllowedError,
    PathRoot,
    SecurityError,
    atomic_write_bytes,
)


class UserSettingsStore:
    """Reads and writes ``settings.json`` and the persistent root list."""

    def __init__(self, *, settings_file: Path, allowlist: PathAllowlist) -> None:
        self.settings_file = settings_file
        self.allowlist = allowlist
        self._settings_lock = threading.RLock()
        self._persistent_roots: dict[str, PathRoot] = {}

    @staticmethod
    def _safe_root_label(value: Any, kind: str) -> str:
        label = str(value or "").strip()
        try:
            exposes_path = bool(label) and Path(label).expanduser().is_absolute()
        except (OSError, ValueError):
            exposes_path = True
        return f"{kind.title()} directory" if not label or exposes_path else label

    def _find_registered_root(self, path: Path) -> PathRoot | None:
        canonical = os.path.normcase(str(path.resolve(strict=False)))
        for value in self.allowlist.list_public():
            root = self.allowlist.get(str(value["root_id"]))
            if os.path.normcase(str(root.path)) == canonical:
                return root
        return None

    def _read_settings_document_unlocked(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.settings_file.read_text(encoding="utf-8-sig"))
            return dict(raw) if isinstance(raw, Mapping) else {}
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def read_settings_document(self) -> dict[str, Any]:
        with self._settings_lock:
            return self._read_settings_document_unlocked()

    def _serialized_persistent_roots(self) -> list[dict[str, Any]]:
        return [
            {
                "root_id": root.root_id,
                "path": str(root.path),
                "label": root.label,
                "kind": root.kind,
                "writable": root.writable,
            }
            for root in sorted(
                self._persistent_roots.values(), key=lambda value: value.root_id
            )
        ]

    def _write_settings_document_unlocked(self, document: Mapping[str, Any]) -> None:
        data = json.dumps(dict(document), ensure_ascii=False, indent=2).encode("utf-8")
        atomic_write_bytes(self.settings_file, data)

    def _persist_roots_unlocked(self) -> None:
        document = self._read_settings_document_unlocked()
        document["roots"] = self._serialized_persistent_roots()
        self._write_settings_document_unlocked(document)

    def load_persistent_roots(self) -> None:
        document = self._read_settings_document_unlocked()
        values = document.get("roots", [])
        if not isinstance(values, list):
            return
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for value in values:
            if not isinstance(value, Mapping):
                continue
            root_id = value.get("root_id")
            raw_path = value.get("path")
            kind = str(value.get("kind") or "")
            if not isinstance(root_id, str) or not root_id or not isinstance(raw_path, str):
                continue
            if kind not in {"input", "output", "model"}:
                continue
            path = Path(raw_path).expanduser().resolve(strict=False)
            path_key = os.path.normcase(str(path))
            if root_id in seen_ids or path_key in seen_paths or not path.is_dir():
                continue
            seen_ids.add(root_id)
            seen_paths.add(path_key)
            writable_value = value.get("writable")
            writable = kind == "output" or (kind == "input" and writable_value is True)
            existing = self._find_registered_root(path)
            if existing is not None:
                if existing.kind != kind or existing.writable != writable:
                    continue
                root = existing
            else:
                try:
                    root = self.allowlist.register(
                        path,
                        root_id=root_id,
                        label=self._safe_root_label(value.get("label"), kind),
                        kind=kind,
                        writable=writable,
                    )
                except SecurityError:
                    continue
            self._persistent_roots[root.root_id] = root

    def register_persistent_root(
        self,
        path: Path,
        *,
        name: str,
        kind: str,
        writable: bool | None = None,
    ) -> PathRoot:
        canonical = path.expanduser().resolve(strict=False)
        if not canonical.is_dir():
            raise PathNotAllowedError("root directory does not exist")
        desired_writable = kind == "output" if writable is None else bool(writable)
        if kind == "model" and desired_writable:
            raise SecurityError("model roots cannot be writable")
        with self._settings_lock:
            existing = self._find_registered_root(canonical)
            added = existing is None
            replaced: PathRoot | None = None
            if existing is not None:
                if existing.kind != kind:
                    raise SecurityError(
                        "directory is already registered with a different kind"
                    )
                # Registration is monotonic: a general read-only registration
                # request must not silently revoke a previously authorised
                # writable binding for the same root.
                if existing.writable and not desired_writable:
                    desired_writable = True
                if existing.writable != desired_writable:
                    replaced = existing
                    root = self.allowlist.register(
                        canonical,
                        root_id=existing.root_id,
                        kind=kind,
                        label=self._safe_root_label(name, kind),
                        writable=desired_writable,
                    )
                else:
                    root = existing
            else:
                root = self.allowlist.register(
                    canonical,
                    kind=kind,
                    label=self._safe_root_label(name, kind),
                    writable=desired_writable,
                )
            previous = self._persistent_roots.get(root.root_id)
            self._persistent_roots[root.root_id] = root
            try:
                self._persist_roots_unlocked()
            except Exception:
                if previous is None:
                    self._persistent_roots.pop(root.root_id, None)
                else:
                    self._persistent_roots[root.root_id] = previous
                if added:
                    self.allowlist.unregister(root.root_id)
                elif replaced is not None:
                    self.allowlist.register(
                        replaced.path,
                        root_id=replaced.root_id,
                        kind=replaced.kind,
                        label=replaced.label,
                        writable=replaced.writable,
                    )
                raise
            return root

    def save_user_settings(self, values: Mapping[str, Any]) -> None:
        with self._settings_lock:
            document = dict(values)
            document["roots"] = self._serialized_persistent_roots()
            self._write_settings_document_unlocked(document)
