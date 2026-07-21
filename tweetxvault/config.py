"""Config models and XDG path helpers."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

APP_NAME = "tweetxvault"
API_BASE_URL = "https://x.com/i/api/graphql"
CLIENT_WEB_BUNDLE_BASE = "https://abs.twimg.com/responsive-web/client-web"
DISCOVERY_PAGE_URL = "https://x.com/?lang=en"
BUNDLE_URL_REGEX = r"https://abs\.twimg\.com/responsive-web/client-web/[A-Za-z0-9_.~-]+\.js"
PUBLIC_BEARER_TOKEN = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16c"
    "HjhLTvJu4FA33AGWWjCpTnA"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
CONFIG_FILENAME = "config.toml"
QUERY_ID_CACHE_FILENAME = "query-ids.json"
LOCK_FILENAME = "sync.lock"
DB_FILENAME = "archive.db"


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auth_token: str | None = None
    ct0: str | None = None
    user_id: str | None = None
    browser: str | None = None
    browser_profile: str | None = None
    browser_profile_path: str | None = None
    firefox_profile_path: str | None = None


class SyncConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page_delay: float = Field(default=2.0, ge=0)
    detail_delay: float = Field(default=0.0, ge=0)
    max_retries: int = Field(default=3, ge=0)
    backoff_base: float = Field(default=2.0, ge=0)
    detail_max_retries: int = Field(default=2, ge=0)
    detail_backoff_base: float = Field(default=30.0, ge=0)
    cooldown_threshold: int = Field(default=3, ge=1)
    cooldown_duration: float = Field(default=300.0, ge=0)
    timeout: float = Field(default=30.0, ge=1.0)
    max_linked_depth: int = Field(default=1, ge=0)


class WebConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    password_hash: str | None = None
    auto_start: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    fetch_avatars: bool = True


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cache_size_kb: int = Field(default=1000000, ge=0)
    mmap_size_bytes: int = Field(default=8589934592, ge=0)


class TaggingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    api_key: str | None = None
    model: str = "gemini-3.5-flash"
    thinking_level: str = "high"
    batch: bool = True
    limit: int = Field(default=20, ge=1)
    google_search: bool = True
    rpd: int | None = None
    max_media_size_mb: int = 100


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auth: AuthConfig = Field(default_factory=AuthConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    tagging: TaggingConfig = Field(default_factory=TaggingConfig)


class XDGPaths(BaseModel):
    """Resolved application paths."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILENAME

    @property
    def query_id_cache_file(self) -> Path:
        return self.cache_dir / QUERY_ID_CACHE_FILENAME

    @property
    def lock_file(self) -> Path:
        return self.data_dir / LOCK_FILENAME

    @property
    def database_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def database_file(self) -> Path:
        """Backward-compatible alias for older callers/tests."""
        return self.database_path


def resolve_paths(env: Mapping[str, str] | None = None) -> XDGPaths:
    import platformdirs

    env = env or os.environ
    # Allow explicit env-var overrides; otherwise use platformdirs for
    # cross-platform defaults (XDG on Linux, ~/Library on macOS, %APPDATA% on Windows).
    if raw := env.get("XDG_CONFIG_HOME"):
        config_dir = Path(raw).expanduser() / APP_NAME
    else:
        config_dir = Path(platformdirs.user_config_dir(APP_NAME))

    if raw := env.get("XDG_DATA_HOME"):
        data_dir = Path(raw).expanduser() / APP_NAME
    else:
        data_dir = Path(platformdirs.user_data_dir(APP_NAME))

    if raw := env.get("XDG_CACHE_HOME"):
        cache_dir = Path(raw).expanduser() / APP_NAME
    else:
        cache_dir = Path(platformdirs.user_cache_dir(APP_NAME))

    return XDGPaths(
        config_dir=config_dir,
        data_dir=data_dir,
        cache_dir=cache_dir,
    )


def ensure_paths(paths: XDGPaths) -> XDGPaths:
    for path in (paths.config_dir, paths.data_dir, paths.cache_dir):
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _load_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[database]\ncache_size_kb = 1000000\nmmap_size_bytes = 8589934592\n", encoding="utf-8")
    else:
        content = path.read_text(encoding="utf-8")
        if "[database]" not in content:
            content = content.rstrip() + "\n\n[database]\ncache_size_kb = 1000000\nmmap_size_bytes = 8589934592\n"
            path.write_text(content, encoding="utf-8")
            
    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    return loaded if isinstance(loaded, dict) else {}


def _env_float(env: Mapping[str, str], name: str) -> float | None:
    value = env.get(name)
    return float(value) if value is not None else None


def _env_int(env: Mapping[str, str], name: str) -> int | None:
    value = env.get(name)
    return int(value) if value is not None else None


def load_config(env: Mapping[str, str] | None = None) -> tuple[AppConfig, XDGPaths]:
    env = env or os.environ
    paths = ensure_paths(resolve_paths(env))
    raw = _load_config_file(paths.config_file)
    config = AppConfig.model_validate(raw)

    auth_updates = {
        "auth_token": env.get("TWEETXVAULT_AUTH_TOKEN"),
        "ct0": env.get("TWEETXVAULT_CT0"),
        "user_id": env.get("TWEETXVAULT_USER_ID"),
        "browser": env.get("TWEETXVAULT_BROWSER"),
        "browser_profile": env.get("TWEETXVAULT_BROWSER_PROFILE"),
        "browser_profile_path": env.get("TWEETXVAULT_BROWSER_PROFILE_PATH"),
        "firefox_profile_path": env.get("TWEETXVAULT_FIREFOX_PROFILE_PATH"),
    }
    auth_updates = {key: value for key, value in auth_updates.items() if value is not None}
    sync_updates = {
        "page_delay": _env_float(env, "TWEETXVAULT_PAGE_DELAY"),
        "detail_delay": _env_float(env, "TWEETXVAULT_DETAIL_DELAY"),
        "max_retries": _env_int(env, "TWEETXVAULT_MAX_RETRIES"),
        "backoff_base": _env_float(env, "TWEETXVAULT_BACKOFF_BASE"),
        "detail_max_retries": _env_int(env, "TWEETXVAULT_DETAIL_MAX_RETRIES"),
        "detail_backoff_base": _env_float(env, "TWEETXVAULT_DETAIL_BACKOFF_BASE"),
        "cooldown_threshold": _env_int(env, "TWEETXVAULT_COOLDOWN_THRESHOLD"),
        "cooldown_duration": _env_float(env, "TWEETXVAULT_COOLDOWN_DURATION"),
        "timeout": _env_float(env, "TWEETXVAULT_TIMEOUT"),
    }
    sync_updates = {key: value for key, value in sync_updates.items() if value is not None}

    if auth_updates:
        config.auth = config.auth.model_copy(update=auth_updates)
    if sync_updates:
        config.sync = config.sync.model_copy(update=sync_updates)
    return config, paths


def save_app_config(paths: XDGPaths, config: AppConfig) -> None:
    lines = []
    
    def format_value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        elif isinstance(v, (int, float)):
            return str(v)
        elif v is None:
            return '""'
        else:
            s = str(v).replace("\\", "\\\\").replace('"', '\\"')
            return f'"{s}"'

    config_dict = config.model_dump(exclude_unset=True)
    
    for section, fields in config_dict.items():
        if isinstance(fields, dict):
            lines.append(f"[{section}]")
            for k, v in fields.items():
                if v is not None:
                    lines.append(f"{k} = {format_value(v)}")
            lines.append("")

    content = "\n".join(lines).strip() + "\n"
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    paths.config_file.write_text(content, encoding="utf-8")

def get_config_ui_schema() -> dict[str, Any]:
    return {
        "whitelist": [
            "auth.auth_token",
            "auth.ct0",
            "auth.user_id",
            "web.auto_start",
            "web.fetch_avatars",
            "web.host",
            "web.port",
            "tagging.enabled",
            "tagging.api_key",
            "tagging.model",
            "tagging.thinking_level",
            "tagging.batch",
            "tagging.limit",
            "tagging.google_search",
            "tagging.rpd",
            "tagging.max_media_size_mb",
        ],
        "blacklist": [
            "web.password_hash"
        ],
        "types": {
            "auth.auth_token": "password",
            "auth.ct0": "password",
            "tagging.api_key": "password",
            "tagging.thinking_level": "select",
        },
        "full_width": [
            "auth.browser_profile_path",
            "auth.firefox_profile_path",
            "tagging.api_key"
        ],
        "select_options": {
            "tagging.thinking_level": [
                {"value": "high", "label": "High"},
                {"value": "medium", "label": "Medium"},
                {"value": "low", "label": "Low"},
                {"value": "none", "label": "None"}
            ]
        },
        "labels": {
            "auth": "Authentication",
            "sync": "Sync & Delays",
            "web": "Web Server",
            "database": "Database",
            "tagging": "Media Tagging",
            "auth.auth_token": "Auth Token",
            "auth.ct0": "CT0 (CSRF Token)",
            "auth.user_id": "User ID",
            "auth.browser": "Browser",
            "auth.browser_profile": "Browser Profile",
            "auth.browser_profile_path": "Browser Profile Path",
            "auth.firefox_profile_path": "Firefox Profile Path",
            "sync.page_delay": "Page Delay (s)",
            "sync.detail_delay": "Detail Delay (s)",
            "sync.max_retries": "Max Retries",
            "sync.backoff_base": "Backoff Base (s)",
            "sync.detail_max_retries": "Detail Max Retries",
            "sync.detail_backoff_base": "Detail Backoff Base (s)",
            "sync.cooldown_threshold": "Cooldown Threshold",
            "sync.cooldown_duration": "Cooldown Duration (s)",
            "sync.timeout": "Timeout (s)",
            "sync.max_linked_depth": "Max Linked Depth",
            "web.auto_start": "Auto-Start Server",
            "web.host": "Host",
            "web.port": "Port",
            "web.fetch_avatars": "Fetch Avatars locally",
            "database.cache_size_kb": "Cache Size (KB)",
            "database.mmap_size_bytes": "MMap Size (Bytes)",
            "tagging.enabled": "Enable Tagging",
            "tagging.api_key": "Gemini API Key",
            "tagging.model": "Model",
            "tagging.thinking_level": "Thinking Level",
            "tagging.batch": "Batch Processing",
            "tagging.limit": "Max Tweets Per Batch",
            "tagging.google_search": "Google Search Grounding",
            "tagging.rpd": "API Requests Per Day",
            "tagging.max_media_size_mb": "Max Media Size (MB)",
        },
        "descriptions": {
            "auth.auth_token": "Your Twitter authentication token. See README",
            "auth.ct0": "Your CSRF token. See README",
            "auth.user_id": "Your numerical X user ID.",
            "auth.browser": "Select a browser to automatically extract cookies from.",
            "auth.browser_profile": "Name of the browser profile to extract cookies from.",
            "auth.browser_profile_path": "Absolute path to a specific browser profile.",
            "auth.firefox_profile_path": "Absolute path to a Firefox profile.",
            "database.cache_size_kb": "How much RAM to allocate for faster database queries.",
            "database.mmap_size_bytes": "How much of the database file to map directly into memory for faster searching.",
            "sync.page_delay": "How many seconds to wait between fetching pages of tweets.",
            "sync.detail_delay": "How many seconds to wait between fetching individual tweet details.",
            "sync.max_retries": "How many times to retry fetching a timeline if Twitter rate-limits you.",
            "sync.backoff_base": "How much to multiply the wait time by after each failed timeline request.",
            "sync.detail_max_retries": "How many times to retry fetching a single tweet if Twitter rate-limits you.",
            "sync.detail_backoff_base": "How much to multiply the wait time by after each failed single tweet request.",
            "sync.cooldown_threshold": "How many consecutive rate-limit errors trigger a long cooldown pause.",
            "sync.cooldown_duration": "How many seconds to pause when a long cooldown is triggered.",
            "sync.timeout": "How many seconds to wait before giving up on a slow network request.",
            "sync.max_linked_depth": "How deep to go when fetching nested tweet replies or quoted links.",
            "web.auto_start": "Automatically start the Web UI running in the background after running sync commands.",
            "web.fetch_avatars": "Automatically download and cache user profile pictures.",
            "web.host": "The IP address the Web UI runs on (default is 127.0.0.1 for local only).",
            "web.port": "The port the Web UI runs on.",
            "tagging.enabled": "Turn automated AI tagging on or off.",
            "tagging.api_key": "Your Google Gemini API key.",
            "tagging.model": "Which Gemini AI model to use for tagging media.",
            "tagging.thinking_level": "How much reasoning effort the AI should use.",
            "tagging.batch": "Group multiple tweets together in one API call to save time.",
            "tagging.google_search": "Allow the AI to search Google to more effectively identify characters, franchises, or people in images.",
            "tagging.limit": "How many tweets to fetch and tag per batch.",
            "tagging.rpd": "Daily limit on how many API requests the app can make to Gemini.",
            "tagging.max_media_size_mb": "Skip uploading media files larger than this size.",
        },
        }
    
