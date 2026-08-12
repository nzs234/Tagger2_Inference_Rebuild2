"""Structural guard: no workflow response DTO may expose server-side paths.

The existing desensitisation tests assert on specific endpoint bodies. This one
inspects every response model, so a newly added endpoint that reuses a leaky
shape fails here rather than in review.
"""

import pytest
from pydantic import BaseModel
from tagger2.workflow import api as workflow_api

# Fields that describe server-side filesystem state or raw config blobs.
FORBIDDEN_FIELDS = frozenset(
    {
        "workspace_path",
        "config_json",
        "config_hash",
        "config_version",
        "backup_path",
        "absolute_path",
        "source_path",
        "output_path",
        "traceback",
        "stack_trace",
    }
)


def _response_models():
    for name in dir(workflow_api):
        value = getattr(workflow_api, name)
        if (
            isinstance(value, type)
            and issubclass(value, BaseModel)
            and value is not BaseModel
        ):
            yield name, value


def test_response_models_are_discovered():
    """Guard against the introspection silently matching nothing."""
    assert len(list(_response_models())) >= 5


@pytest.mark.parametrize("name,model", list(_response_models()))
def test_response_model_has_no_forbidden_field(name, model):
    leaked = sorted(set(model.model_fields) & FORBIDDEN_FIELDS)
    assert not leaked, f"{name} exposes server-side field(s): {leaked}"


@pytest.mark.parametrize("name,model", list(_response_models()))
def test_response_model_uses_root_id_for_roots(name, model):
    """A root must be identified by ID, never by a resolved filesystem path."""
    for field_name in model.model_fields:
        if field_name.endswith("_root"):
            pytest.fail(f"{name}.{field_name} should be a *_root_id reference")
