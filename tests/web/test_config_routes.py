from __future__ import annotations

from pathlib import Path

import pytest

from tweetxvault.config import AppConfig, XDGPaths
from tweetxvault.web.routes import config as config_routes


def _paths(tmp_path: Path) -> XDGPaths:
    return XDGPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
    )


def _configured_app() -> AppConfig:
    return AppConfig.model_validate(
        {
            "auth": {
                "auth_token": "auth-secret",
                "ct0": "csrf-secret",
                "user_id": "123",
            },
            "web": {
                "password_hash": "password-secret",
                "host": "127.0.0.1",
                "port": 9000,
            },
            "tagging": {
                "enabled": True,
                "api_key": "gemini-secret",
                "limit": 8,
            },
        }
    )


def test_get_config_masks_every_secret(monkeypatch, make_web_client, tmp_path: Path) -> None:
    config = _configured_app()
    monkeypatch.setattr(
        "tweetxvault.config.load_config",
        lambda: (config, _paths(tmp_path)),
    )
    client = make_web_client(config_routes.router)

    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.json()
    assert data["auth"]["auth_token"] == "********"
    assert data["auth"]["ct0"] == "********"
    assert data["web"]["password_hash"] == "********"
    assert data["tagging"]["api_key"] == "********"
    assert data["auth"]["user_id"] == "123"
    assert data["web"]["port"] == 9000


def test_get_config_keeps_unset_secrets_null(monkeypatch, make_web_client, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "tweetxvault.config.load_config",
        lambda: (AppConfig(), _paths(tmp_path)),
    )
    client = make_web_client(config_routes.router)

    data = client.get("/api/config").json()

    assert data["auth"]["auth_token"] is None
    assert data["auth"]["ct0"] is None
    assert data["web"]["password_hash"] is None
    assert data["tagging"]["api_key"] is None


def test_get_defaults_returns_complete_valid_config(make_web_client) -> None:
    client = make_web_client(config_routes.router)

    response = client.get("/api/config/defaults")

    assert response.status_code == 200
    assert AppConfig.model_validate(response.json()) == AppConfig()
    assert set(response.json()) == {"auth", "sync", "web", "database", "tagging"}


def test_get_schema_returns_ui_contract(make_web_client) -> None:
    client = make_web_client(config_routes.router)

    response = client.get("/api/config/schema")

    assert response.status_code == 200
    schema = response.json()
    assert "tagging.api_key" in schema["whitelist"]
    assert "web.password_hash" in schema["blacklist"]
    assert schema["types"]["auth.auth_token"] == "password"
    assert schema["types"]["tagging.api_key"] == "password"
    assert schema["select_options"]["tagging.thinking_level"]


def test_post_config_preserves_masked_secrets(monkeypatch, make_web_client, tmp_path: Path) -> None:
    current = _configured_app()
    paths = _paths(tmp_path)
    saved: list[tuple[XDGPaths, AppConfig]] = []
    monkeypatch.setattr("tweetxvault.config.load_config", lambda: (current, paths))
    monkeypatch.setattr(
        "tweetxvault.config.save_app_config",
        lambda actual_paths, config: saved.append((actual_paths, config)),
    )
    payload = current.model_dump()
    payload["auth"]["auth_token"] = "********"
    payload["auth"]["ct0"] = "********"
    payload["web"]["password_hash"] = "********"
    payload["tagging"]["api_key"] = "********"
    payload["web"]["port"] = 8123
    client = make_web_client(config_routes.router)

    response = client.post("/api/config", json=payload)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert len(saved) == 1
    saved_paths, saved_config = saved[0]
    assert saved_paths is paths
    assert saved_config.auth.auth_token == "auth-secret"
    assert saved_config.auth.ct0 == "csrf-secret"
    assert saved_config.web.password_hash == "password-secret"
    assert saved_config.tagging.api_key == "gemini-secret"
    assert saved_config.web.port == 8123


def test_post_config_accepts_replacement_secrets(
    monkeypatch, make_web_client, tmp_path: Path
) -> None:
    current = _configured_app()
    paths = _paths(tmp_path)
    saved: list[AppConfig] = []
    monkeypatch.setattr("tweetxvault.config.load_config", lambda: (current, paths))
    monkeypatch.setattr(
        "tweetxvault.config.save_app_config",
        lambda _paths, config: saved.append(config),
    )
    payload = current.model_dump()
    payload["auth"]["auth_token"] = "new-auth"
    payload["auth"]["ct0"] = "new-csrf"
    payload["web"]["password_hash"] = "new-hash"
    payload["tagging"]["api_key"] = "new-api-key"
    client = make_web_client(config_routes.router)

    response = client.post("/api/config", json=payload)

    assert response.status_code == 200
    assert saved[0].auth.auth_token == "new-auth"
    assert saved[0].auth.ct0 == "new-csrf"
    assert saved[0].web.password_hash == "new-hash"
    assert saved[0].tagging.api_key == "new-api-key"


def test_post_config_rejects_invalid_nested_values(
    monkeypatch, make_web_client, tmp_path: Path
) -> None:
    current = _configured_app()
    monkeypatch.setattr(
        "tweetxvault.config.load_config",
        lambda: (current, _paths(tmp_path)),
    )
    payload = current.model_dump()
    payload["tagging"]["limit"] = 0
    client = make_web_client(config_routes.router)

    response = client.post("/api/config", json=payload)

    assert response.status_code == 400
    assert "greater than or equal to 1" in response.json()["detail"]


def test_post_config_requires_all_sections(make_web_client) -> None:
    client = make_web_client(config_routes.router)

    response = client.post("/api/config", json={"auth": {}})

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "attribute"),
    [
        ("/api/config", "load_config"),
        ("/api/config/defaults", "AppConfig"),
        ("/api/config/schema", "get_config_ui_schema"),
    ],
)
def test_get_config_endpoints_translate_failures_to_500(
    monkeypatch, make_web_client, path: str, attribute: str
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(f"tweetxvault.config.{attribute}", fail)
    client = make_web_client(config_routes.router)

    response = client.get(path)

    assert response.status_code == 500
    assert response.json() == {"detail": "config unavailable"}


def test_post_config_translates_save_failure_to_400(
    monkeypatch, make_web_client, tmp_path: Path
) -> None:
    current = _configured_app()
    monkeypatch.setattr(
        "tweetxvault.config.load_config",
        lambda: (current, _paths(tmp_path)),
    )
    monkeypatch.setattr(
        "tweetxvault.config.save_app_config",
        lambda *_args: (_ for _ in ()).throw(OSError("read-only config")),
    )
    client = make_web_client(config_routes.router)

    response = client.post("/api/config", json=current.model_dump())

    assert response.status_code == 400
    assert response.json() == {"detail": "read-only config"}


def test_config_endpoints_require_authentication(make_web_client) -> None:
    client = make_web_client(config_routes.router, password="secret")

    response = client.get("/api/config/defaults")

    assert response.status_code == 401
