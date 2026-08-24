"""Sidecar names must always pair with their image name.

Generation filenames carry dots inside the name (LoRA weights such as
``_0.75)`` / ``_1.2)``).  ``Path.stem`` and ``Path.with_suffix`` treat the text
after the last dot as an extension, so the old two-step idiom truncated the
name and the TXT no longer matched the image.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from tagger2.artifacts import (
    ArtifactManager,
    MAX_NAME_BYTES,
    atomic_write_bytes,
    numbered_name,
    numbered_path,
    replace_suffix,
    strip_artifact_suffix,
    write_anima_artifacts,
)
from tagger2.anima import parse_anima_response
from tagger2.local_inference import LocalPrediction
from tagger2.main import Runtime
from tagger2.schemas import TagItem
from tagger2.security import PathAllowlist
from tagger2.storage import SQLiteStorage


LONG_DOTTED_IMAGE = (
    "43900-1956700319-(by 3Danimetest_0.75),(by hiru1181273415_0.9),"
    "(by rossciaco_0.8),(by Nani14cm_0.65),(by uken l_0.65),"
    "_(andyredtiger_1.2),yellow.png"
)
LONG_DOTTED_STEM = LONG_DOTTED_IMAGE[: -len(".png")]


def _anima():
    return parse_anima_response(
        '{"quality":["highres"],"count":"solo","character":"","series":"",'
        '"artist":"","appearance":["red fur"],"tags":["digital art"],'
        '"environment":["outdoors"],"nl":"caption"}'
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (LONG_DOTTED_IMAGE, LONG_DOTTED_STEM),
        ("a_1.2),yellow.png", "a_1.2),yellow"),
        ("weights_0.75.JPEG", "weights_0.75"),
        ("plain.webp", "plain"),
        ("caption.txt", "caption"),
        ("payload.json", "payload"),
        # No known extension: nothing may be removed, otherwise the sidecar
        # would drift away from the image name again.
        ("a_1.2),yellow", "a_1.2),yellow"),
        ("no_extension", "no_extension"),
        ("archive.tar.gz", "archive.tar.gz"),
    ],
)
def test_strip_artifact_suffix_only_removes_known_extensions(name, expected):
    assert strip_artifact_suffix(name) == expected


def test_replace_suffix_keeps_every_dot_inside_the_name():
    image = Path("D:/imgs") / LONG_DOTTED_IMAGE

    assert replace_suffix(image, ".txt").name == LONG_DOTTED_STEM + ".txt"
    assert replace_suffix(image, ".json").name == LONG_DOTTED_STEM + ".json"
    # The regression: stem + with_suffix ate ".2),yellow".
    assert replace_suffix(image, ".txt").name != (image.parent / image.stem).with_suffix(".txt").name


def test_numbered_name_keeps_dots_and_extension():
    assert numbered_name(LONG_DOTTED_IMAGE, 3) == f"{LONG_DOTTED_STEM} (3).png"
    assert numbered_name("a_1.2),yellow.txt", 1) == "a_1.2),yellow (1).txt"
    assert numbered_name("a_1.2),yellow", 1) == "a_1.2),yellow (1)"
    assert numbered_path(Path("D:/x/a_0.9.json"), 2).name == "a_0.9 (2).json"


def _runtime_with_roots(tmp_path: Path) -> tuple[Runtime, PathAllowlist, Path, Path]:
    images = tmp_path / "images"
    images.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    allowlist = PathAllowlist()
    allowlist.register(images, kind="input", root_id="input")
    allowlist.register(out, kind="output", root_id="output", writable=True)
    runtime = Runtime.__new__(Runtime)
    runtime.allowlist = allowlist
    runtime.settings = SimpleNamespace(artifact_dir=tmp_path / "artifacts", project_root=tmp_path)
    return runtime, allowlist, images, out


def test_output_path_beside_source_matches_the_image_name(tmp_path):
    runtime, _allowlist, images, _out = _runtime_with_roots(tmp_path)
    image = images / LONG_DOTTED_IMAGE
    image.write_bytes(b"image")
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    job = db.create_job(
        "local",
        {"output": {"txt": True}},
        [{"image_id": "one", "relative_path": LONG_DOTTED_IMAGE, "source_root_id": "input"}],
    )
    item = db.list_items(job.id)[0]

    txt = runtime._output_path(item, job, ".txt")
    json_path = runtime._output_path(item, job, ".json")

    assert txt == images / (LONG_DOTTED_STEM + ".txt")
    assert json_path == images / (LONG_DOTTED_STEM + ".json")
    # Exactly the pairing the dataset scanner expects.
    assert txt.name == image.with_suffix(".txt").name
    db.close()


def test_output_path_into_output_root_matches_the_image_name(tmp_path):
    runtime, _allowlist, images, out = _runtime_with_roots(tmp_path)
    image = images / LONG_DOTTED_IMAGE
    image.write_bytes(b"image")
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    job = db.create_job(
        "local",
        {"output": {"txt": True, "root_id": "output"}},
        [{"image_id": "one", "relative_path": LONG_DOTTED_IMAGE, "source_root_id": "input"}],
    )
    item = db.list_items(job.id)[0]

    assert runtime._output_path(item, job, ".txt") == out / (LONG_DOTTED_STEM + ".txt")
    db.close()


def test_output_path_for_uploads_matches_the_artifact_name(tmp_path):
    runtime, _allowlist, images, _out = _runtime_with_roots(tmp_path)
    upload = images / LONG_DOTTED_IMAGE
    upload.write_bytes(b"image")
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    job = db.create_job(
        "local",
        {"output": {"txt": True}},
        [
            {
                "image_id": "one",
                "relative_path": LONG_DOTTED_IMAGE,
                "payload": {
                    "upload_path": str(upload),
                    "artifact_name": LONG_DOTTED_IMAGE,
                },
            }
        ],
    )
    item = db.list_items(job.id)[0]

    txt = runtime._output_path(item, job, ".txt")

    assert txt.name == LONG_DOTTED_STEM + ".txt"
    db.close()


def test_conflict_rename_keeps_dots_and_extension(tmp_path):
    runtime = Runtime.__new__(Runtime)
    existing = tmp_path / (LONG_DOTTED_STEM + ".txt")
    existing.write_text("first", encoding="utf-8")

    renamed, skipped = Runtime._conflict_path(runtime, existing, "rename")

    assert not skipped
    assert renamed.name == f"{LONG_DOTTED_STEM} (1).txt"


def test_local_flow_writes_txt_next_to_its_image(tmp_path):
    runtime, _allowlist, images, _out = _runtime_with_roots(tmp_path)
    image = images / LONG_DOTTED_IMAGE
    image.write_bytes(b"image bytes")
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    runtime.artifacts = ArtifactManager(db)
    job = db.create_job(
        "local",
        {
            "model_ids": ["model-one"],
            "output": {"txt": True, "json": True, "conflict": "validate-skip"},
        },
        [{"image_id": "one", "relative_path": LONG_DOTTED_IMAGE, "source_root_id": "input"}],
    )
    item = db.list_items(job.id)[0]
    prediction = LocalPrediction(
        tags=[
            TagItem(
                text="very_highres",
                category="quality",
                score=0.9,
                source="local",
                model_id="model-one",
            )
        ]
    )

    written = Runtime._write_local_result(runtime, item, job, image, prediction)

    assert written.status == "succeeded"
    produced = sorted(path.name for path in images.iterdir())
    assert produced == sorted(
        [LONG_DOTTED_IMAGE, LONG_DOTTED_STEM + ".json", LONG_DOTTED_STEM + ".txt"]
    )
    db.close()


def test_write_anima_pairs_json_and_txt_with_a_dotted_name(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    source = tmp_path / LONG_DOTTED_IMAGE
    source.write_bytes(b"source")
    job = db.create_job(
        "online", {"prompt_version": "1"}, [{"image_id": "one", "relative_path": source.name}]
    )
    item = db.list_items(job.id)[0]
    manager = ArtifactManager(db)
    output = tmp_path / "out"

    result = manager.write_anima(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        payload=_anima(),
        config_hash=job.config_hash,
        output_dir=output,
        write_txt=True,
    )

    assert result.json_path.name == LONG_DOTTED_STEM + ".json"
    assert result.txt_path is not None
    assert result.txt_path.name == LONG_DOTTED_STEM + ".txt"

    beside = manager.write_anima(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        payload=_anima(),
        config_hash=job.config_hash,
        write_txt=True,
    )
    assert beside.json_path == tmp_path / (LONG_DOTTED_STEM + ".json")
    assert beside.txt_path == tmp_path / (LONG_DOTTED_STEM + ".txt")
    db.close()


def test_write_anima_accepts_a_sidecar_relative_path_without_doubling(tmp_path):
    db = SQLiteStorage(tmp_path / "jobs.sqlite3")
    source = tmp_path / LONG_DOTTED_IMAGE
    source.write_bytes(b"source")
    job = db.create_job(
        "online", {"prompt_version": "1"}, [{"image_id": "one", "relative_path": source.name}]
    )
    item = db.list_items(job.id)[0]
    output = tmp_path / "out"

    result = ArtifactManager(db).write_anima(
        job_id=job.id,
        item_id=item.id,
        source_path=source,
        payload=_anima(),
        config_hash=job.config_hash,
        output_dir=output,
        relative_path=LONG_DOTTED_STEM + ".json",
    )

    assert result.json_path.name == LONG_DOTTED_STEM + ".json"
    db.close()


def test_standalone_writer_pairs_with_a_dotted_name(tmp_path):
    source = tmp_path / LONG_DOTTED_IMAGE
    source.write_bytes(b"source")

    json_path, txt_path = write_anima_artifacts(source, _anima(), write_txt=True)

    assert json_path.name == LONG_DOTTED_STEM + ".json"
    assert txt_path is not None and txt_path.name == LONG_DOTTED_STEM + ".txt"


def test_atomic_write_bytes_handles_a_maximal_length_name(tmp_path):
    # A name at the filesystem limit: the temporary file must not push it over.
    name = "x" * (MAX_NAME_BYTES - len(".txt")) + ".txt"
    destination = tmp_path / name

    written = atomic_write_bytes(destination, b"tags")

    assert written.name == name
    assert written.read_bytes() == b"tags"
    assert [path.name for path in tmp_path.iterdir()] == [name]
