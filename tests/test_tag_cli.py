from __future__ import annotations

import os
import runpy
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import pytest
from rich.console import Console
from typer.testing import CliRunner

import tweetxvault.cli as cli
import tweetxvault.jobs as jobs
import tweetxvault.tagging as tagging
from tweetxvault.config import AppConfig, TaggingConfig
from tweetxvault.exceptions import ConfigError, TweetXVaultError

runner = CliRunner()


class FakeTagStore:
    def __init__(self, tweet_ids: list[str]) -> None:
        self.tweet_ids = tweet_ids
        self.eligible_limits: list[int] = []

    def get_eligible_tweets_for_tagging(self, *, limit: int) -> list[str]:
        self.eligible_limits.append(limit)
        return self.tweet_ids[:limit]


class FakeLockedJob:
    def __init__(
        self,
        store: FakeTagStore,
        events: list[str],
        *,
        enter_error: BaseException | None = None,
    ) -> None:
        self.store = store
        self.events = events
        self.enter_error = enter_error

    async def __aenter__(self) -> FakeLockedJob:
        self.events.append("enter")
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        self.events.append(f"exit:{exc_type.__name__ if exc_type else 'clean'}")


def capture_console(monkeypatch: pytest.MonkeyPatch) -> StringIO:
    output = StringIO()
    monkeypatch.setattr(
        cli,
        "_configure_logging",
        lambda: Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=240,
        ),
    )
    return output


def configure_tag_command(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    *,
    batch: bool = True,
    limit: int = 20,
    tweet_ids: list[str] | None = None,
    enter_error: BaseException | None = None,
) -> tuple[
    AppConfig,
    FakeTagStore,
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    config = AppConfig(
        tagging=TaggingConfig(
            enabled=True,
            api_key="test-key",
            batch=batch,
            limit=limit,
        )
    )
    store = FakeTagStore(tweet_ids if tweet_ids is not None else ["1", "2", "3"])
    lifecycle: list[str] = []
    lock_calls: list[dict[str, Any]] = []
    tagging_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(cli, "load_config", lambda: (config, paths))

    def fake_locked_archive_job(**kwargs: Any) -> FakeLockedJob:
        lock_calls.append(kwargs)
        return FakeLockedJob(store, lifecycle, enter_error=enter_error)

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        lifecycle.append("tag")
        tagging_calls.append(kwargs)
        return len(kwargs["tweet_ids"])

    monkeypatch.setattr(jobs, "locked_archive_job", fake_locked_archive_job)
    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    return config, store, lifecycle, lock_calls, tagging_calls


def test_root_help_exposes_installed_non_sync_additions() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "tag" in result.stdout
    assert "Use Gemini to generate search tags and descriptions" in result.stdout
    assert "migrate" in result.stdout
    assert "Migrate data from older LanceDB storage" in result.stdout
    assert "web" in result.stdout
    assert "Manage the background web UI server" in result.stdout
    assert "serve-daemon" not in result.stdout


def test_tag_help_documents_model_override() -> None:
    result = runner.invoke(cli.app, ["tag", "--help"])

    assert result.exit_code == 0
    assert "--model" in result.stdout
    assert "Override the Gemini model specified in config.toml" in result.stdout


@pytest.mark.parametrize(
    ("batch", "configured_limit", "expected_limit", "expected_ids"),
    [
        (True, 2, 2, ["1", "2"]),
        (False, 9, 1, ["1"]),
    ],
)
def test_tag_command_respects_batch_limit_and_forwards_default_model(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    batch: bool,
    configured_limit: int,
    expected_limit: int,
    expected_ids: list[str],
) -> None:
    output = capture_console(monkeypatch)
    config, store, lifecycle, lock_calls, tagging_calls = configure_tag_command(
        monkeypatch,
        paths,
        batch=batch,
        limit=configured_limit,
    )

    result = runner.invoke(cli.app, ["tag"])

    assert result.exit_code == 0, result.output
    assert output.getvalue() == ""
    assert store.eligible_limits == [expected_limit]
    assert lifecycle == ["enter", "tag", "exit:clean"]
    assert lock_calls == [{"config": config, "paths": paths, "console": ANY}]
    assert tagging_calls == [
        {
            "store": store,
            "config": config,
            "paths": paths,
            "console": ANY,
            "tweet_ids": expected_ids,
            "model_override": None,
        }
    ]


def test_tag_command_forwards_model_override_inside_locked_job(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    capture_console(monkeypatch)
    config, store, lifecycle, lock_calls, tagging_calls = configure_tag_command(
        monkeypatch,
        paths,
        tweet_ids=["41", "42"],
    )

    result = runner.invoke(
        cli.app,
        ["tag", "--model", "gemini-explicit"],
    )

    assert result.exit_code == 0, result.output
    assert lifecycle == ["enter", "tag", "exit:clean"]
    assert lock_calls[0]["config"] is config
    assert lock_calls[0]["paths"] is paths
    assert lock_calls[0]["console"] is tagging_calls[0]["console"]
    assert tagging_calls[0]["store"] is store
    assert tagging_calls[0]["tweet_ids"] == ["41", "42"]
    assert tagging_calls[0]["model_override"] == "gemini-explicit"


def test_tag_command_reports_no_eligible_rows_and_still_closes_job(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    output = capture_console(monkeypatch)
    _, store, lifecycle, _, tagging_calls = configure_tag_command(
        monkeypatch,
        paths,
        tweet_ids=[],
    )

    result = runner.invoke(cli.app, ["tag"])

    assert result.exit_code == 0, result.output
    assert store.eligible_limits == [20]
    assert tagging_calls == []
    assert lifecycle == ["enter", "exit:clean"]
    assert "No eligible untagged media tweets found." in output.getvalue()


def test_tag_command_maps_config_error_to_exit_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = capture_console(monkeypatch)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda: (_ for _ in ()).throw(ConfigError("invalid tagging config")),
    )

    result = runner.invoke(cli.app, ["tag"])

    assert result.exit_code == 1
    assert "invalid tagging config" in output.getvalue()


def test_tag_command_maps_runtime_domain_error_to_exit_two_and_closes_job(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    output = capture_console(monkeypatch)
    _, _, lifecycle, _, _ = configure_tag_command(
        monkeypatch,
        paths,
        enter_error=TweetXVaultError("archive is unavailable"),
    )

    result = runner.invoke(cli.app, ["tag"])

    assert result.exit_code == 2
    assert lifecycle == ["enter"]
    assert "archive is unavailable" in output.getvalue()


def test_python_module_help_dispatches_real_cli() -> None:
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"

    result = subprocess.run(
        [sys.executable, "-m", "tweetxvault", "--help"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "tweetxvault CLI" in result.stdout
    assert "tag" in result.stdout
    assert "migrate" in result.stdout
    assert "web" in result.stdout
    assert "serve-daemon" not in result.stdout


def test_python_module_dispatches_hidden_serve_daemon_via_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched_arguments: list[list[str]] = []

    def fake_app() -> None:
        dispatched_arguments.append(sys.argv[1:])

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tweetxvault", "serve-daemon"])

    runpy.run_module("tweetxvault", run_name="__main__")

    assert dispatched_arguments == [["serve-daemon"]]


def test_hidden_serve_daemon_closes_archive_before_starting_server(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    events: list[object] = []
    config = AppConfig()

    class FakeArchive:
        def close(self) -> None:
            events.append("close")

    def fake_run_server(*args: Any) -> None:
        events.append(("run_server", args))

    monkeypatch.setattr(cli, "load_config", lambda: (config, paths))
    monkeypatch.setattr(
        cli,
        "open_archive_store",
        lambda loaded_paths, *, create, config: (
            events.append(("open", loaded_paths, create, config)) or FakeArchive()
        ),
    )
    monkeypatch.setattr("tweetxvault.web.server.run_server", fake_run_server)

    cli.serve_daemon_internal()

    assert events[0] == ("open", paths, False, config)
    assert events[1] == "close"
    assert events[2] == (
        "run_server",
        (
            config,
            paths,
            config.web.host,
            config.web.port,
            config.web.password_hash,
        ),
    )
