from __future__ import annotations

import tomllib
from pathlib import Path

import platformdirs
import pytest
from pydantic import ValidationError

from tweetxvault.config import (
    AppConfig,
    AuthConfig,
    DatabaseConfig,
    TaggingConfig,
    WebConfig,
    XDGPaths,
    ensure_paths,
    get_config_ui_schema,
    load_config,
    resolve_paths,
    save_app_config,
)


def _xdg_env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config-home"),
        "XDG_DATA_HOME": str(tmp_path / "data-home"),
        "XDG_CACHE_HOME": str(tmp_path / "cache-home"),
    }


def test_resolve_paths_honors_explicit_xdg_roots(tmp_path: Path) -> None:
    paths = resolve_paths(_xdg_env(tmp_path))

    assert paths.config_dir == tmp_path / "config-home" / "tweetxvault"
    assert paths.data_dir == tmp_path / "data-home" / "tweetxvault"
    assert paths.cache_dir == tmp_path / "cache-home" / "tweetxvault"
    assert paths.database_file == paths.database_path == paths.data_dir / "archive.db"
    assert paths.media_dir == paths.data_dir / "media"


def test_resolve_paths_uses_platform_defaults_for_explicit_empty_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platformdirs, "user_config_dir", lambda _name: "/platform/config")
    monkeypatch.setattr(platformdirs, "user_data_dir", lambda _name: "/platform/data")
    monkeypatch.setattr(platformdirs, "user_cache_dir", lambda _name: "/platform/cache")

    paths = resolve_paths({})

    assert paths.config_dir == Path("/platform/config")
    assert paths.data_dir == Path("/platform/data")
    assert paths.cache_dir == Path("/platform/cache")


def test_ensure_paths_creates_all_application_directories(tmp_path: Path) -> None:
    paths = XDGPaths(
        config_dir=tmp_path / "nested" / "config",
        data_dir=tmp_path / "nested" / "data",
        cache_dir=tmp_path / "nested" / "cache",
    )

    assert ensure_paths(paths) is paths
    assert all(path.is_dir() for path in (paths.config_dir, paths.data_dir, paths.cache_dir))


def test_load_config_creates_default_database_section_idempotently(tmp_path: Path) -> None:
    env = _xdg_env(tmp_path)

    first, paths = load_config(env)
    first_text = paths.config_file.read_text(encoding="utf-8")
    second, _ = load_config(env)

    assert first.database == DatabaseConfig()
    assert second == first
    assert first_text == paths.config_file.read_text(encoding="utf-8")
    assert first_text.count("[database]") == 1


def test_load_config_migrates_existing_file_without_database_section(tmp_path: Path) -> None:
    env = _xdg_env(tmp_path)
    paths = ensure_paths(resolve_paths(env))
    paths.config_file.write_text(
        "# keep this comment\n[web]\nport = 9123\n",
        encoding="utf-8",
    )

    config, _ = load_config(env)
    migrated = paths.config_file.read_text(encoding="utf-8")

    assert config.web.port == 9123
    assert config.database == DatabaseConfig()
    assert "# keep this comment" in migrated
    assert migrated.count("[database]") == 1
    load_config(env)
    assert paths.config_file.read_text(encoding="utf-8") == migrated


def test_load_config_applies_environment_overrides_after_toml(tmp_path: Path) -> None:
    env = _xdg_env(tmp_path)
    paths = ensure_paths(resolve_paths(env))
    paths.config_file.write_text(
        """
[auth]
auth_token = "file-token"
browser = "firefox"

[sync]
page_delay = 9
max_retries = 8
max_linked_depth = 2

[database]
cache_size_kb = 32
mmap_size_bytes = 64
""".lstrip(),
        encoding="utf-8",
    )
    env.update(
        {
            "TWEETXVAULT_AUTH_TOKEN": "env-token",
            "TWEETXVAULT_PAGE_DELAY": "1.5",
            "TWEETXVAULT_MAX_RETRIES": "4",
            "TWEETXVAULT_MAX_LINKED_DEPTH": "7",
        }
    )

    config, _ = load_config(env)

    assert config.auth.auth_token == "env-token"
    assert config.auth.browser == "firefox"
    assert config.sync.page_delay == 1.5
    assert config.sync.max_retries == 4
    assert config.sync.max_linked_depth == 7
    assert config.database.cache_size_kb == 32


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TWEETXVAULT_PAGE_DELAY", "not-a-float"),
        ("TWEETXVAULT_MAX_RETRIES", "2.5"),
    ],
)
def test_load_config_rejects_invalid_numeric_environment_values(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    env = _xdg_env(tmp_path)
    env[name] = value

    with pytest.raises(ValueError):
        load_config(env)


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (WebConfig, {"host": ""}),
        (WebConfig, {"port": 0}),
        (WebConfig, {"port": 65536}),
        (DatabaseConfig, {"cache_size_kb": -1}),
        (DatabaseConfig, {"mmap_size_bytes": -1}),
        (TaggingConfig, {"model": ""}),
        (TaggingConfig, {"thinking_level": "extreme"}),
        (TaggingConfig, {"limit": 0}),
        (TaggingConfig, {"rpd": 0}),
        (TaggingConfig, {"max_media_size_mb": 0}),
    ],
)
def test_new_config_models_reject_invalid_values(model, kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_save_app_config_round_trips_defaults_and_escaped_strings(paths: XDGPaths) -> None:
    config = AppConfig(
        auth=AuthConfig(
            auth_token='slash\\quote"line\nnext',
            browser_profile_path="C:\\Profiles\\Main",
        ),
        web=WebConfig(host="localhost", port=8765),
        tagging=TaggingConfig(api_key='secret"value', thinking_level="low", rpd=25),
    )

    save_app_config(paths, config)

    with paths.config_file.open("rb") as handle:
        raw = tomllib.load(handle)
    loaded = AppConfig.model_validate(raw)

    assert raw["auth"]["auth_token"] == config.auth.auth_token
    assert loaded == config
    assert "[database]" in paths.config_file.read_text(encoding="utf-8")


def test_config_ui_schema_only_references_real_fields_and_masks_secrets() -> None:
    schema = get_config_ui_schema()
    config_dump = AppConfig().model_dump()

    def exists(path: str) -> bool:
        section, field = path.split(".", 1)
        return section in config_dump and field in config_dump[section]

    basic = set(schema["whitelist"])
    blocked = set(schema["blacklist"])
    editable = {
        f"{section}.{field}" for section, fields in config_dump.items() for field in fields
    } - blocked
    advanced = editable - basic

    assert basic
    assert basic.isdisjoint(blocked)
    assert all(exists(path) for path in basic | blocked)
    assert set(schema["full_width"]) <= editable
    assert set(schema["types"]) <= editable
    assert schema["types"]["auth.auth_token"] == "password"
    assert schema["types"]["auth.ct0"] == "password"
    assert schema["types"]["tagging.api_key"] == "password"
    assert "web.password_hash" in blocked
    assert all(path in schema["labels"] for path in editable)
    assert all(path in schema["descriptions"] for path in editable)
    assert {
        "auth.browser",
        "auth.browser_profile",
        "auth.browser_profile_path",
        "auth.firefox_profile_path",
        "sync.page_delay",
        "sync.detail_delay",
        "sync.max_retries",
        "sync.backoff_base",
        "sync.detail_max_retries",
        "sync.detail_backoff_base",
        "sync.cooldown_threshold",
        "sync.cooldown_duration",
        "sync.timeout",
        "sync.max_linked_depth",
        "database.cache_size_kb",
        "database.mmap_size_bytes",
    } <= advanced
