"""Tests for content-addressed tokenizer resources."""

from __future__ import annotations

from pathlib import Path

from tagger2.workflow.resources import WorkflowResourceCatalog
from tagger2.workflow.tokenizer_resource import (
    TokenizerResourceError,
    load_tokenizer_counter,
    validate_tokenizer_resource,
)


def _write_tokenizer(path: Path) -> None:
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace

    tokenizer = Tokenizer(WordLevel(vocab={"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(path))


def test_tokenizer_resource_validates_and_counts(tmp_path):
    source = tmp_path / "tokenizer.json"
    _write_tokenizer(source)

    report = validate_tokenizer_resource(source)
    assert report["valid"] is True
    assert load_tokenizer_counter(source)([b"hello world"]) == [2]

    catalog = WorkflowResourceCatalog(tmp_path / "resources")
    manifest = catalog.import_resource(source, "tokenizer-test-v1", "tokenizer")
    loaded = catalog.get_resource_path(manifest.resource_id)
    assert loaded is not None
    assert catalog.validate_resource(loaded, "tokenizer")["valid"] is True


def test_tokenizer_resource_rejects_invalid_file(tmp_path):
    source = tmp_path / "broken.json"
    source.write_text("not json", encoding="utf-8")

    report = validate_tokenizer_resource(source)
    assert report["valid"] is False
    try:
        load_tokenizer_counter(source)
    except TokenizerResourceError as exc:
        assert "not loadable" in str(exc)
    else:  # pragma: no cover - protects the fail-closed contract
        raise AssertionError("invalid tokenizer unexpectedly loaded")
