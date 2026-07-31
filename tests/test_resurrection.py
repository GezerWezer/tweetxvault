from __future__ import annotations

from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

import tweetxvault.archive_import as archive_import
from tweetxvault.exceptions import TerminalUnavailableError
from tweetxvault.storage import open_archive_store


@pytest.mark.asyncio
async def test_resurrect_dead_tweets_forwards_context_and_maps_result(
    paths,
    config,
    auth_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_enrich_pending_rows(**kwargs):
        captured.update(kwargs)
        return 4, 2, 1, 7

    monkeypatch.setattr(
        archive_import,
        "resolve_job_context",
        lambda **_kwargs: (config, paths),
    )
    monkeypatch.setattr(archive_import, "_enrich_pending_rows", fake_enrich_pending_rows)

    result = await archive_import.resurrect_dead_tweets(
        limit=25,
        config=config,
        paths=paths,
        auth_bundle=auth_bundle,
    )

    assert captured["limit"] == 25
    assert captured["config"] is config
    assert captured["paths"] is paths
    assert captured["auth_bundle"] is auth_bundle
    assert captured["resurrect_dead"] is True
    assert result.reconciled_collections == []
    assert result.detail_lookups == 4
    assert result.detail_terminal_unavailable == 2
    assert result.detail_transient_failures == 1
    assert result.pending_enrichment == 7


@pytest.mark.asyncio
async def test_resurrect_dead_tweets_resolves_auth_when_not_supplied(
    paths,
    config,
    auth_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        archive_import,
        "resolve_job_context",
        lambda **_kwargs: (config, paths),
    )
    monkeypatch.setattr(archive_import, "resolve_auth_bundle", lambda _config: auth_bundle)

    async def fake_enrich_pending_rows(**kwargs):
        assert kwargs["auth_bundle"] is auth_bundle
        return 0, 0, 0, 0

    monkeypatch.setattr(archive_import, "_enrich_pending_rows", fake_enrich_pending_rows)

    result = await archive_import.resurrect_dead_tweets(
        limit=None,
        config=config,
        paths=paths,
    )

    assert result == archive_import.ArchiveEnrichResult()


@pytest.mark.asyncio
async def test_dead_tweet_enrichment_nonpositive_limit_only_counts_remaining(
    paths,
    config,
    auth_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SimpleNamespace(count_dead_tweets_for_resurrection=lambda: 9)

    @asynccontextmanager
    async def fake_locked_archive_job(**_kwargs):
        yield SimpleNamespace(store=store)

    monkeypatch.setattr(archive_import, "locked_archive_job", fake_locked_archive_job)

    result = await archive_import._enrich_pending_rows(
        limit=0,
        config=config,
        paths=paths,
        auth_bundle=auth_bundle,
        transport=None,
        console=Console(file=StringIO(), force_terminal=False),
        resurrect_dead=True,
    )

    assert result == (0, 0, 0, 9)


@pytest.mark.asyncio
async def test_terminal_unavailable_resurrection_is_counted_as_terminal(
    paths,
    config,
    auth_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updates: list[dict[str, object]] = []
    dirty: list[tuple[int, int]] = []

    class FakeStore:
        def list_dead_tweets_for_resurrection(self, *, limit):
            assert limit == 1
            return [{"tweet_id": "dead"}]

        def update_tweet_object_enrichment(self, tweet_id, **kwargs):
            updates.append({"tweet_id": tweet_id, **kwargs})

        def count_dead_tweets_for_resurrection(self):
            return 1

    class FakeClient:
        async def aclose(self) -> None:
            return None

    job = SimpleNamespace(
        store=FakeStore(),
        mark_dirty=lambda *, rows, batches: dirty.append((rows, batches)),
    )

    @asynccontextmanager
    async def fake_locked_archive_job(**_kwargs):
        yield job

    async def fake_resolve_query_ids(*_args, **_kwargs):
        return {"TweetDetail": "query-id"}

    async def fake_fetch_page(*_args, **_kwargs):
        raise TerminalUnavailableError("gone")

    monkeypatch.setattr(archive_import, "locked_archive_job", fake_locked_archive_job)
    monkeypatch.setattr(archive_import, "resolve_query_ids", fake_resolve_query_ids)
    monkeypatch.setattr(
        archive_import, "build_async_client", lambda *_args, **_kwargs: FakeClient()
    )
    monkeypatch.setattr(archive_import, "fetch_page", fake_fetch_page)
    monkeypatch.setattr(archive_import, "_flush_buffer", lambda *_args, **_kwargs: 1)

    result = await archive_import._enrich_pending_rows(
        limit=1,
        config=config,
        paths=paths,
        auth_bundle=auth_bundle,
        transport=None,
        console=Console(file=StringIO(), force_terminal=False),
        resurrect_dead=True,
    )

    assert result == (0, 1, 0, 1)
    assert updates[0]["tweet_id"] == "dead"
    assert updates[0]["enrichment_state"] == "terminal_unavailable"
    assert updates[0]["enrichment_http_status"] == 404
    assert dirty == [(1, 1)]


def test_dead_tweet_storage_helpers_order_limit_count_and_persist(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    store._merge_records(
        [
            store._record(
                row_key="tweet_object:newer",
                record_type="tweet_object",
                tweet_id="newer",
                enrichment_state="terminal_unavailable",
                enrichment_checked_at="2026-02-01T00:00:00+00:00",
            ),
            store._record(
                row_key="tweet_object:older",
                record_type="tweet_object",
                tweet_id="older",
                enrichment_state="terminal_unavailable",
                enrichment_checked_at="2026-01-01T00:00:00+00:00",
            ),
            store._record(
                row_key="tweet_object:pending",
                record_type="tweet_object",
                tweet_id="pending",
                enrichment_state="pending",
            ),
        ]
    )

    assert store.count_dead_tweets_for_resurrection() == 2
    assert store.list_dead_tweets_for_resurrection(limit=1) == [
        {
            "tweet_id": "older",
            "enrichment_checked_at": "2026-01-01T00:00:00+00:00",
        }
    ]

    store.persist_terminal_unavailable_target("missing", "ThreadExpandDetail")

    object_row = store._get_row("tweet_object:missing")
    captures = store._query(
        expr="record_type = 'raw_capture' AND cursor_in = 'missing'",
        cols=["operation", "http_status", "source", "raw_json"],
    )
    assert object_row is not None
    assert object_row["enrichment_state"] == "terminal_unavailable"
    assert object_row["source"] == "live_graphql"
    assert captures == [
        {
            "operation": "ThreadExpandDetail",
            "http_status": "404",
            "source": "live_graphql",
            "raw_json": '{"__tombstone__": true}',
        }
    ]
    store.close()
