"""Per-router dependency object shared by the workflow API route modules."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .db import WorkflowDatabase
from .preflight import WorkflowPreflightService
from .resources import WorkflowResourceCatalog
from ..security import PathAllowlist


@dataclass
class WorkflowRouteContext:
    """Dependencies captured once by :func:`tagger2.workflow.api.create_workflow_router`.

    Route modules receive this object instead of closing over factory locals,
    so every moved helper names its dependency explicitly.  ``registry``,
    ``engine`` and ``storage`` stay optional because the workflow must remain
    constructible without a host model runtime (see the factory docstring).
    """

    allowlist: PathAllowlist
    resources: WorkflowResourceCatalog
    database: WorkflowDatabase
    registry: Any | None
    engine: Any | None
    storage: Any | None
    root_registrar: Callable[..., Any] | None
    token_counter: Callable[[Sequence[str]], Sequence[int]] | None
    preflight_service: WorkflowPreflightService
