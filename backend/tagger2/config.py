"""Application configuration with secure local-first defaults."""

from __future__ import annotations

import ipaddress
import os
import tomllib
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"}
)


def _env_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}


def _project_path(value: Any, project_root: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return (project_root / path if not path.is_absolute() else path).resolve(strict=False)


def _read_toml_config(config_path: Path, project_root: Path) -> dict[str, Any]:
    """Translate the public TOML profile into :class:`AppConfig` fields."""

    if not config_path.is_file():
        return {}
    try:
        with config_path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read configuration file {config_path}: {exc}") from exc

    server = document.get("server", {})
    paths = document.get("paths", {})
    limits = document.get("limits", {})
    runtime = document.get("runtime", {})
    for name, section in (
        ("server", server),
        ("paths", paths),
        ("limits", limits),
        ("runtime", runtime),
    ):
        if not isinstance(section, Mapping):
            raise ValueError(f"configuration section [{name}] must be a table")

    values: dict[str, Any] = {}
    server_fields = {
        "host": "host",
        "port": "port",
        "lan_access": "allow_lan",
        "allow_lan": "allow_lan",
        "production": "production",
        "access_token_env": "access_token_env",
        "allow_local_providers": "allow_local_providers",
    }
    for source_name, field_name in server_fields.items():
        if source_name in server:
            values[field_name] = server[source_name]
    if "debug" in server and "production" not in server:
        values["production"] = not bool(server["debug"])
    if str(server.get("auth_token") or "").strip():
        warnings.warn(
            "[server].auth_token is ignored; set the environment variable named by "
            "[server].access_token_env instead",
            stacklevel=2,
        )

    path_fields = {
        "data_dir": "data_dir",
        "database_path": "database_path",
        "upload_dir": "upload_dir",
        "artifact_dir": "artifact_dir",
        "log_dir": "log_dir",
        "cache_dir": "cache_dir",
    }
    for source_name, field_name in path_fields.items():
        value = paths.get(source_name)
        if value not in (None, ""):
            values[field_name] = _project_path(value, project_root)

    roots: list[dict[str, Any]] = []
    for kind, writable, source_name in (
        ("input", False, "allowed_input_roots"),
        ("output", True, "allowed_output_roots"),
    ):
        configured = paths.get(source_name, [])
        if not isinstance(configured, list):
            raise ValueError(f"[paths].{source_name} must be an array")
        for index, value in enumerate(configured, start=1):
            path = _project_path(value, project_root)
            roots.append(
                {
                    "root_id": f"config-{kind}-{index}",
                    "path": path,
                    "label": path.name or f"{kind.title()} root {index}",
                    "kind": kind,
                    "writable": writable,
                }
            )
    if roots:
        values["roots"] = roots

    limit_fields = {
        "max_upload_bytes": "max_upload_bytes",
        "max_request_bytes": "max_request_bytes",
        "max_pixels": "max_image_pixels",
        "max_image_pixels": "max_image_pixels",
        "max_image_edge": "max_image_edge",
        "max_online_edge": "online_image_edge",
        "online_image_edge": "online_image_edge",
        "online_image_bytes": "online_image_bytes",
        "max_batch_items": "max_batch_items",
        "max_online_concurrency": "max_online_concurrency",
        "scan_page_size_max": "scan_page_size_max",
    }
    for source_name, field_name in limit_fields.items():
        if source_name in limits:
            values[field_name] = limits[source_name]

    runtime_fields = {
        "max_loaded_models": "max_loaded_models",
        "model_memory_budget_mb": "model_memory_budget_mb",
        "allow_unsafe_pickle": "allow_unsafe_pickle",
        "gpu_concurrency": "gpu_concurrency",
    }
    for source_name, field_name in runtime_fields.items():
        if source_name in runtime:
            values[field_name] = runtime[source_name]
    return values


class RootSettings(BaseModel):
    """Server-side path root; its path must never be returned by the API."""

    model_config = ConfigDict(extra="forbid")

    root_id: str = Field(min_length=1, max_length=128)
    path: Path
    label: str = Field(min_length=1, max_length=256)
    kind: str = Field(pattern=r"^(input|output|model|upload)$")
    writable: bool = False

    @field_validator("path", mode="before")
    @classmethod
    def _expand_path(cls, value: Any) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(str(value))))


class AppConfig(BaseModel):
    """Runtime settings.

    Secrets are referenced by environment variable name and are intentionally
    absent from this model.  API keys are managed by :mod:`tagger2.secrets`.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    project_root: Path = PROJECT_ROOT
    data_dir: Path | None = None
    database_path: Path | None = None
    upload_dir: Path | None = None
    artifact_dir: Path | None = None
    log_dir: Path | None = None
    cache_dir: Path | None = None

    host: str = "127.0.0.1"
    port: int = Field(default=20000, ge=1, le=65535)
    production: bool = True
    allow_lan: bool = False
    access_token_env: str = "TAGGER2_ACCESS_TOKEN"
    allow_local_providers: bool = False

    max_upload_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_request_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    max_image_pixels: int = Field(default=80_000_000, ge=1_000_000)
    max_image_edge: int = Field(default=16_384, ge=256)
    online_image_edge: int = Field(default=2048, ge=256, le=16_384)
    online_image_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024)
    scan_page_size_max: int = Field(default=2000, ge=1, le=20_000)
    max_batch_items: int = Field(default=100_000, ge=1, le=1_000_000)
    max_online_concurrency: int = Field(default=16, ge=1, le=128)
    job_log_limit: int = Field(default=2000, ge=100, le=100_000)
    gpu_concurrency: int = Field(default=1, ge=1, le=8)
    max_loaded_models: int = Field(default=2, ge=1, le=32)
    model_memory_budget_mb: int | None = Field(default=None, ge=256)
    allow_unsafe_pickle: bool = False
    image_extensions: frozenset[str] = DEFAULT_IMAGE_EXTENSIONS
    roots: list[RootSettings] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _derive_paths(cls, raw: Any) -> Any:
        values = dict(raw or {})
        root = Path(values.get("project_root") or PROJECT_ROOT).expanduser()
        data = Path(values.get("data_dir") or root / "data")
        values["project_root"] = root
        values["data_dir"] = data
        values.setdefault("database_path", data / "tagger2.sqlite3")
        values.setdefault("upload_dir", data / "uploads")
        values.setdefault("artifact_dir", data / "artifacts")
        values.setdefault("log_dir", data / "logs")
        # The migrated Hugging Face/model cache lives outside the runtime data
        # directory.  Keeping this default stable also prevents a first launch
        # from silently creating a second multi-gigabyte cache tree.
        values.setdefault("cache_dir", root / "data_cache")
        return values

    @field_validator(
        "project_root",
        "data_dir",
        "database_path",
        "upload_dir",
        "artifact_dir",
        "log_dir",
        "cache_dir",
        mode="before",
    )
    @classmethod
    def _normalise_path(cls, value: Any) -> Path | None:
        if value is None:
            return None
        return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve(strict=False)

    @field_validator("image_extensions", mode="before")
    @classmethod
    def _normalise_extensions(cls, value: Any) -> frozenset[str]:
        values = value or DEFAULT_IMAGE_EXTENSIONS
        if isinstance(values, str):
            values = [item for item in values.split(",") if item.strip()]
        return frozenset(
            extension if str(extension).startswith(".") else f".{extension}"
            for extension in (str(item).strip().casefold() for item in values)
            if extension and extension != "."
        )

    @model_validator(mode="after")
    def _validate_network_boundary(self) -> "AppConfig":
        local_names = {"localhost", "localhost.localdomain"}
        is_loopback = self.host.casefold() in local_names
        try:
            address = ipaddress.ip_address(self.host)
            is_loopback = is_loopback or address.is_loopback
        except ValueError:
            pass
        if not self.allow_lan and not is_loopback:
            raise ValueError("non-loopback host requires allow_lan=true")
        if self.allow_lan and not self.access_token_env.strip():
            raise ValueError("LAN access requires an access-token environment variable")
        return self

    @property
    def docs_enabled(self) -> bool:
        return not self.production

    @property
    def access_token_configured(self) -> bool:
        return bool(os.getenv(self.access_token_env, "").strip())

    def validate_runtime(self) -> None:
        if self.allow_lan and not self.access_token_configured:
            raise RuntimeError(
                f"LAN access is enabled but {self.access_token_env} is not configured"
            )

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.upload_dir,
            self.artifact_dir,
            self.log_dir,
            self.cache_dir,
        ):
            if path is not None:
                path.mkdir(parents=True, exist_ok=True)
        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AppConfig":
        """Build settings from ``config/app.toml`` and environment overrides.

        TOML is intentionally limited to non-secret operational settings.  An
        environment variable always wins, which keeps deployments able to
        override a checked-in profile without editing files.  Relative paths
        in TOML are resolved against ``project_root`` rather than the process
        working directory.
        """
        source = os.environ if env is None else env

        project_root = Path(
            source.get("TAGGER2_PROJECT_ROOT", PROJECT_ROOT)
        ).expanduser().resolve(strict=False)
        config_name = source.get("TAGGER2_CONFIG")
        config_path = (
            Path(config_name).expanduser()
            if config_name
            else project_root / "config" / "app.toml"
        )
        if not config_path.is_absolute():
            config_path = project_root / config_path

        values: dict[str, Any] = {"project_root": project_root}
        values.update(_read_toml_config(config_path, project_root))

        def optional_int(name: str) -> int | None:
            value = source.get(name)
            return int(value) if value and value.strip() else None

        # Environment values are applied field-by-field so omitted variables
        # do not erase values loaded from TOML.
        direct_values: dict[str, Any] = {
            "host": source.get("TAGGER2_HOST"),
            "port": int(source["TAGGER2_PORT"]) if source.get("TAGGER2_PORT") else None,
            "production": (
                _env_bool(source["TAGGER2_PRODUCTION"])
                if source.get("TAGGER2_PRODUCTION") is not None
                else None
            ),
            "allow_lan": (
                _env_bool(source["TAGGER2_ALLOW_LAN"])
                if source.get("TAGGER2_ALLOW_LAN") is not None
                else None
            ),
            "allow_local_providers": (
                _env_bool(source["TAGGER2_ALLOW_LOCAL_PROVIDERS"])
                if source.get("TAGGER2_ALLOW_LOCAL_PROVIDERS") is not None
                else None
            ),
            "allow_unsafe_pickle": (
                _env_bool(source["TAGGER2_ALLOW_UNSAFE_PICKLE"])
                if source.get("TAGGER2_ALLOW_UNSAFE_PICKLE") is not None
                else None
            ),
            "access_token_env": source.get("TAGGER2_ACCESS_TOKEN_ENV"),
            "model_memory_budget_mb": optional_int("TAGGER2_MODEL_MEMORY_BUDGET_MB"),
        }
        values.update({key: value for key, value in direct_values.items() if value is not None})
        path_names = {
            "data_dir": "TAGGER2_DATA_DIR",
            "database_path": "TAGGER2_DATABASE_PATH",
            "upload_dir": "TAGGER2_UPLOAD_DIR",
            "artifact_dir": "TAGGER2_ARTIFACT_DIR",
            "log_dir": "TAGGER2_LOG_DIR",
            "cache_dir": "TAGGER2_CACHE_DIR",
        }
        for field_name, env_name in path_names.items():
            if source.get(env_name):
                values[field_name] = _project_path(source[env_name], project_root)
        return cls.model_validate(values)


# ``Settings`` is kept as a conventional import name for FastAPI dependencies.
Settings = AppConfig


@lru_cache(maxsize=1)
def get_settings() -> AppConfig:
    return AppConfig.from_env()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


get_config = get_settings


def configure_cache_environment(settings: AppConfig | None = None) -> None:
    """Point ML libraries at the project's non-secret cache directory."""

    current = settings or get_settings()
    cache = current.cache_dir or current.project_root / "data_cache"
    os.environ.setdefault("HF_HOME", str(cache / "huggingface"))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(cache / "transformers"))
    os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", str(cache / "torchinductor"))
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


__all__ = [
    "PROJECT_ROOT",
    "DEFAULT_IMAGE_EXTENSIONS",
    "RootSettings",
    "AppConfig",
    "Settings",
    "get_settings",
    "get_config",
    "reset_settings_cache",
    "configure_cache_environment",
]
