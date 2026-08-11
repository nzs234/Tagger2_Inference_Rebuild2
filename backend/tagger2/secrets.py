"""Secret storage backed by environment variables and the OS credential store.

No implementation in this module writes API keys to project files.  The
keyring backend maps to Windows Credential Manager on the target platform;
environment variables remain a read-only deployment override.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping, MutableMapping, Protocol, Sequence


class SecretStoreError(RuntimeError):
    pass


class SecretStoreUnavailable(SecretStoreError):
    pass


def _clean_namespace(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_")
    if not clean:
        raise ValueError("secret namespace cannot be empty")
    return clean


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip().strip('"\'')
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def split_secret_pool(value: str | None) -> list[str]:
    if not value:
        return []
    # A single key can contain punctuation; only comma and line breaks are
    # treated as separators.
    return _dedupe(re.split(r"[,\r\n]+", value))


@dataclass(frozen=True, slots=True)
class SecretMetadata:
    configured: bool
    source: str | None = None
    key_suffix: str | None = None
    count: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "source": self.source,
            "key_suffix": self.key_suffix,
            "count": self.count,
        }


class SecretBackend(Protocol):
    name: str

    def get_many(self, namespace: str) -> list[str]: ...

    def set_many(self, namespace: str, values: Sequence[str]) -> None: ...

    def delete(self, namespace: str) -> None: ...


class EnvSecretStore:
    """Read provider key pools from ``TAGGER2_SECRET_<PROVIDER>``."""

    name = "environment"

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        prefix: str = "TAGGER2_SECRET_",
        writable: bool = False,
    ):
        self._environ = os.environ if environ is None else environ
        self.prefix = prefix
        self.writable = writable

    def variable_name(self, namespace: str) -> str:
        return f"{self.prefix}{_clean_namespace(namespace).upper()}"

    def get_many(self, namespace: str) -> list[str]:
        return split_secret_pool(self._environ.get(self.variable_name(namespace)))

    def get(self, namespace: str) -> str | None:
        values = self.get_many(namespace)
        return values[0] if values else None

    get_secret = get

    def set_many(self, namespace: str, values: Sequence[str]) -> None:
        if not self.writable or not isinstance(self._environ, MutableMapping):
            raise SecretStoreUnavailable("environment secret store is read-only")
        clean = _dedupe(values)
        if not clean:
            self.delete(namespace)
            return
        self._environ[self.variable_name(namespace)] = ",".join(clean)

    def set(self, namespace: str, value: str) -> None:
        self.set_many(namespace, [value])

    set_secret = set

    def delete(self, namespace: str) -> None:
        if not self.writable or not isinstance(self._environ, MutableMapping):
            raise SecretStoreUnavailable("environment secret store is read-only")
        self._environ.pop(self.variable_name(namespace), None)


class KeyringSecretStore:
    """Store encrypted credentials through Python keyring."""

    name = "keyring"

    def __init__(self, *, service_name: str = "Tagger2 Inference"):
        self.service_name = service_name

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - depends on platform env
            raise SecretStoreUnavailable("keyring is not installed") from exc
        return keyring

    def _account(self, namespace: str) -> str:
        return f"provider:{_clean_namespace(namespace).casefold()}"

    def get_many(self, namespace: str) -> list[str]:
        try:
            raw = self._keyring().get_password(self.service_name, self._account(namespace))
        except Exception as exc:  # keyring raises backend-specific exceptions
            raise SecretStoreUnavailable("credential store is unavailable") from exc
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [raw]
        if isinstance(parsed, list):
            return _dedupe([str(value) for value in parsed])
        return [raw]

    def get(self, namespace: str) -> str | None:
        values = self.get_many(namespace)
        return values[0] if values else None

    def set_many(self, namespace: str, values: Sequence[str]) -> None:
        clean = _dedupe(values)
        if not clean:
            self.delete(namespace)
            return
        # JSON permits a key pool while keeping the credential store entry
        # opaque to the application logs and database.
        value = json.dumps(clean, ensure_ascii=False)
        try:
            self._keyring().set_password(self.service_name, self._account(namespace), value)
        except Exception as exc:
            raise SecretStoreUnavailable("credential store is unavailable") from exc

    def set(self, namespace: str, value: str) -> None:
        self.set_many(namespace, [value])

    def delete(self, namespace: str) -> None:
        try:
            keyring = self._keyring()
            existing = keyring.get_password(self.service_name, self._account(namespace))
            if existing is not None:
                keyring.delete_password(self.service_name, self._account(namespace))
        except SecretStoreUnavailable:
            raise
        except Exception as exc:
            raise SecretStoreUnavailable("credential store is unavailable") from exc


class CompositeSecretStore:
    """Environment override plus writable OS credential backend."""

    def __init__(
        self,
        *,
        environment: EnvSecretStore | None = None,
        keyring_store: KeyringSecretStore | None = None,
    ):
        self.environment = environment or EnvSecretStore()
        self.keyring = keyring_store or KeyringSecretStore()

    def get_many(self, namespace: str) -> list[str]:
        env_values = self.environment.get_many(namespace)
        if env_values:
            return env_values
        try:
            return self.keyring.get_many(namespace)
        except SecretStoreUnavailable:
            return []

    def get(self, namespace: str) -> str | None:
        values = self.get_many(namespace)
        return values[0] if values else None

    get_secret = get

    def set_many(self, namespace: str, values: Sequence[str]) -> None:
        self.keyring.set_many(namespace, values)

    def set(self, namespace: str, value: str) -> None:
        self.set_many(namespace, [value])

    set_secret = set

    def delete(self, namespace: str) -> None:
        self.keyring.delete(namespace)

    delete_secret = delete

    def metadata(self, namespace: str) -> SecretMetadata:
        env_values = self.environment.get_many(namespace)
        if env_values:
            return _metadata(env_values, source=self.environment.name)
        try:
            values = self.keyring.get_many(namespace)
        except SecretStoreUnavailable:
            values = []
        return _metadata(values, source=self.keyring.name if values else None)


def _metadata(values: Sequence[str], *, source: str | None) -> SecretMetadata:
    clean = _dedupe(values)
    suffix = None
    if clean:
        # Never reveal a short credential in full. Normal provider keys are
        # long enough for the final four characters to be useful in the UI.
        suffix = clean[0][-4:] if len(clean[0]) > 4 else "****"
    return SecretMetadata(
        configured=bool(clean),
        source=source if clean else None,
        key_suffix=suffix,
        count=len(clean),
    )


# Conventional public name used by the provider service.
SecretStore = CompositeSecretStore


def get_secret_metadata(store: CompositeSecretStore, namespace: str) -> dict[str, object]:
    """Return the only secret information permitted in an API response."""

    return store.metadata(namespace).as_dict()


__all__ = [
    "SecretStoreError",
    "SecretStoreUnavailable",
    "SecretMetadata",
    "SecretBackend",
    "EnvSecretStore",
    "KeyringSecretStore",
    "CompositeSecretStore",
    "SecretStore",
    "split_secret_pool",
    "get_secret_metadata",
]
