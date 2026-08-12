"""Config models and XDG path helpers."""

from __future__ import annotations

import copy
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
ACTIVITY_STATUS_FILENAME = "activity-status.json"
COMMAND_LOCK_FILENAME = "command.lock"
DB_FILENAME = "archive.db"
DEFAULT_SQLITE_CACHE_SIZE_KB = 512 * 1024
DEFAULT_SQLITE_MMAP_SIZE_BYTES = 1024**3
AUTH_PLACEHOLDER_FIELDS = ("auth_token", "ct0", "user_id")
LEGACY_BROWSER_AUTH_FIELDS = (
    "browser",
    "browser_profile",
    "browser_profile_path",
    "firefox_profile_path",
)
CONFIG_SECTION_ORDER = ("auth", "sync", "web", "schedule", "activity", "database", "tagging")


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auth_token: str | None = None
    ct0: str | None = None
    user_id: str | None = None


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
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    fetch_avatars: bool = True


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    cadence: Literal["hours", "daily", "weekly", "monthly"] = "daily"
    every_hours: int = Field(default=6, ge=1, le=720)
    time: str = Field(default="03:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekday: int = Field(default=0, ge=0, le=6)
    day_of_month: int = Field(default=1, ge=1, le=31)
    timezone: str = Field(default="local", min_length=1)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        if value == "local":
            return value
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value


class ActivityConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    max_runs: int = Field(default=100, ge=1, le=10_000)
    retention_days: int = Field(default=90, ge=1, le=3650)


class DatabaseConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cache_size_kb: int = Field(default=DEFAULT_SQLITE_CACHE_SIZE_KB, ge=0)
    mmap_size_bytes: int = Field(default=DEFAULT_SQLITE_MMAP_SIZE_BYTES, ge=0)


class TaggingConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    api_key: str | None = None
    model: str = Field(default="gemini-3.5-flash", min_length=1)
    thinking_level: Literal["high", "medium", "low", "none"] = "high"
    batch: bool = True
    limit: int = Field(default=20, ge=1)
    google_search: bool = True
    rpd: int | None = Field(default=None, ge=1)
    max_media_size_mb: int = Field(default=100, ge=1)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auth: AuthConfig = Field(default_factory=AuthConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    activity: ActivityConfig = Field(default_factory=ActivityConfig)
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
    def activity_status_file(self) -> Path:
        return self.data_dir / ACTIVITY_STATUS_FILENAME

    @property
    def command_lock_file(self) -> Path:
        return self.data_dir / COMMAND_LOCK_FILENAME

    @property
    def staged_archive_file(self) -> Path:
        return self.data_dir / "setup" / "archive.zip"

    @property
    def schedule_state_file(self) -> Path:
        return self.data_dir / "schedule-state.json"

    @property
    def activity_runs_dir(self) -> Path:
        return self.data_dir / "activity" / "runs"

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

    env = os.environ if env is None else env
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
        raw: dict[str, Any] = {"auth": {key: "" for key in AUTH_PLACEHOLDER_FIELDS}}
        _write_config_file(path, raw)
        return raw

    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    if not isinstance(loaded, dict):
        loaded = {}
    changed = _ensure_auth_skeleton(loaded)
    if changed:
        _write_config_file(path, loaded)
    return loaded


def _ensure_auth_skeleton(raw: dict[str, Any]) -> bool:
    auth = raw.get("auth")
    changed = not isinstance(auth, dict)
    if changed:
        auth = {}
        raw["auth"] = auth
    for key in LEGACY_BROWSER_AUTH_FIELDS:
        if key in auth:
            del auth[key]
            changed = True
    for key in AUTH_PLACEHOLDER_FIELDS:
        if key not in auth:
            auth[key] = ""
            changed = True
    return changed


def _normalize_auth_placeholders(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(raw)
    auth = normalized.get("auth")
    if isinstance(auth, dict):
        for key in AUTH_PLACEHOLDER_FIELDS:
            if auth.get(key) == "":
                auth[key] = None
    return normalized


def _format_toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _write_config_file(path: Path, raw: Mapping[str, Any]) -> None:
    lines: list[str] = []
    section_names = [name for name in CONFIG_SECTION_ORDER if name in raw]
    section_names.extend(name for name in raw if name not in CONFIG_SECTION_ORDER)
    for section in section_names:
        fields = raw[section]
        if not isinstance(fields, Mapping) or (section != "auth" and not fields):
            continue
        lines.append(f"[{section}]")
        for key, value in fields.items():
            if value is not None:
                lines.append(f"{key} = {_format_toml_value(value)}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _env_float(env: Mapping[str, str], name: str) -> float | None:
    value = env.get(name)
    return float(value) if value is not None else None


def _env_int(env: Mapping[str, str], name: str) -> int | None:
    value = env.get(name)
    return int(value) if value is not None else None


def load_config(env: Mapping[str, str] | None = None) -> tuple[AppConfig, XDGPaths]:
    env = os.environ if env is None else env
    paths = ensure_paths(resolve_paths(env))
    raw = _load_config_file(paths.config_file)
    config = AppConfig.model_validate(_normalize_auth_placeholders(raw))

    auth_updates = {
        "auth_token": env.get("TWEETXVAULT_AUTH_TOKEN"),
        "ct0": env.get("TWEETXVAULT_CT0"),
        "user_id": env.get("TWEETXVAULT_USER_ID"),
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
        "max_linked_depth": _env_int(env, "TWEETXVAULT_MAX_LINKED_DEPTH"),
    }
    sync_updates = {key: value for key, value in sync_updates.items() if value is not None}

    if auth_updates:
        config.auth = config.auth.model_copy(update=auth_updates)
    if sync_updates:
        config.sync = config.sync.model_copy(update=sync_updates)
    return config, paths


def _valid_config_paths() -> set[str]:
    return {
        f"{section}.{field}"
        for section, model_field in AppConfig.model_fields.items()
        for field in model_field.annotation.model_fields
    }


def update_config_values(paths: XDGPaths, changes: Mapping[str, Any]) -> None:
    raw = _load_config_file(paths.config_file)
    valid_paths = _valid_config_paths()
    defaults = AppConfig().model_dump()

    for path, value in changes.items():
        if path not in valid_paths:
            raise ValueError(f"Unknown configuration field: {path}")
        section, field = path.split(".", 1)
        section_data = raw.get(section)
        if not isinstance(section_data, dict):
            section_data = {}
            raw[section] = section_data

        if section == "auth" and field in AUTH_PLACEHOLDER_FIELDS:
            section_data[field] = "" if value is None else value
        elif value is None or value == defaults[section][field]:
            section_data.pop(field, None)
        else:
            section_data[field] = value

    for section in tuple(raw):
        if section != "auth" and isinstance(raw[section], dict) and not raw[section]:
            del raw[section]
    _ensure_auth_skeleton(raw)
    AppConfig.model_validate(_normalize_auth_placeholders(raw))
    _write_config_file(paths.config_file, raw)


def get_explicit_config_fields(paths: XDGPaths) -> list[str]:
    raw = _load_config_file(paths.config_file)
    valid_paths = _valid_config_paths()
    explicit: list[str] = []
    for section in CONFIG_SECTION_ORDER:
        fields = raw.get(section)
        if not isinstance(fields, dict):
            continue
        for field, value in fields.items():
            path = f"{section}.{field}"
            if path not in valid_paths or path == "web.password_hash":
                continue
            if section == "auth" and field in AUTH_PLACEHOLDER_FIELDS and value == "":
                continue
            explicit.append(path)
    return sorted(explicit)


def get_config_ui_schema() -> dict[str, Any]:
    return {
        "whitelist": [
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
            "auth.auth_token",
            "auth.ct0",
            "auth.user_id",
            "web.password_hash",
            "schedule.enabled",
            "schedule.cadence",
            "schedule.every_hours",
            "schedule.time",
            "schedule.weekday",
            "schedule.day_of_month",
            "schedule.timezone",
            "activity.max_runs",
            "activity.retention_days",
        ],
        "types": {
            "tagging.api_key": "password",
            "tagging.thinking_level": "select",
        },
        "full_width": [
            "tagging.api_key",
        ],
        "select_options": {
            "tagging.thinking_level": [
                {"value": "high", "label": "High"},
                {"value": "medium", "label": "Medium"},
                {"value": "low", "label": "Low"},
                {"value": "none", "label": "None"},
            ]
        },
        "labels": {
            "auth": "Authentication",
            "sync": "Sync & Delays",
            "web": "Web Server",
            "schedule": "Scheduled Syncs",
            "activity": "Activity History",
            "database": "Database",
            "tagging": "AI Tagging",
            "auth.auth_token": "Auth Token",
            "auth.ct0": "CT0 (CSRF Token)",
            "auth.user_id": "User ID",
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
            "web.host": "Host",
            "web.port": "Port",
            "web.fetch_avatars": "Fetch Avatars locally",
            "schedule.enabled": "Enable Scheduled Syncs",
            "schedule.cadence": "Schedule Frequency",
            "schedule.every_hours": "Every N Hours",
            "schedule.time": "Run Time",
            "schedule.weekday": "Weekday",
            "schedule.day_of_month": "Day of Month",
            "schedule.timezone": "Time Zone",
            "activity.max_runs": "Maximum Saved Runs",
            "activity.retention_days": "Log Retention (Days)",
            "database.cache_size_kb": "Cache Size (KiB)",
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
            "database.cache_size_kb": (
                "Maximum SQLite page-cache target per database connection. "
                "Default: 524288 KiB (512 MiB)."
            ),
            "database.mmap_size_bytes": (
                "Maximum portion of the database SQLite may access through memory-mapped I/O. "
                "Default: 1073741824 bytes (1 GiB)."
            ),
            "sync.page_delay": "How many seconds to wait between fetching pages of tweets.",
            "sync.detail_delay": (
                "How many seconds to wait between fetching individual tweet details."
            ),
            "sync.max_retries": (
                "How many times to retry fetching a timeline if Twitter rate-limits you."
            ),
            "sync.backoff_base": (
                "How much to multiply the wait time by after each failed timeline request."
            ),
            "sync.detail_max_retries": (
                "How many times to retry fetching a single tweet if Twitter rate-limits you."
            ),
            "sync.detail_backoff_base": (
                "How much to multiply the wait time by after each failed single tweet request."
            ),
            "sync.cooldown_threshold": (
                "How many consecutive rate-limit errors trigger a long cooldown pause."
            ),
            "sync.cooldown_duration": (
                "How many seconds to pause when a long cooldown is triggered."
            ),
            "sync.timeout": "How many seconds to wait before giving up on a slow network request.",
            "sync.max_linked_depth": (
                "How deep to go when fetching nested tweet replies or quoted links."
            ),
            "web.fetch_avatars": "Automatically download and cache user profile pictures.",
            "web.host": "The IP address the Web UI runs on (default is 127.0.0.1 for local only).",
            "web.port": "The port the Web UI runs on.",
            "schedule.enabled": "Run sync automatically from the always-on Web service.",
            "schedule.cadence": "Run every N hours, every day, every week, or every month.",
            "schedule.every_hours": "Hours between runs when the hourly cadence is selected.",
            "schedule.time": "Local or configured-zone time for daily, weekly, and monthly runs.",
            "schedule.weekday": "Weekday for weekly runs, where Monday is zero.",
            "schedule.day_of_month": "Preferred calendar day for monthly runs.",
            "schedule.timezone": "IANA time zone name, or local for the system time zone.",
            "activity.max_runs": "Maximum number of completed command histories to retain.",
            "activity.retention_days": "Maximum age of completed command histories.",
            "tagging.enabled": "Turn automated AI tagging on or off.",
            "tagging.api_key": "Your Google Gemini API key.",
            "tagging.model": "Which Gemini AI model to use for tagging tweets.",
            "tagging.thinking_level": "How much reasoning effort the AI should use.",
            "tagging.batch": "Group multiple tweets together in one API call to save time.",
            "tagging.google_search": (
                "Allow the AI to search Google to identify people, characters, and franchises."
            ),
            "tagging.limit": "How many tweets to fetch and tag per batch.",
            "tagging.rpd": "Daily limit on how many API requests the app can make to Gemini.",
            "tagging.max_media_size_mb": "Skip uploading media files larger than this size.",
        },
    }
