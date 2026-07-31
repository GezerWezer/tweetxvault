from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tweetxvault.config import AppConfig, DatabaseConfig
from tweetxvault.storage import open_archive_store
from tweetxvault.storage.backend import ARCHIVE_COLUMNS, ArchiveStore

CREATED_2012 = "Tue Oct 09 21:39:26 +0000 2012"
CREATED_2024 = "Thu Apr 11 03:55:13 +0000 2024"
CREATED_2026 = "Sat Mar 14 00:00:00 +0000 2026"


def _membership(
    store: ArchiveStore,
    tweet_id: str,
    *,
    collection: str,
    text: str,
    created_at: str,
    created_at_ts: int,
    sort_index: str,
    folder_id: str | None = None,
    author_username: str = "alice",
    author_display_name: str = "Alice Example",
) -> dict[str, object]:
    return store._record(
        row_key=f"tweet:{collection}:{folder_id or ''}:{tweet_id}",
        record_type="tweet",
        tweet_id=tweet_id,
        collection_type=collection,
        folder_id=folder_id or "",
        text=text,
        author_id=f"author-{tweet_id}",
        author_username=author_username,
        author_display_name=author_display_name,
        created_at=created_at,
        created_at_ts=created_at_ts,
        sort_index=sort_index,
        raw_json=f'{{"id":"{tweet_id}"}}',
    )


def _seed_search_rows(store: ArchiveStore) -> None:
    rows = [
        _membership(
            store,
            "1",
            collection="bookmark",
            text="alpha beta hello-world",
            created_at=CREATED_2012,
            created_at_ts=1,
            sort_index="10",
        ),
        _membership(
            store,
            "1",
            collection="like",
            text="alpha beta hello-world",
            created_at=CREATED_2012,
            created_at_ts=1,
            sort_index="9",
        ),
        _membership(
            store,
            "2",
            collection="like",
            text="alpha gamma",
            created_at=CREATED_2024,
            created_at_ts=2,
            sort_index="20",
            author_username="bob",
            author_display_name="Bob Builder",
        ),
        _membership(
            store,
            "3",
            collection="bookmark",
            text="article membership",
            created_at=CREATED_2026,
            created_at_ts=3,
            sort_index="30",
        ),
        store._record(
            row_key="article:3",
            record_type="article",
            tweet_id="3",
            title="Alpha Analysis",
            summary_text="A focused beta report",
            content_text="Long form article content",
        ),
    ]
    store._merge_records(rows)


def test_schema_creates_fts_triggers_and_page_indexes(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None

    objects = {
        (row["type"], row["name"])
        for row in store.conn.execute(
            "SELECT type, name FROM sqlite_master WHERE name LIKE 'archive%' "
            "OR name LIKE 'idx_archive%'"
        )
    }

    assert ("table", "archive") in objects
    assert ("table", "archive_fts") in objects
    assert {
        ("trigger", "archive_ai"),
        ("trigger", "archive_au"),
        ("trigger", "archive_ad"),
    } <= objects
    assert {
        ("index", "idx_archive_record_page"),
        ("index", "idx_archive_record_collection_page"),
        ("index", "idx_archive_tweet_id"),
    } <= objects
    store.close()


def test_fts_triggers_follow_insert_update_and_delete(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    row = _membership(
        store,
        "1",
        collection="bookmark",
        text="initial searchable phrase",
        created_at=CREATED_2012,
        created_at_ts=1,
        sort_index="1",
    )

    store._merge_records([row])
    assert [result["tweet_id"] for result in store.search_fts("initial")] == ["1"]

    row["text"] = "replacement searchable phrase"
    store._merge_records([row])
    assert store.search_fts("initial") == []
    assert [result["tweet_id"] for result in store.search_fts("replacement")] == ["1"]

    store._delete("row_key = 'tweet:bookmark::1'")
    assert store.search_fts("replacement") == []
    store.close()


def test_created_at_ts_migration_backfills_and_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "archive.db"
    columns = []
    for name in ARCHIVE_COLUMNS:
        if name == "created_at_ts":
            continue
        columns.append(f"{name} {'TEXT PRIMARY KEY' if name == 'row_key' else 'TEXT'}")
    connection = sqlite3.connect(db_path)
    connection.execute(f"CREATE TABLE archive ({', '.join(columns)})")
    connection.execute(
        "INSERT INTO archive(row_key, record_type, tweet_id, created_at) VALUES (?, ?, ?, ?)",
        ("tweet:bookmark::1", "tweet", "1", CREATED_2012),
    )
    connection.commit()
    connection.close()

    first = ArchiveStore(db_path, create=True)
    first_value = first.conn.execute(
        "SELECT created_at_ts FROM archive WHERE tweet_id = '1'"
    ).fetchone()[0]
    first.close()
    second = ArchiveStore(db_path, create=True)
    second_value = second.conn.execute(
        "SELECT created_at_ts FROM archive WHERE tweet_id = '1'"
    ).fetchone()[0]
    created_at_columns = [
        row["name"] for row in second.conn.execute("PRAGMA table_info(archive)")
    ].count("created_at_ts")

    assert first_value == second_value == 1_349_818_766
    assert created_at_columns == 1
    second.close()


def test_database_pragmas_use_configured_values(paths) -> None:
    config = AppConfig(database=DatabaseConfig(cache_size_kb=321, mmap_size_bytes=4096))
    store = open_archive_store(paths, create=True, config=config)
    assert store is not None

    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("PRAGMA cache_size").fetchone()[0] == -321
    assert store.conn.execute("PRAGMA mmap_size").fetchone()[0] == 4096
    store.close()


def test_query_supports_projection_order_limit_and_offset(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_search_rows(store)

    rows = store._query(
        expr="record_type = 'tweet'",
        cols=["tweet_id", "created_at_ts"],
        order_by="created_at_ts DESC, tweet_id DESC",
        limit=2,
        offset=1,
    )

    assert rows == [
        {"tweet_id": "2", "created_at_ts": 2},
        {"tweet_id": "1", "created_at_ts": 1},
    ]
    store.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        (False, False),
        (True, True),
        (0, False),
        (2, True),
        ("0", False),
        ("false", False),
        ("YES", True),
        ("unexpected", False),
    ],
)
def test_parse_bool_handles_sqlite_text_values(paths, raw: object, expected: bool) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    assert store._parse_bool(raw) is expected
    store.close()


def test_paginated_ids_support_collection_order_and_random(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_search_rows(store)

    assert store.get_paginated_tweet_ids("bookmark", 10, 0, "newest") == ["3", "1"]
    assert store.get_paginated_tweet_ids("bookmark", 10, 0, "oldest") == ["1", "3"]
    assert store.get_paginated_tweet_ids("bookmark", 1, 1, "newest") == ["1"]
    assert set(store.get_paginated_tweet_ids("bookmark", 10, 0, "random")) == {"1", "3"}
    assert store.get_paginated_tweet_ids("all", 10, 0, "newest") == ["3", "2", "1"]
    assert store.count_export_rows("all") == 3
    assert store.count_export_rows("like") == 2
    assert store.get_paginated_tweet_ids("bookmark", 0, 0) == []
    store.close()


@pytest.mark.parametrize(
    ("limit", "offset", "sort"),
    [(-1, 0, "newest"), (1, -1, "newest"), (1, 0, "sideways")],
)
def test_paginated_ids_reject_invalid_controls(
    paths,
    limit: int,
    offset: int,
    sort: str,
) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    with pytest.raises(ValueError):
        store.get_paginated_tweet_ids("all", limit, offset, sort)
    store.close()


def test_fetch_tweets_preserves_requested_order_and_hydrates_tags(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_search_rows(store)
    store.update_media_tags("1", ["Nature", "Sky"])
    store.update_media_tags("2", ["City"])

    rows = store.fetch_tweets_by_ids(["2", "missing", "1"])

    assert [row["tweet_id"] for row in rows] == ["2", "1"]
    assert rows[0]["media_tags"]["tags"] == ["City"]
    assert rows[1]["media_tags"]["tags"] == ["Nature", "Sky"]
    store.close()


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        ('"alpha beta"', ["1"]),
        ("hello-world", ["1"]),
        ("alpha AND gamma", ["2"]),
        ("alpha OR gamma", ["1", "2"]),
        ("", []),
        ("   ", []),
    ],
)
def test_fts_handles_phrases_punctuation_booleans_and_blank_queries(
    paths,
    query: str,
    expected_ids: list[str],
) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_search_rows(store)

    results = store.search_fts(query, limit=20, types={"post"})

    assert sorted({row["tweet_id"] for row in results}) == sorted(expected_ids)
    store.close()


def test_fts_filters_types_and_collections_and_hydrates_memberships(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_search_rows(store)

    posts = store.search_fts("alpha", types={"post"}, collections={"bookmark"})
    articles = store.search_fts("alpha", types={"article"}, collections={"bookmark"})
    excluded = store.search_fts("alpha", types={"article"}, collections={"like"})

    assert [row["tweet_id"] for row in posts] == ["1"]
    assert posts[0]["collections"] == ["bookmark", "like"]
    assert [row["tweet_id"] for row in articles] == ["3"]
    assert articles[0]["type"] == "article"
    assert excluded == []
    store.close()


def test_fts_overfetches_past_duplicate_memberships(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    duplicate_rows = [
        _membership(
            store,
            "1",
            collection="bookmark",
            folder_id=f"folder-{index}",
            text="shared overfetch token",
            created_at=CREATED_2012,
            created_at_ts=1,
            sort_index=str(100 - index),
        )
        for index in range(12)
    ]
    duplicate_rows.append(
        _membership(
            store,
            "2",
            collection="like",
            text="shared overfetch token",
            created_at=CREATED_2024,
            created_at_ts=2,
            sort_index="1",
        )
    )
    store._merge_records(duplicate_rows)

    results = store.search_fts("overfetch", limit=2, types={"post"})

    assert {row["tweet_id"] for row in results} == {"1", "2"}
    store.close()


def test_search_authors_matches_username_display_name_and_at_prefix(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None
    _seed_search_rows(store)

    assert [row["username"] for row in store.search_authors("@ali")] == ["alice"]
    assert [row["username"] for row in store.search_authors("Builder")] == ["bob"]
    assert store.search_authors("@") == []
    assert store.search_authors("") == []
    store.close()


def test_page_queries_use_covering_indexes_without_temporary_sort(paths) -> None:
    store = open_archive_store(paths, create=True)
    assert store is not None

    all_plan = store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT tweet_id FROM archive "
        "WHERE record_type = 'tweet' "
        "ORDER BY created_at_ts DESC, CAST(sort_index AS INTEGER) DESC, tweet_id DESC "
        "LIMIT 20"
    ).fetchall()
    collection_plan = store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT tweet_id FROM archive "
        "WHERE record_type = 'tweet' AND collection_type = 'bookmark' "
        "ORDER BY created_at_ts DESC, CAST(sort_index AS INTEGER) DESC, tweet_id DESC "
        "LIMIT 20"
    ).fetchall()
    details = " ".join(row["detail"] for row in all_plan + collection_plan)

    assert "idx_archive_record_page" in details
    assert "idx_archive_record_collection_page" in details
    assert "USE TEMP B-TREE" not in details
    store.close()
