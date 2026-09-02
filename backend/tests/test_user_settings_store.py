"""Round-trip tests for the extracted user settings store."""

import json
from pathlib import Path

from tagger2.security import PathAllowlist, SecurityError
from tagger2.user_settings import UserSettingsStore


def _store(tmp_path: Path) -> tuple[UserSettingsStore, PathAllowlist, Path]:
    allowlist = PathAllowlist()
    settings_file = tmp_path / "data" / "settings.json"
    store = UserSettingsStore(settings_file=settings_file, allowlist=allowlist)
    return store, allowlist, settings_file


def test_settings_document_round_trips_and_keeps_roots(tmp_path: Path) -> None:
    store, _allowlist, settings_file = _store(tmp_path)
    assert store.read_settings_document() == {}

    directory = tmp_path / "images"
    directory.mkdir()
    root = store.register_persistent_root(directory, name="Images", kind="input")
    document = json.loads(settings_file.read_text(encoding="utf-8"))
    assert [entry["root_id"] for entry in document["roots"]] == [root.root_id]
    assert document["roots"][0]["kind"] == "input"
    assert document["roots"][0]["writable"] is False

    store.save_user_settings({"default_mode": "local"})
    document = json.loads(settings_file.read_text(encoding="utf-8"))
    assert document["default_mode"] == "local"
    assert [entry["root_id"] for entry in document["roots"]] == [root.root_id]

    # A fresh store over the same file restores both document and roots.
    reloaded, allowlist, _ = _store(tmp_path)
    reloaded.load_persistent_roots()
    assert reloaded.read_settings_document() == {"default_mode": "local", "roots": document["roots"]}
    assert allowlist.get(root.root_id).writable is False


def test_register_persistent_root_monotonic_and_conflicting(tmp_path: Path) -> None:
    store, allowlist, _settings_file = _store(tmp_path)
    directory = tmp_path / "out"
    directory.mkdir()

    store.register_persistent_root(directory, name="Out", kind="input", writable=True)
    same = store.register_persistent_root(directory, name="Out", kind="input")
    assert allowlist.get(same.root_id).writable is True  # never downgraded

    try:
        store.register_persistent_root(directory, name="Out", kind="output")
    except SecurityError:
        pass
    else:
        raise AssertionError("same directory with a different kind must conflict")


def test_corrupt_settings_file_reads_empty(tmp_path: Path) -> None:
    store, _allowlist, settings_file = _store(tmp_path)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{not-json", encoding="utf-8")
    assert store.read_settings_document() == {}
