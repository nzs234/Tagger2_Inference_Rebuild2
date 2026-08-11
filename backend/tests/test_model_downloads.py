import asyncio
import json
from pathlib import Path

import pytest

from tagger2.model_downloads import ModelDownloadManager, parse_huggingface_url
from tagger2.model_registry import ModelRegistry


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://huggingface.co/SmilingWolf/wd-tagger", ("SmilingWolf/wd-tagger", None)),
        (
            "https://www.huggingface.co/owner/model/tree/release-v2",
            ("owner/model", "release-v2"),
        ),
        ("https://huggingface.co/owner/model.git", ("owner/model", None)),
    ],
)
def test_parse_huggingface_model_url(url: str, expected: tuple[str, str | None]) -> None:
    assert parse_huggingface_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://huggingface.co/owner/model",
        "https://example.com/owner/model",
        "https://user:secret@huggingface.co/owner/model",
        "https://huggingface.co/owner/model?token=secret",
        "https://huggingface.co/owner/model/blob/main/model.onnx",
        "https://huggingface.co/../model",
    ],
)
def test_parse_huggingface_url_rejects_unsafe_or_non_repository_urls(url: str) -> None:
    with pytest.raises(ValueError):
        parse_huggingface_url(url)


def test_download_registers_model_without_network(tmp_path: Path, monkeypatch) -> None:
    calls = []
    loaded = []

    def fake_snapshot_download(*, repo_id: str, revision: str | None, local_dir: Path):
        calls.append((repo_id, revision, local_dir))
        local_dir.mkdir(parents=True)
        (local_dir / "model.onnx").write_bytes(b"onnx")
        (local_dir / "selected_tags.csv").write_text(
            "name,category\nportrait,0\nsolo,4\n",
            encoding="utf-8",
        )
        (local_dir / "thresholds.json").write_text(
            json.dumps({"general": 0.44, "character": 0.77}),
            encoding="utf-8",
        )
        return str(local_dir)

    monkeypatch.setattr("tagger2.model_downloads.snapshot_download", fake_snapshot_download)
    model_root = tmp_path / "models"
    model_root.mkdir()
    registry = ModelRegistry([model_root])
    manager = ModelDownloadManager(model_root, registry, loader=loaded.append)

    async def run():
        record = manager.start("https://huggingface.co/owner/tagger", "v2")
        for _ in range(100):
            current = manager.get(record.id)
            if current and current.status in {"succeeded", "failed"}:
                return current
            await asyncio.sleep(0.01)
        raise AssertionError("download did not complete")

    result = asyncio.run(run())
    assert result.status == "succeeded"
    assert len(result.model_ids) == 1
    assert result.loaded_model_ids == result.model_ids
    assert result.load_errors == []
    assert loaded == result.model_ids
    assert calls == [("owner/tagger", "v2", model_root / "owner__tagger")]
    registered = registry.get(result.model_ids[0])
    assert registered.tags == ("portrait", "solo")
    assert registered.thresholds["general"] == 0.44
    assert registered.thresholds["character"] == 0.77
