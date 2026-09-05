"""Behavior tests and offline smoke for the wiki CLI scripts.

``scripts/build_tag_wiki.py`` is loaded via importlib (it is a plain script,
not a package) and its module-level ``app`` is swapped for a stub exposing a
fake tag-wiki service, so every command path, progress render and exit code
is exercised without network access, model downloads or a real runtime. The
real app boot itself stays covered by ``test_cli_module_boots_real_app`` and
``scripts/snapshot_wiki_databases.py`` (release packaging) by a fixture
database built with the real :class:`WikiStore`.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tagger2.tag_wiki.contracts import BuildRequest, TranslateRequest
from tagger2.tag_wiki.service import TagWikiError

ROOT = Path(__file__).resolve().parents[2]
CLI_SCRIPT = ROOT / "scripts" / "build_tag_wiki.py"
SNAPSHOT_SCRIPT = ROOT / "scripts" / "snapshot_wiki_databases.py"


def _load_script(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_cli_module() -> Any:
    return _load_script(CLI_SCRIPT, "build_tag_wiki_cli_test")


class FakeWikiService:
    """Duck-typed TagWikiService covering the CLI's public surface."""

    def __init__(
        self,
        *,
        start_error: TagWikiError | None = None,
        build_final: dict[str, Any] | None = None,
        translate_final: dict[str, Any] | None = None,
        translate_noop: bool = False,
        hang_build: bool = False,
        status_interrupt: BaseException | None = None,
    ) -> None:
        self.build_requests: list[BuildRequest] = []
        self.translate_requests: list[TranslateRequest] = []
        self.closed = False
        self._start_error = start_error
        self._build_final = build_final or {"state": "idle", "phase": "done", "message": "构建完成"}
        self._translate_final = translate_final or {
            "state": "idle",
            "done": 3,
            "failed": 0,
            "total": 3,
            "message": "翻译完成",
        }
        self._translate_noop = translate_noop
        self._hang_build = hang_build
        self._status_interrupt = status_interrupt
        self._build_state: dict[str, Any] = {"state": "idle", "phase": "idle", "message": ""}
        self._translate_state: dict[str, Any] = {
            "state": "idle",
            "done": 0,
            "failed": 0,
            "total": 0,
            "message": "",
        }
        self._build_task: asyncio.Task[None] | None = None
        self._translate_task: asyncio.Task[None] | None = None

    # -- build ---------------------------------------------------------------

    async def start_build(self, request: BuildRequest) -> dict[str, Any]:
        self.build_requests.append(request)
        if self._start_error is not None:
            raise self._start_error
        self._build_state = {"state": "running", "phase": "download", "message": "开始构建"}

        async def run() -> None:
            if self._hang_build:
                # Simulate a long-running pipeline that never finishes.
                await asyncio.Event().wait()
            await asyncio.sleep(0)
            self._build_state = dict(self._build_final)

        self._build_task = asyncio.get_running_loop().create_task(run())
        return {"build": dict(self._build_state)}

    def build_task(self) -> asyncio.Task[None] | None:
        return self._build_task

    async def wait_build(self) -> dict[str, Any]:
        if self._build_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._build_task
        return dict(self._build_state)

    # -- translate -----------------------------------------------------------

    async def start_translate(self, request: TranslateRequest) -> dict[str, Any]:
        self.translate_requests.append(request)
        if self._start_error is not None:
            raise self._start_error
        if self._translate_noop:
            self._translate_state = {
                **self._translate_state,
                "state": "idle",
                "message": "范围内页面均已有中文摘要",
            }
            return dict(self._translate_state)
        self._translate_state = {**self._translate_state, "state": "running", "total": 3}

        async def run() -> None:
            await asyncio.sleep(0)
            self._translate_state = dict(self._translate_final)

        self._translate_task = asyncio.get_running_loop().create_task(run())
        return dict(self._translate_state)

    def translate_task(self) -> asyncio.Task[None] | None:
        return self._translate_task

    async def wait_translate(self) -> dict[str, Any]:
        if self._translate_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._translate_task
        return dict(self._translate_state)

    def translate_progress(self) -> dict[str, Any]:
        return dict(self._translate_state)

    # -- shared --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        if self._status_interrupt is not None:
            raise self._status_interrupt
        return {"build": dict(self._build_state), "translate": dict(self._translate_state)}

    async def aclose(self) -> None:
        self.closed = True
        for task in (self._build_task, self._translate_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _install_fake_service(monkeypatch: pytest.MonkeyPatch, service: FakeWikiService) -> Any:
    module = _load_cli_module()
    # The progress loops poll in real seconds; shrink them for tests.
    monkeypatch.setattr(module, "BUILD_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(module, "TRANSLATE_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(
        module,
        "app",
        SimpleNamespace(state=SimpleNamespace(runtime=SimpleNamespace(tag_wiki=service))),
    )
    return module


# -- argument surface ----------------------------------------------------------


def test_skip_reindex_help_documents_real_semantics() -> None:
    """The help text no longer implies only dump parsing is skipped: the dump
    refresh check, pruning, model check and vector pass still run."""
    module = _load_cli_module()
    help_text = module._build_parser().format_help()
    assert "skip re-importing the dump" in help_text
    assert "--no-download" in help_text


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--build"], dict(download_dump=True, reindex=True, force_reembed=False)),
        (
            ["--build", "--skip-reindex"],
            dict(download_dump=True, reindex=False, force_reembed=False),
        ),
        (
            ["--build", "--no-download", "--force-reembed"],
            dict(download_dump=False, reindex=True, force_reembed=True),
        ),
    ],
)
def test_flags_map_to_build_request(argv: list[str], expected: dict[str, Any]) -> None:
    module = _load_cli_module()
    args = module._build_parser().parse_args(argv)
    request = BuildRequest(
        download_dump=not args.no_download,
        reindex=not args.skip_reindex,
        force_reembed=args.force_reembed,
    )
    assert request.download_dump is expected["download_dump"]
    assert request.reindex is expected["reindex"]
    assert request.force_reembed is expected["force_reembed"]


# -- build command ---------------------------------------------------------------


def test_build_success_streams_phases_and_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService()
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--build"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 0
    assert service.build_requests and service.build_requests[0].profile == "e621"
    out = capsys.readouterr().out
    assert json.loads(out.splitlines()[0])["state"] == "running"
    assert json.loads(out.splitlines()[-1])["phase"] == "done"


def test_build_skips_reindex_in_request(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeWikiService()
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--build", "--skip-reindex", "--no-download"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 0
    request = service.build_requests[0]
    assert request.reindex is False
    assert request.download_dump is False


def test_build_busy_exits_two_without_traceback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService(
        start_error=TagWikiError("已有一次构建在进行中", code="wiki_busy", status_code=409)
    )
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--build"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "cannot start" in captured.err
    assert "已有一次构建在进行中" in captured.err


def test_build_error_state_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService(
        build_final={"state": "error", "phase": "embed", "message": "构建失败", "error": "boom"}
    )
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--build"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 1
    assert "FAILED: boom" in capsys.readouterr().err


# -- translate command -----------------------------------------------------------


def test_translate_success_streams_progress_and_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService()
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--translate", "--scope", "popular", "--concurrency", "2"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 0
    request = service.translate_requests[0]
    assert request.scope == "popular"
    assert request.concurrency == 2
    out = capsys.readouterr().out
    assert json.loads(out.splitlines()[-1])["done"] == 3
    assert "3/3 done" in out


def test_translate_nothing_to_do_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService(translate_noop=True)
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--translate"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "nothing to do" in captured.out
    assert "范围内页面均已有中文摘要" in captured.out


def test_translate_busy_exits_two(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService(
        start_error=TagWikiError("已有一次翻译任务在进行中", code="wiki_busy", status_code=409)
    )
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--translate"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 2
    assert "cannot start" in capsys.readouterr().err


def test_translate_error_state_exits_one(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService(
        translate_final={
            "state": "error",
            "done": 1,
            "failed": 2,
            "total": 3,
            "message": "翻译任务失败",
            "error": "provider down",
        }
    )
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--translate"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 1
    assert "FAILED: provider down" in capsys.readouterr().err


# -- status / interruption ---------------------------------------------------------


def test_status_prints_document_and_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    service = FakeWikiService()
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--status"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["build"]["state"] == "idle"


def test_sigint_cancels_service_and_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    """First Ctrl-C: asyncio.Runner cancels the CLI's main task, so the
    handler must cancel the background jobs on the still-open event loop and
    report the conventional 130. The hanging build task is cancelled too."""
    service = FakeWikiService(hang_build=True)
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--build"])

    async def scenario() -> int:
        main_task = asyncio.get_running_loop().create_task(module._main(args))
        await asyncio.sleep(0.05)  # let the CLI enter its build progress loop
        main_task.cancel()  # what asyncio.Runner does on SIGINT
        return await main_task

    exit_code = asyncio.run(scenario())

    assert exit_code == 130
    assert service.closed is True
    assert service.build_task() is not None
    assert service.build_task().done()


def test_keyboard_interrupt_in_command_loop_closes_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """A KeyboardInterrupt surfacing inside the command loop is caught by the
    same handler instead of leaving background jobs behind."""
    service = FakeWikiService(status_interrupt=KeyboardInterrupt())
    module = _install_fake_service(monkeypatch, service)
    args = module._build_parser().parse_args(["--status"])

    exit_code = asyncio.run(module._main(args))

    assert exit_code == 130
    assert service.closed is True


# -- real app / release packaging smoke (offline) -----------------------------------


def test_cli_module_boots_real_app_offline() -> None:
    """Smoke: importing the CLI boots the real Runtime (SQLite migrations,
    resource catalog, tag-wiki wiring) without touching the network."""
    module = _load_cli_module()
    status = module.app.state.runtime.tag_wiki.status()
    assert status["build"]["state"] == "idle"
    assert set(status["profiles"]) == {"e621", "danbooru"}


def test_snapshot_wiki_databases_release_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Release packaging: VACUUM INTO copies a built wiki database and skips
    missing profiles gracefully; with no databases at all it fails loudly."""

    from tagger2.tag_wiki.wiki_store import WikiStore

    data_dir = tmp_path / "data"
    store = WikiStore(data_dir / "tag_wiki" / "tag_wiki.sqlite3")
    try:
        store.upsert_page(
            {
                "title": "hug",
                "display_title": "hug",
                "body_md": "h2. Usage\nUse for hugging.",
                "wiki_id": 1,
                "updated_at": "2026-01-01T00:00:00Z",
                "url": "https://e621.net/wiki_pages/1",
                "sections": [{"heading": "Usage", "text": "Use for hugging."}],
                "links": [],
            }
        )
    finally:
        store.close()

    module = _load_script(SNAPSHOT_SCRIPT, "snapshot_wiki_databases_test")
    dest = tmp_path / "release"
    monkeypatch.setattr(
        sys,
        "argv",
        ["snapshot_wiki_databases.py", "--data-dir", str(data_dir), "--dest", str(dest)],
    )
    module.main()

    copied = dest / "tag_wiki.sqlite3"
    assert copied.is_file() and copied.stat().st_size > 0
    with sqlite3.connect(copied) as conn:
        assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 1

    # Nothing built at all -> the packaging CLI refuses to ship an empty set.
    empty_dest = tmp_path / "release-empty"
    monkeypatch.setattr(
        sys,
        "argv",
        ["snapshot_wiki_databases.py", "--data-dir", str(tmp_path / "nope"), "--dest", str(empty_dest)],
    )
    with pytest.raises(SystemExit):
        module.main()
