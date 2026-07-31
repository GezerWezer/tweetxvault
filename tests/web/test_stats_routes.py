from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tweetxvault.storage.backend import ArchiveCollectionStats, ArchiveStats
from tweetxvault.web.routes import stats as stats_routes


def _stats_store() -> SimpleNamespace:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE archive (
            record_type TEXT,
            tweet_id TEXT,
            author_id TEXT,
            created_at_ts INTEGER,
            key TEXT,
            value TEXT,
            enrichment_state TEXT,
            conversation_id TEXT,
            status TEXT,
            raw_json TEXT
        )
        """
    )
    return SimpleNamespace(conn=conn)


def test_summary_reports_counts_ranges_owner_and_latest_sync(make_web_client) -> None:
    store = _stats_store()
    store.conn.executemany(
        """
        INSERT INTO archive (
            record_type, tweet_id, author_id, created_at_ts, key, value
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("tweet", "t1", "a1", 1_700_000_000, None, None),
            ("tweet", "t2", "a2", 1_800_000_000, None, None),
            ("article", None, None, None, None, None),
            ("media", "t1", "a1", None, None, None),
            ("url", "t1", None, None, None, None),
            ("sync_state", None, None, 1_750_000_000, None, None),
            ("metadata", None, None, None, "owner_user_id", "owner-42"),
        ],
    )
    client = make_web_client(stats_routes.router, store=store)

    response = client.get("/api/stats/summary")

    assert response.status_code == 200
    assert response.json() == {
        "owner_user_id": "owner-42",
        "unique_posts": 2,
        "articles": 1,
        "media_rows": 1,
        "urls": 1,
        "profiles": 2,
        "oldest_post": "Nov 14, 2023",
        "newest_post": "Jan 15, 2027",
        "latest_sync": "Jun 15, 2025",
    }


def test_summary_uses_empty_defaults(make_web_client) -> None:
    store = _stats_store()
    client = make_web_client(stats_routes.router, store=store)

    data = client.get("/api/stats/summary").json()

    assert data == {
        "owner_user_id": "Local Vault",
        "unique_posts": 0,
        "articles": 0,
        "media_rows": 0,
        "urls": 0,
        "profiles": 0,
        "oldest_post": None,
        "newest_post": None,
        "latest_sync": None,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "-"),
        ("", "-"),
        ("2026-03-14T01:02:00", "Mar 14, 2026 1:02 am"),
        ("not-a-date", "not-a-date"),
    ],
)
def test_format_stats_timestamp(raw: str | None, expected: str) -> None:
    assert stats_routes._format_stats_timestamp(raw) == expected


def test_collections_formats_names_and_backfill_states(make_web_client) -> None:
    archive_stats = ArchiveStats(
        collections=[
            ArchiveCollectionStats(
                collection_type="bookmark",
                post_count=3,
                backfill_incomplete=True,
                backfill_cursor="cursor",
            ),
            ArchiveCollectionStats(
                collection_type="like",
                post_count=2,
                backfill_incomplete=True,
            ),
            ArchiveCollectionStats(
                collection_type="tweet",
                post_count=1,
                backfill_cursor="saved",
            ),
            ArchiveCollectionStats(collection_type="custom", post_count=4),
        ]
    )
    store = SimpleNamespace(archive_stats=lambda: archive_stats)
    client = make_web_client(stats_routes.router, store=store)

    response = client.get("/api/stats/collections")

    assert response.status_code == 200
    rows = response.json()
    assert [(row["collection"], row["count"], row["backfill_status"]) for row in rows] == [
        ("Bookmarks", 3, "resume older"),
        ("Likes", 2, "incomplete"),
        ("Authored Tweets", 1, "saved cursor"),
        ("Custom", 4, "none saved"),
    ]
    assert all(row["oldest"] == row["newest"] == row["last_synced"] == "-" for row in rows)


def test_health_reports_pipeline_counts(make_web_client) -> None:
    store = _stats_store()
    store.conn.executemany(
        """
        INSERT INTO archive (record_type, enrichment_state, conversation_id, status)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("tweet_object", "done", None, None),
            ("tweet_object", "done", None, None),
            ("tweet_object", "pending", None, None),
            ("tweet_object", "terminal_unavailable", None, None),
            ("tweet", None, "c1", None),
            ("tweet", None, "c1", None),
            ("tweet", None, "c2", None),
            ("article", None, None, "preview"),
            ("article", None, None, "body_present"),
        ],
    )
    client = make_web_client(stats_routes.router, store=store)

    response = client.get("/api/stats/health")

    assert response.status_code == 200
    assert response.json() == {
        "enrichment": {"done": 2, "pending": 1, "terminal": 1},
        "threads_expanded": 2,
        "preview_articles": 1,
    }


def test_tag_stats_reports_case_insensitive_usage_and_coverage(make_web_client) -> None:
    store = _stats_store()
    store.conn.executemany(
        "INSERT INTO archive (record_type, tweet_id, raw_json) VALUES (?, ?, ?)",
        [
            ("media", "t1", None),
            ("media", "t1", None),
            ("media", "t2", None),
            ("media", "t3", None),
            ("media_tag", "t1", json.dumps({"tags": ["Nature", "Bird"]})),
            ("media_tag", "t2", json.dumps({"tags": ["nature", "Sky"]})),
        ],
    )
    client = make_web_client(stats_routes.router, store=store)

    response = client.get("/api/stats/tags")

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "eligible_tweets": 3,
        "tagged_tweets": 2,
        "untagged_eligible": 1,
        "unique_tags": 3,
        "total_tag_instances": 4,
        "coverage_pct": 66.7,
        "avg_tags_per_tweet": 2.0,
        "top_tags": data["top_tags"],
    }
    assert {item["tag"]: item["count"] for item in data["top_tags"]} == {
        "nature": 2,
        "bird": 1,
        "sky": 1,
    }


def test_tag_stats_handles_empty_archive(make_web_client) -> None:
    store = _stats_store()
    client = make_web_client(stats_routes.router, store=store)

    data = client.get("/api/stats/tags").json()

    assert data["coverage_pct"] == 0.0
    assert data["avg_tags_per_tweet"] == 0.0
    assert data["untagged_eligible"] == 0
    assert data["top_tags"] == []


def test_tag_stats_ignore_malformed_tag_json(make_web_client) -> None:
    store = _stats_store()
    store.conn.executemany(
        "INSERT INTO archive (record_type, tweet_id, raw_json) VALUES (?, ?, ?)",
        [
            ("media", "t1", None),
            ("media", "t2", None),
            ("media_tag", "t1", "not-json"),
            ("media_tag", "t2", json.dumps({"tags": ["Bird"]})),
        ],
    )
    client = make_web_client(stats_routes.router, store=store)

    response = client.get("/api/stats/tags")

    assert response.status_code == 200
    assert response.json()["tagged_tweets"] == 1
    assert response.json()["unique_tags"] == 1
    assert response.json()["top_tags"] == [{"tag": "bird", "count": 1}]


def test_stats_routes_require_authentication(make_web_client) -> None:
    client = make_web_client(stats_routes.router, store=_stats_store(), password="secret")

    response = client.get("/api/stats/summary")

    assert response.status_code == 401
