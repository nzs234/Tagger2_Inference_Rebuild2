"""The NL-translation provider fallback must ignore providers without a key."""

from types import SimpleNamespace

from tagger2.main import Runtime


class FakeStorage:
    def __init__(self, profiles):
        self._profiles = profiles

    def list_provider_profiles(self):
        return list(self._profiles)


def _runtime(profiles, configured_refs):
    runtime = object.__new__(Runtime)
    runtime.storage = FakeStorage(profiles)  # type: ignore[attr-defined]
    runtime.secrets = SimpleNamespace(  # type: ignore[attr-defined]
        metadata=lambda ref: {"configured": ref in configured_refs}
    )
    return runtime


def test_only_enabled_providers_with_a_credential_are_offered(monkeypatch):
    profiles = [
        {"id": "no-key", "enabled": True},
        {"id": "disabled", "enabled": False},
        {"id": "ready", "enabled": True},
        {"id": "ready-custom-ref", "enabled": True, "secret_ref": "custom"},
    ]
    configured = {"provider_ready", "custom"}
    monkeypatch.setattr(
        "tagger2.main.get_secret_metadata",
        lambda _store, ref: {"configured": ref in configured},
    )

    assert _runtime(profiles, configured)._enabled_provider_ids() == [
        "ready",
        "ready-custom-ref",
    ]


def test_no_configured_provider_yields_an_empty_list(monkeypatch):
    monkeypatch.setattr(
        "tagger2.main.get_secret_metadata", lambda _store, _ref: {"configured": False}
    )

    assert _runtime([{"id": "seeded-default", "enabled": True}], set())._enabled_provider_ids() == []
