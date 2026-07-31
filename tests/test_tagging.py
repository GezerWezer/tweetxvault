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
                updated_at TEXT
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
async def test_retryable_generation_errors_use_exponential_backoff(
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

    assert await tagging.tag_media_tweets(store, make_config(), paths, console, ["1"]) == 1
    assert sleeps == [15, 30]
    assert f"Gemini API busy ({reason})" in output.getvalue()


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


@pytest.mark.asyncio
async def test_rpd_paces_multiple_generation_requests(
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
    monkeypatch.setattr(tagging.time, "monotonic", lambda: 100.0)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tagging.asyncio, "sleep", fake_sleep)
    console, _ = make_console()

    assert (
        await tagging.tag_media_tweets(
            store,
            make_config(google_search=True, rpd=86_400),
            paths,
            console,
            ["1"],
        )
        == 1
    )
    assert sleeps == [1.0]
