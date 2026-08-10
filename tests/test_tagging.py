from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from rich.console import Console

from tweetxvault import tagging
from tweetxvault.config import AppConfig, TaggingConfig
from tweetxvault.pipeline import PipelineReporter
from tweetxvault.rpd import get_rpd_status, reserve_rpd_request


class FakeStore:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """
            CREATE TABLE archive (
                row_key TEXT PRIMARY KEY,
                record_type TEXT NOT NULL,
                tweet_id TEXT,
                media_key TEXT,
                media_type TEXT,
                local_path TEXT,
                raw_json TEXT,
                author_display_name TEXT,
                author_username TEXT,
                text TEXT,
                enrichment_state TEXT,
                updated_at TEXT,
                key TEXT,
                value TEXT
            )
            """
        )
        self.tag_counts: list[dict[str, Any]] = []

    def add_tweet(
        self,
        tweet_id: str,
        *,
        text: str = 'Text with "quotes"',
        raw_json: str | None = None,
        author_display_name: str = "Test Author",
        author_username: str = "tester",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO archive (
                row_key, record_type, tweet_id, raw_json,
                author_display_name, author_username, text
            ) VALUES (?, 'tweet_object', ?, ?, ?, ?, ?)
            """,
            (
                f"tweet_object:{tweet_id}",
                tweet_id,
                raw_json or json.dumps({"legacy": {}}),
                author_display_name,
                author_username,
                text,
            ),
        )
        self.conn.commit()

    def add_media(
        self,
        tweet_id: str,
        *,
        media_key: str = "media-1",
        media_type: str = "photo",
        local_path: str | None = "media/photo.png",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO archive (
                row_key, record_type, tweet_id, media_key, media_type, local_path
            ) VALUES (?, 'media', ?, ?, ?, ?)
            """,
            (
                f"media:{tweet_id}:{media_key}",
                tweet_id,
                media_key,
                media_type,
                local_path,
            ),
        )
        self.conn.commit()

    def get_tag_counts(self, *, limit: int = 50) -> list[dict[str, Any]]:
        assert limit == 50
        return self.tag_counts

    def media_tag(self, tweet_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM archive WHERE record_type = 'media_tag' AND tweet_id = ?",
            (tweet_id,),
        ).fetchone()


class PendingTagStore:
    def __init__(self, tweet_ids: list[str]) -> None:
        self.remaining = list(tweet_ids)
        self.selection_limits: list[int] = []

    def get_eligible_tweets_for_tagging(self, *, limit: int) -> list[str]:
        self.selection_limits.append(limit)
        selected = self.remaining[:limit]
        del self.remaining[: len(selected)]
        return selected

    def count_eligible_tweets_for_tagging(self) -> int:
        return len(self.remaining)


class FakeFiles:
    def __init__(
        self,
        *,
        listed: Iterable[str] = (),
        list_error: Exception | None = None,
        upload_error: Exception | None = None,
        states: dict[str, list[str]] | None = None,
    ) -> None:
        self.listed = list(listed)
        self.list_error = list_error
        self.upload_error = upload_error
        self.states = states or {}
        self.deleted: list[str] = []
        self.uploaded: list[str] = []
        self.get_calls: list[str] = []

    def list(self) -> list[SimpleNamespace]:
        if self.list_error:
            raise self.list_error
        return [SimpleNamespace(name=name) for name in self.listed]

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)

    def upload(self, *, file: str) -> SimpleNamespace:
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append(file)
        name = f"files/{Path(file).name}"
        return SimpleNamespace(name=name)

    def get(self, *, name: str) -> SimpleNamespace:
        self.get_calls.append(name)
        states = self.states.setdefault(name, ["ACTIVE"])
        state = states.pop(0) if len(states) > 1 else states[0]
        return SimpleNamespace(name=name, state=SimpleNamespace(name=state))


class FakeModels:
    def __init__(self, outcomes: Iterable[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(
        self,
        outcomes: Iterable[object],
        *,
        files: FakeFiles | None = None,
    ) -> None:
        self.files = files or FakeFiles()
        self.models = FakeModels(outcomes)


class FailingTagConnection:
    def __init__(self, connection: sqlite3.Connection, *, fail_on_write: int) -> None:
        self.connection = connection
        self.fail_on_write = fail_on_write
        self.media_tag_writes = 0
        self.rollbacks = 0

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if "INSERT OR REPLACE INTO archive" in sql and parameters[1] == "media_tag":
            self.media_tag_writes += 1
            if self.media_tag_writes == self.fail_on_write:
                raise sqlite3.OperationalError("write failed for top-secret-api-key")
        return self.connection.execute(sql, parameters)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.connection.rollback()


class FailingReservationConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.rollbacks = 0

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if sql.strip() == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("quota storage failed for top-secret-api-key")
        return self.connection.execute(sql, parameters)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.rollbacks += 1
        self.connection.rollback()


class TrackingImage:
    def __init__(self) -> None:
        self.loaded = False
        self.closed = False

    def load(self) -> None:
        self.loaded = True

    def close(self) -> None:
        self.closed = True


def response(payload: object, *, finish_reason: str | None = None) -> SimpleNamespace:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    candidates = [SimpleNamespace(finish_reason=finish_reason)] if finish_reason is not None else []
    return SimpleNamespace(text=text, candidates=candidates)


def empty_response(*, finish_reason: str = "MAX_TOKENS") -> SimpleNamespace:
    return SimpleNamespace(
        text="",
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


def make_config(**updates: Any) -> AppConfig:
    values = {
        "enabled": True,
        "api_key": "top-secret-api-key",
        "model": "gemini-default",
        "thinking_level": "high",
        "google_search": False,
        "max_media_size_mb": 100,
    }
    values.update(updates)
    return AppConfig(tagging=TaggingConfig(**values))


def make_console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, color_system=None, width=240), output


def write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 3), color=(20, 40, 60)).save(path)


def prepare_photo(store: FakeStore, data_dir: Path, tweet_id: str = "1") -> None:
    store.add_tweet(tweet_id)
    store.add_media(tweet_id)
    write_image(data_dir / "media" / "photo.png")


def successful_result(tweet_id: str = "1") -> list[dict[str, object]]:
    return [
        {
            "id": tweet_id,
            "description": "A precise media description.",
            "tags": ["deadlock", "ivy (deadlock)"],
        }
    ]


def rpd_used(store: FakeStore, *, model: str = "gemini-default", limit: int = 100) -> int:
    return get_rpd_status(store, model=model, limit=limit).used


@pytest.mark.asyncio
async def test_pending_tagging_marks_planned_step_skipped_when_queue_is_empty(paths) -> None:
    console, _ = make_console()
    reporter = PipelineReporter(console, "tag", interactive=False)

    with reporter:
        result = await tagging.tag_pending_media_tweets(
            PendingTagStore([]),
            make_config(),
            paths,
            console,
        )

    assert result == tagging.TaggingRunResult()
    assert reporter._step_by_key["tagging"].state == "skipped"
    assert "no eligible untagged" in reporter._step_by_key["tagging"].summary


@pytest.mark.asyncio
async def test_tagging_step_is_active_while_the_eligible_queue_is_counted(paths) -> None:
    console, _ = make_console()
    reporter = PipelineReporter(console, "tag", interactive=False)

    class ObservedStore(PendingTagStore):
        def count_eligible_tweets_for_tagging(self) -> int:
            assert reporter.active_step is not None
            assert reporter.active_step.key == "tagging"
            assert "Counting eligible" in reporter.active_step.activity
            return super().count_eligible_tweets_for_tagging()

    with reporter:
        result = await tagging.tag_pending_media_tweets(
            ObservedStore([]),
            make_config(),
            paths,
            console,
        )

    assert result == tagging.TaggingRunResult()


@pytest.mark.asyncio
async def test_pipeline_tagging_total_respects_remaining_daily_request_capacity(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = PendingTagStore(["1", "2", "3", "4", "5"])
    console, _ = make_console()
    reporter = PipelineReporter(console, "tag", interactive=False)
    monkeypatch.setattr(
        tagging,
        "get_rpd_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            allowed=True,
            used=2,
            limit=3,
            remaining=1,
        ),
    )

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        return len(kwargs["tweet_ids"])

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)

    with reporter:
        result = await tagging.tag_pending_media_tweets(
            store,
            make_config(batch=True, limit=2, rpd=3),
            paths,
            console,
        )

    assert result == tagging.TaggingRunResult(processed=2, tagged=2, batches=1)
    assert store.remaining == ["3", "4", "5"]
    step = reporter._step_by_key["tagging"]
    assert step.total == 2
    assert step.show_eta is False
    assert step.show_rate is False
    assert "1/3 daily requests available" in step.detail


@pytest.mark.asyncio
async def test_pending_tagging_loops_through_full_and_short_batches(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = PendingTagStore(["1", "2", "3", "4", "5"])
    calls: list[dict[str, Any]] = []

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        calls.append(kwargs)
        return len(kwargs["tweet_ids"])

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    console, _ = make_console()

    result = await tagging.tag_pending_media_tweets(
        store,
        make_config(batch=True, limit=2),
        paths,
        console,
    )

    assert result == tagging.TaggingRunResult(processed=5, tagged=5, batches=3)
    assert store.selection_limits == [2, 2, 2]
    assert [call["tweet_ids"] for call in calls] == [["1", "2"], ["3", "4"], ["5"]]


@pytest.mark.asyncio
async def test_pending_tagging_limit_counts_complete_batches(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = PendingTagStore([str(index) for index in range(1, 9)])
    selected_batches: list[list[str]] = []

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        selected_batches.append(kwargs["tweet_ids"])
        return len(kwargs["tweet_ids"])

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    console, _ = make_console()

    result = await tagging.tag_pending_media_tweets(
        store,
        make_config(batch=True, limit=3),
        paths,
        console,
        batch_limit=2,
    )

    assert result == tagging.TaggingRunResult(processed=6, tagged=6, batches=2)
    assert store.selection_limits == [3, 3]
    assert selected_batches == [["1", "2", "3"], ["4", "5", "6"]]
    assert store.remaining == ["7", "8"]


@pytest.mark.asyncio
async def test_pending_tagging_batch_size_overrides_disabled_config_batching(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = PendingTagStore(["1", "2", "3", "4", "5"])
    selected_batches: list[list[str]] = []

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        selected_batches.append(kwargs["tweet_ids"])
        return len(kwargs["tweet_ids"])

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    console, _ = make_console()

    result = await tagging.tag_pending_media_tweets(
        store,
        make_config(batch=False, limit=3),
        paths,
        console,
        batch_size=3,
    )

    assert result == tagging.TaggingRunResult(processed=5, tagged=5, batches=2)
    assert store.selection_limits == [3, 3]
    assert selected_batches == [["1", "2", "3"], ["4", "5"]]


@pytest.mark.asyncio
async def test_pending_tagging_without_batching_processes_one_tweet_per_request(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = PendingTagStore(["1", "2", "3"])
    selected_batches: list[list[str]] = []

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        selected_batches.append(kwargs["tweet_ids"])
        return len(kwargs["tweet_ids"])

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    console, _ = make_console()

    result = await tagging.tag_pending_media_tweets(
        store,
        make_config(batch=False, limit=20),
        paths,
        console,
    )

    assert result == tagging.TaggingRunResult(processed=3, tagged=3, batches=3)
    assert store.selection_limits == [1, 1, 1, 1]
    assert selected_batches == [["1"], ["2"], ["3"]]


@pytest.mark.parametrize("tagged_batch", [0, 2])
@pytest.mark.asyncio
async def test_pending_tagging_stops_after_zero_or_partial_batch(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    tagged_batch: int,
) -> None:
    store = PendingTagStore(["1", "2", "3", "4", "5", "6"])
    calls: list[list[str]] = []

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        calls.append(kwargs["tweet_ids"])
        return tagged_batch

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    console, _ = make_console()

    result = await tagging.tag_pending_media_tweets(
        store,
        make_config(batch=True, limit=3),
        paths,
        console,
    )

    assert result == tagging.TaggingRunResult(processed=3, tagged=tagged_batch, batches=1)
    assert store.selection_limits == [3]
    assert calls == [["1", "2", "3"]]
    assert store.remaining == ["4", "5", "6"]


@pytest.mark.asyncio
async def test_pending_tagging_dry_run_forces_one_tweet_and_one_batch(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = PendingTagStore(["1", "2", "3"])
    calls: list[dict[str, Any]] = []

    async def fake_tag_media_tweets(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(tagging, "tag_media_tweets", fake_tag_media_tweets)
    console, _ = make_console()

    result = await tagging.tag_pending_media_tweets(
        store,
        make_config(batch=True, limit=20),
        paths,
        console,
        batch_limit=10,
        batch_size=7,
        model_override="gemini-test",
        dry_run=True,
    )

    assert result == tagging.TaggingRunResult(processed=1, tagged=1, batches=1)
    assert store.selection_limits == [1]
    assert len(calls) == 1
    assert calls[0]["tweet_ids"] == ["1"]
    assert calls[0]["model_override"] == "gemini-test"
    assert calls[0]["dry_run"] is True
    assert store.remaining == ["2", "3"]


@pytest.mark.parametrize(
    "tag_config",
    [
        TaggingConfig(enabled=False, api_key="present"),
        TaggingConfig(enabled=True, api_key=None),
        TaggingConfig(enabled=True, api_key=""),
    ],
)
@pytest.mark.asyncio
async def test_disabled_or_missing_api_key_does_not_create_client(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    tag_config: TaggingConfig,
) -> None:
    monkeypatch.setattr(
        tagging.genai,
        "Client",
        lambda **kwargs: pytest.fail(f"client created unexpectedly: {kwargs}"),
    )
    console, output = make_console()

    count = await tagging.tag_media_tweets(
        FakeStore(), AppConfig(tagging=tag_config), paths, console, ["1"]
    )

    assert count == 0
    assert "disabled or missing API key" in output.getvalue()


@pytest.mark.asyncio
async def test_empty_tweet_ids_does_not_create_client(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    monkeypatch.setattr(
        tagging.genai,
        "Client",
        lambda **kwargs: pytest.fail(f"client created unexpectedly: {kwargs}"),
    )
    console, _ = make_console()

    assert await tagging.tag_media_tweets(FakeStore(), make_config(), paths, console, []) == 0


@pytest.mark.asyncio
async def test_client_creation_failure_is_reported_without_leaking_api_key(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    monkeypatch.setattr(
        tagging.genai,
        "Client",
        lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"invalid credential {kwargs['api_key']}")
        ),
    )
    console, output = make_console()

    count = await tagging.tag_media_tweets(FakeStore(), make_config(), paths, console, ["1"])

    assert count == 0
    assert "top-secret-api-key" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()


@pytest.mark.asyncio
async def test_remote_files_are_cleared_and_cleanup_warning_is_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    files = FakeFiles(listed=["files/old-a", "files/old-b"])
    client = FakeClient([response(successful_result())], files=files)
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 1
    assert files.deleted == ["files/old-a", "files/old-b"]

    warning_files = FakeFiles(list_error=RuntimeError("cleanup unavailable"))
    warning_client = FakeClient([response(successful_result())], files=warning_files)
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: warning_client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 1
    assert "Could not clear Gemini File API" in output.getvalue()


@pytest.mark.parametrize(
    ("seed_kind", "expected_message"),
    [
        ("no_tweet", "No loadable media found"),
        ("no_media", "No loadable media found"),
        ("no_local_path", "No loadable media found"),
        ("missing_file", "No loadable media found"),
        ("corrupt_image", "Failed to load image"),
        ("oversize", "exceeds limit"),
    ],
)
@pytest.mark.asyncio
async def test_unusable_media_is_skipped_without_model_request(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    seed_kind: str,
    expected_message: str,
) -> None:
    store = FakeStore()
    if seed_kind != "no_tweet":
        store.add_tweet("1")
    if seed_kind not in {"no_tweet", "no_media"}:
        local_path = None if seed_kind == "no_local_path" else "media/photo.png"
        store.add_media("1", local_path=local_path)
    media_path = paths.data_dir / "media" / "photo.png"
    if seed_kind == "corrupt_image":
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"not an image")
    elif seed_kind == "oversize":
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"x" * (1024 * 1024 + 1))

    client = FakeClient([])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()
    config = make_config(max_media_size_mb=1) if seed_kind == "oversize" else make_config()

    assert await tagging.tag_media_tweets(store, config, paths, console, ["1"]) == 0
    assert client.models.calls == []
    assert expected_message in output.getvalue()


@pytest.mark.asyncio
async def test_invalid_tweet_raw_json_does_not_prevent_media_tagging(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    store.add_tweet("1", raw_json="{not-json")
    store.add_media("1")
    write_image(paths.data_dir / "media" / "photo.png")
    client = FakeClient([response(successful_result())])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 1
    assert "invalid stored tweet JSON" in output.getvalue()


@pytest.mark.asyncio
async def test_prompt_uses_data_dir_tweet_context_existing_tags_and_model_override(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    store.add_tweet(
        "1",
        text='A quoted "caption"',
        raw_json=json.dumps({"legacy": {"quoted_status_id_str": "99"}}),
        author_display_name="Display Name",
        author_username="handle",
    )
    store.add_media("1", local_path="nested/photo.png")
    store.tag_counts = [{"tag": "Existing Tag", "count": 4}]
    write_image(paths.data_dir / "nested" / "photo.png")
    client = FakeClient([response(successful_result())])
    supplied_keys: list[str] = []
    monkeypatch.setattr(
        tagging.genai,
        "Client",
        lambda **kwargs: supplied_keys.append(kwargs["api_key"]) or client,
    )
    console, _ = make_console()

    count = await tagging.tag_media_tweets(
        store,
        make_config(model="configured-model"),
        paths,
        console,
        ["1"],
        model_override="override-model",
    )

    assert count == 1
    assert supplied_keys == ["top-secret-api-key"]
    call = client.models.calls[0]
    assert call["model"] == "override-model"
    string_parts = "".join(part for part in call["contents"] if isinstance(part, str))
    assert '"Existing Tag"' in string_parts
    assert "[ID: 1]" in string_parts
    assert "Type: Quote Tweet" in string_parts
    assert "Author: Display Name (@handle)" in string_parts
    assert 'Text: "A quoted \\"caption\\""' in string_parts
    assert "[Attached Image: media-1]" in string_parts
    assert any(isinstance(part, Image.Image) for part in call["contents"])


@pytest.mark.asyncio
async def test_reply_context_and_empty_existing_tags_are_in_prompt(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    store.add_tweet(
        "1",
        raw_json=json.dumps({"legacy": {"in_reply_to_status_id_str": "2"}}),
    )
    store.add_media("1")
    write_image(paths.data_dir / "media" / "photo.png")
    client = FakeClient([response(successful_result())])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"])

    string_parts = "".join(
        part for part in client.models.calls[0]["contents"] if isinstance(part, str)
    )
    assert "Type: Reply" in string_parts
    assert "None yet (create new tags as needed)." in string_parts


@pytest.mark.asyncio
async def test_dry_run_prints_validated_preview_without_saving_and_counts_rpd(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    store.add_tweet(
        "42",
        text="A locally archived tweet",
        raw_json=json.dumps({"legacy": {"in_reply_to_status_id_str": "41"}}),
        author_display_name="Preview Author",
        author_username="preview_user",
    )
    store.add_media("42", local_path="preview/photo.png")
    write_image(paths.data_dir / "preview" / "photo.png")
    client = FakeClient(
        [
            response(
                [
                    {
                        "id": "42",
                        "description": "A validated media description.",
                        "tags": ["  deadLOCK ", "ivy (deadLOCK)"],
                        "raw_debug": "RAW_RESPONSE_SENTINEL",
                    }
                ]
            )
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(rpd=5),
            paths,
            console,
            ["42"],
            dry_run=True,
        )
        == 1
    )

    preview = output.getvalue()
    assert "Tweet 42 (Reply)" in preview
    assert "Author: Preview Author (@preview_user)" in preview
    assert "Text: A locally archived tweet" in preview
    assert "Description: A validated media description." in preview
    assert "Tags: Deadlock, Ivy (Deadlock)" in preview
    assert "RAW_RESPONSE_SENTINEL" not in preview
    assert "no media tags were saved" in preview
    assert store.media_tag("42") is None
    assert rpd_used(store, limit=5) == 1


@pytest.mark.asyncio
async def test_dry_run_does_not_overwrite_an_existing_media_tag(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    original_payload = json.dumps({"description": "Original description", "tags": ["Original Tag"]})
    store.conn.execute(
        "INSERT INTO archive "
        "(row_key, record_type, tweet_id, raw_json, enrichment_state, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("media_tag:1", "media_tag", "1", original_payload, "done", "original-time"),
    )
    store.conn.commit()
    client = FakeClient([response(successful_result())])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(),
            paths,
            console,
            ["1"],
            dry_run=True,
        )
        == 1
    )

    row = store.media_tag("1")
    assert row is not None
    assert row["raw_json"] == original_payload
    assert row["enrichment_state"] == "done"
    assert row["updated_at"] == "original-time"


@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("400 INVALID_ARGUMENT"), empty_response()],
)
@pytest.mark.asyncio
async def test_dry_run_model_rejection_does_not_create_a_failure_marker(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    outcome: object,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([outcome])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(rpd=5),
            paths,
            console,
            ["1"],
            dry_run=True,
        )
        == 0
    )

    assert store.media_tag("1") is None
    assert rpd_used(store, limit=5) == 1
    assert "Test result was not saved" in output.getvalue()


@pytest.mark.asyncio
async def test_dry_run_rejects_multiple_tweet_ids_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    monkeypatch.setattr(
        tagging.genai,
        "Client",
        lambda **kwargs: pytest.fail(f"client created unexpectedly: {kwargs}"),
    )
    console, _ = make_console()

    with pytest.raises(ValueError, match="requires exactly one tweet"):
        await tagging.tag_media_tweets(
            FakeStore(),
            make_config(),
            paths,
            console,
            ["1", "2"],
            dry_run=True,
        )


@pytest.mark.parametrize("media_type", ["video", "animated_gif"])
@pytest.mark.asyncio
async def test_video_media_types_upload_poll_and_delete(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    media_type: str,
) -> None:
    store = FakeStore()
    store.add_tweet("1")
    store.add_media(
        "1",
        media_type=media_type,
        local_path="media/clip.mp4",
    )
    video_path = paths.data_dir / "media" / "clip.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video bytes")
    files = FakeFiles(states={"files/clip.mp4": ["PROCESSING", "ACTIVE"]})
    client = FakeClient([response(successful_result())], files=files)
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, _ = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 1
    assert files.uploaded == [str(video_path)]
    assert files.get_calls == ["files/clip.mp4", "files/clip.mp4"]
    assert sleeps == [10]
    assert files.deleted == ["files/clip.mp4"]
    assert any(
        getattr(part, "name", None) == "files/clip.mp4"
        for part in client.models.calls[0]["contents"]
    )


@pytest.mark.asyncio
async def test_failed_video_processing_is_not_sent_and_is_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    store.add_tweet("1")
    store.add_media("1", media_type="video", local_path="media/clip.mp4")
    video_path = paths.data_dir / "media" / "clip.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video bytes")
    files = FakeFiles(states={"files/clip.mp4": ["FAILED"]})
    client = FakeClient([], files=files)
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    assert client.models.calls == []
    assert files.deleted == ["files/clip.mp4"]
    assert "failed to process video" in output.getvalue()


@pytest.mark.asyncio
async def test_video_upload_and_poll_errors_are_secret_safe_and_clean_up(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    store.add_tweet("1")
    store.add_media("1", media_type="video", local_path="media/clip.mp4")
    video_path = paths.data_dir / "media" / "clip.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video bytes")

    upload_files = FakeFiles(upload_error=RuntimeError("top-secret-api-key rejected"))
    upload_client = FakeClient([], files=upload_files)
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: upload_client)
    console, output = make_console()
    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    assert "top-secret-api-key" not in output.getvalue()

    class PollErrorFiles(FakeFiles):
        def get(self, *, name: str) -> SimpleNamespace:
            raise RuntimeError("poll top-secret-api-key failed")

    poll_files = PollErrorFiles()
    poll_client = FakeClient([], files=poll_files)
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: poll_client)
    console, output = make_console()
    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    assert "top-secret-api-key" not in output.getvalue()
    assert poll_files.deleted == ["files/clip.mp4"]


@pytest.mark.parametrize(
    "google_search",
    [False, True],
)
@pytest.mark.asyncio
async def test_generation_config_wires_grounding_schema_and_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    google_search: bool,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([response(successful_result())])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    await tagging.tag_media_tweets(
        store,
        make_config(google_search=google_search, thinking_level="medium"),
        paths,
        console,
        ["1"],
    )

    request_config = client.models.calls[0]["config"]
    assert request_config.response_mime_type == "application/json"
    assert request_config.response_schema == list[tagging.TagResult]
    assert bool(request_config.tools) is google_search
    assert request_config.thinking_config is not None
    if "thinking_level" in request_config.thinking_config.__class__.model_fields:
        assert request_config.thinking_config.thinking_level.value == "MEDIUM"
    else:
        assert request_config.thinking_config.thinking_budget == 4096


@pytest.mark.asyncio
async def test_local_generation_config_failure_does_not_consume_rpd(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    monkeypatch.setattr(
        tagging.types,
        "GenerateContentConfig",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("local config failure")),
    )
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(rpd=5), paths, console, ["1"]) == 0
    assert client.models.calls == []
    assert rpd_used(store, limit=5) == 0
    assert "local config failure" in output.getvalue()


@pytest.mark.asyncio
async def test_generation_disables_sdk_retries_and_unlimited_mode_writes_no_quota_state(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([response(successful_result())])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    assert await tagging.tag_media_tweets(store, make_config(rpd=None), paths, console, ["1"]) == 1
    assert len(client.models.calls) == 1
    request_config = client.models.calls[0]["config"]
    assert request_config.http_options.retry_options.attempts == 1
    assert (
        store.conn.execute(
            "SELECT count(*) FROM archive WHERE row_key LIKE 'metadata:gemini_rpd:%'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_successful_multi_tweet_batch_costs_one_request(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    client = FakeClient(
        [
            response(
                [
                    successful_result("1")[0],
                    successful_result("2")[0],
                ]
            )
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(rpd=5),
            paths,
            console,
            ["1", "2"],
        )
        == 2
    )
    assert len(client.models.calls) == 1
    assert rpd_used(store, limit=5) == 1


@pytest.mark.parametrize(
    "error_text",
    [
        "400 bad request",
        "INVALID_ARGUMENT schema",
        "429 quota",
        "RESOURCE_EXHAUSTED quota",
    ],
)
@pytest.mark.asyncio
async def test_grounding_failures_retry_immediately_without_search(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    error_text: str,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient(
        [
            RuntimeError(error_text),
            response(successful_result()),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, output = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(google_search=True),
            paths,
            console,
            ["1"],
        )
        == 1
    )
    assert len(client.models.calls) == 2
    assert client.models.calls[0]["config"].tools
    assert not client.models.calls[1]["config"].tools
    assert sleeps == []
    assert "Retrying immediately without Search" in output.getvalue()


@pytest.mark.parametrize(
    ("error_text", "reason"),
    [
        ("429 busy", "429"),
        ("RESOURCE_EXHAUSTED quota", "429"),
        ("503 busy", "503"),
    ],
)
@pytest.mark.asyncio
async def test_retryable_generation_errors_use_only_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    error_text: str,
    reason: str,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient(
        [
            RuntimeError(error_text),
            RuntimeError(error_text),
            response(successful_result()),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, output = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(rpd=20),
            paths,
            console,
            ["1"],
        )
        == 1
    )
    assert sleeps == [15, 30]
    assert len(client.models.calls) == 3
    assert rpd_used(store, limit=20) == 3
    assert f"Gemini API busy ({reason})" in output.getvalue()


@pytest.mark.asyncio
async def test_rpd_cap_reached_mid_retry_skips_next_sleep_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient(
        [
            RuntimeError("503 busy"),
            RuntimeError("503 still busy"),
            response(successful_result()),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(rpd=2), paths, console, ["1"]) == 0
    assert len(client.models.calls) == 2
    assert sleeps == [15]
    assert rpd_used(store, limit=2) == 2
    assert store.media_tag("1") is None
    assert "daily request limit reached" in output.getvalue()


@pytest.mark.asyncio
async def test_grounding_fallback_stops_at_cap_without_sleep_or_failure_marker(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient(
        [
            RuntimeError("400 grounding incompatible"),
            response(successful_result()),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, output = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(google_search=True, rpd=1),
            paths,
            console,
            ["1"],
        )
        == 0
    )
    assert len(client.models.calls) == 1
    assert sleeps == []
    assert rpd_used(store, limit=1) == 1
    assert store.media_tag("1") is None
    assert "Retrying immediately without Search" in output.getvalue()
    assert "daily request limit reached" in output.getvalue()


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_zero_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([RuntimeError("503 top-secret-api-key unavailable") for _ in range(5)])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    assert sleeps == [15, 30, 60, 120]
    assert len(client.models.calls) == 5
    assert "top-secret-api-key" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()


@pytest.mark.asyncio
async def test_invalid_argument_batch_is_recursively_split_and_model_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    client = FakeClient(
        [
            RuntimeError("400 INVALID_ARGUMENT payload too large"),
            response(successful_result("1")),
            response(successful_result("2")),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    count = await tagging.tag_media_tweets(
        store,
        make_config(),
        paths,
        console,
        ["1", "2"],
        model_override="override-model",
    )

    assert count == 2
    assert [call["model"] for call in client.models.calls] == [
        "override-model",
        "override-model",
        "override-model",
    ]
    assert store.media_tag("1")["enrichment_state"] == "done"
    assert store.media_tag("2")["enrichment_state"] == "done"
    assert "Splitting into batches of 1 and 1" in output.getvalue()


@pytest.mark.parametrize(
    "parent_outcome",
    [RuntimeError("400 INVALID_ARGUMENT payload too large"), empty_response()],
)
@pytest.mark.asyncio
async def test_recursive_split_stops_at_cap_without_marking_unattempted_child_failed(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    parent_outcome: object,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    client = FakeClient(
        [
            parent_outcome,
            response(successful_result("1")),
        ]
    )
    client_creations = 0

    def make_client(**kwargs: Any) -> FakeClient:
        nonlocal client_creations
        client_creations += 1
        return client

    monkeypatch.setattr(tagging.genai, "Client", make_client)
    console, output = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(rpd=2),
            paths,
            console,
            ["1", "2"],
        )
        == 1
    )
    assert client_creations == 2
    assert len(client.models.calls) == 2
    assert rpd_used(store, limit=2) == 2
    assert store.media_tag("1")["enrichment_state"] == "done"
    assert store.media_tag("2") is None
    assert "daily request limit reached" in output.getvalue()


@pytest.mark.asyncio
async def test_already_exhausted_rpd_preflight_does_not_create_client(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    assert reserve_rpd_request(store, model="gemini-default", limit=1).allowed
    client_creations = 0

    def make_client(**kwargs: Any) -> FakeClient:
        nonlocal client_creations
        client_creations += 1
        return FakeClient([response(successful_result())])

    monkeypatch.setattr(tagging.genai, "Client", make_client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(rpd=1), paths, console, ["1"]) == 0
    assert client_creations == 0
    assert rpd_used(store, limit=1) == 1
    assert store.media_tag("1") is None
    assert "daily request limit reached" in output.getvalue()


@pytest.mark.asyncio
async def test_model_override_uses_an_independent_rpd_counter(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    client = FakeClient(
        [
            response(successful_result("1")),
            response(successful_result("2")),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()
    config = make_config(rpd=1)

    assert (
        await tagging.tag_media_tweets(
            store,
            config,
            paths,
            console,
            ["1"],
            model_override="gemini-override",
        )
        == 1
    )
    assert await tagging.tag_media_tweets(store, config, paths, console, ["2"]) == 1
    assert [call["model"] for call in client.models.calls] == [
        "gemini-override",
        "gemini-default",
    ]
    assert rpd_used(store, model="gemini-override", limit=1) == 1
    assert rpd_used(store, model="gemini-default", limit=1) == 1


@pytest.mark.asyncio
async def test_rpd_reservation_failure_fails_closed_and_redacts_credentials(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    underlying_connection = store.conn
    failing_connection = FailingReservationConnection(underlying_connection)
    store.conn = failing_connection
    client = FakeClient([response(successful_result())])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(rpd=1), paths, console, ["1"]) == 0
    assert client.models.calls == []
    assert failing_connection.rollbacks == 1
    assert store.media_tag("1") is None
    assert "request not sent" in output.getvalue()
    assert "top-secret-api-key" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()


@pytest.mark.asyncio
async def test_empty_batch_response_is_recursively_split(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    client = FakeClient(
        [
            empty_response(),
            response(successful_result("1")),
            response(successful_result("2")),
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1", "2"]) == 2
    assert "empty response" in output.getvalue()
    assert "Reason: MAX_TOKENS" in output.getvalue()


@pytest.mark.parametrize(
    "outcome",
    [RuntimeError("400 INVALID_ARGUMENT"), empty_response()],
)
@pytest.mark.asyncio
async def test_singleton_model_rejection_is_marked_failed(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    outcome: object,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([outcome])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    row = store.media_tag("1")
    assert row is not None
    assert row["enrichment_state"] == "failed"
    assert json.loads(row["raw_json"]) == {}


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"id": "1", "description": "description", "tags": ["a", "b"]}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_top_level_output_returns_zero_without_writes(
    monkeypatch: pytest.MonkeyPatch,
    paths,
    payload: str,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient([response(payload)])
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    assert store.media_tag("1") is None
    assert "invalid JSON response" in output.getvalue()


@pytest.mark.asyncio
async def test_partial_output_stores_only_valid_requested_results(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    client = FakeClient(
        [
            response(
                [
                    {
                        "id": "1",
                        "description": "Valid result",
                        "tags": ["API", "npc (GTA V)"],
                    },
                    {"id": "2", "description": "Missing tags"},
                    {
                        "id": "not-requested",
                        "description": "Ignore this",
                        "tags": ["One", "Two"],
                    },
                    "not an object",
                ]
            )
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1", "2"]) == 1
    payload = json.loads(store.media_tag("1")["raw_json"])
    assert payload == {
        "description": "Valid result",
        "tags": ["Api", "Npc (Gta V)"],
    }
    assert store.media_tag("2") is None
    assert "Skipping invalid Gemini result" in output.getvalue()


@pytest.mark.asyncio
async def test_later_result_write_failure_rolls_back_batch_and_closes_all_images(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir, "1")
    prepare_photo(store, paths.data_dir, "2")
    connection = store.conn
    failing_connection = FailingTagConnection(connection, fail_on_write=2)
    store.conn = failing_connection
    images = [TrackingImage(), TrackingImage()]
    monkeypatch.setattr(tagging.Image, "open", lambda path: images.pop(0))
    opened_images = list(images)
    client = FakeClient(
        [
            response(
                [
                    {
                        "id": "1",
                        "description": "First valid result",
                        "tags": ["One", "Two"],
                    },
                    {
                        "id": "2",
                        "description": "Second valid result",
                        "tags": ["Three", "Four"],
                    },
                ]
            )
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, output = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1", "2"]) == 0
    assert failing_connection.media_tag_writes == 2
    assert failing_connection.rollbacks == 1
    assert (
        connection.execute(
            "SELECT count(*) FROM archive WHERE record_type = 'media_tag'"
        ).fetchone()[0]
        == 0
    )
    assert all(image.loaded for image in opened_images)
    assert all(image.closed for image in opened_images)
    assert "top-secret-api-key" not in output.getvalue()
    assert "[REDACTED]" in output.getvalue()


@pytest.mark.asyncio
async def test_tag_schema_rejects_out_of_contract_tag_counts(
    monkeypatch: pytest.MonkeyPatch,
    paths,
) -> None:
    store = FakeStore()
    prepare_photo(store, paths.data_dir)
    client = FakeClient(
        [
            response(
                [
                    {
                        "id": "1",
                        "description": "Only one tag",
                        "tags": ["lonely"],
                    }
                ]
            )
        ]
    )
    monkeypatch.setattr(tagging.genai, "Client", lambda **kwargs: client)
    console, _ = make_console()

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 0
    assert store.media_tag("1") is None
