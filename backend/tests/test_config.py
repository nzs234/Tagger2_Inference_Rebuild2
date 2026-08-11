from pathlib import Path

from tagger2.config import AppConfig


def _write_config(project: Path, text: str) -> Path:
    path = project / "config" / "app.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_use_new_port_and_migrated_cache(tmp_path: Path) -> None:
    config = AppConfig.from_env({"TAGGER2_PROJECT_ROOT": str(tmp_path)})

    assert config.port == 20000
    assert config.host == "127.0.0.1"
    assert config.cache_dir == (tmp_path / "data_cache").resolve()


def test_toml_is_loaded_with_project_relative_paths(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
[server]
host = "localhost"
port = 21000
debug = true

[paths]
cache_dir = "shared-cache"
allowed_input_roots = ["incoming"]
allowed_output_roots = ["outgoing"]

[limits]
max_pixels = 2000000

[runtime]
max_loaded_models = 3
""",
    )
    config = AppConfig.from_env(
        {
            "TAGGER2_PROJECT_ROOT": str(tmp_path),
            "TAGGER2_CONFIG": str(path),
        }
    )

    assert config.port == 21000
    assert config.production is False
    assert config.cache_dir == (tmp_path / "shared-cache").resolve()
    assert config.max_image_pixels == 2_000_000
    assert config.max_loaded_models == 3
    assert [root.kind for root in config.roots] == ["input", "output"]
    assert config.roots[0].path == (tmp_path / "incoming").resolve()


def test_environment_overrides_toml(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
[server]
host = "127.0.0.1"
port = 21000
debug = true
""",
    )
    config = AppConfig.from_env(
        {
            "TAGGER2_PROJECT_ROOT": str(tmp_path),
            "TAGGER2_CONFIG": str(path),
            "TAGGER2_PORT": "22000",
            "TAGGER2_PRODUCTION": "true",
            "TAGGER2_CACHE_DIR": "environment-cache",
        }
    )

    assert config.port == 22000
    assert config.production is True
    assert config.cache_dir == (tmp_path / "environment-cache").resolve()
