"""Structural guard: the workflow route table must not drift.

``create_workflow_router`` is a large closure factory, so a refactor can
silently drop or reorder a registration. This snapshot pins the exact ordered
``(method, path, endpoint name)`` table produced by the factory. If a route is
renamed, added, removed or re-registered in a different order, this test fails
and the snapshot below must be updated deliberately in the same commit.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

from tagger2.security import PathAllowlist
from tagger2.workflow.api import create_workflow_router
from tagger2.workflow.db import WorkflowDatabase
from tagger2.workflow.resources import WorkflowResourceCatalog

# Ordered snapshot taken from the pre-split factory. Order matters because
# FastAPI matches routes in registration order.
EXPECTED_ROUTE_TABLE: list[tuple[str, str, str]] = [
    ("POST", "/api/v1/workflows/path-bindings/preview", "preview_path_binding"),
    ("POST", "/api/v1/workflows/path-bindings", "bind_manual_paths"),
    ("GET", "/api/v1/workflows/capabilities", "get_capabilities"),
    ("GET", "/api/v1/workflows/resources", "list_resources"),
    ("POST", "/api/v1/workflows/resources/import/preview", "preview_resource_import"),
    ("POST", "/api/v1/workflows/resources/import/apply", "apply_resource_import"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/pause", "pause_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/resume", "resume_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/start", "start_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/cancel", "cancel_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/repair", "repair_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/recover", "recover_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/pin", "pin_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/restore", "restore_job"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/discard", "discard_job"),
    ("GET", "/api/v1/workflows/jobs/{job_id}/count-review", "list_count_review"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/count-review/resolve", "resolve_count_review"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/count-review/resolve-batch", "resolve_count_review_batch"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/count-review/confirm", "confirm_count_review"),
    ("GET", "/api/v1/workflows/jobs/{job_id}/token-review", "list_token_review"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/token-review/review", "review_token_budget"),
    ("POST", "/api/v1/workflows/jobs/{job_id}/token-review/confirm", "confirm_token_review"),
    ("POST", "/api/v1/workflows/jobs/preflight", "preflight_job"),
    ("POST", "/api/v1/workflows/jobs", "create_job"),
    ("GET", "/api/v1/workflows/jobs", "list_jobs"),
    ("GET", "/api/v1/workflows/jobs/{job_id}", "get_job_status"),
    ("GET", "/api/v1/workflows/jobs/{job_id}/report", "get_job_report"),
    ("GET", "/api/v1/workflows/jobs/{job_id}/issues", "list_job_issues"),
    ("GET", "/api/v1/workflows/jobs/{job_id}/events", "list_job_events"),
    ("GET", "/api/v1/workflows/jobs/{job_id}/events/stream", "stream_job_events"),
]


@pytest.fixture()
def workflow_router(tmp_path: Path) -> Iterator[APIRouter]:
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    allowlist = PathAllowlist()
    allowlist.register(tmp_path / "in", kind="input", root_id="in")
    allowlist.register(tmp_path / "out", kind="output", root_id="out", writable=True)
    database = WorkflowDatabase(tmp_path / "wf.sqlite3")
    yield create_workflow_router(
        allowlist,
        WorkflowResourceCatalog(tmp_path / "resources"),
        database=database,
    )


def _route_table(router: APIRouter) -> list[tuple[str, str, str]]:
    table: list[tuple[str, str, str]] = []
    for route in router.routes:
        assert isinstance(route, APIRoute), f"unexpected route type: {type(route)!r}"
        methods = sorted(route.methods)
        assert len(methods) == 1, f"route {route.path} must declare exactly one method"
        table.append((methods[0], route.path, route.name))
    return table


def test_router_prefix_and_tags(workflow_router: APIRouter) -> None:
    assert workflow_router.prefix == "/api/v1/workflows"
    assert workflow_router.tags == ["workflows"]


def test_route_table_matches_snapshot(workflow_router: APIRouter) -> None:
    observed = _route_table(workflow_router)
    assert observed == EXPECTED_ROUTE_TABLE


def test_route_table_has_no_duplicates(workflow_router: APIRouter) -> None:
    observed = _route_table(workflow_router)
    assert len(observed) == len(set(observed)) == len(EXPECTED_ROUTE_TABLE)
