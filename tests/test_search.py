from __future__ import annotations

import json
from typing import Any

import pytest

from tweetxvault.search import SearchQueryError, parse_search_query, search_posts
from tweetxvault.storage.backend import ArchiveStore


def _row(
    tweet_id: str,
    text: str,
    *,
    username: str = "alice",
    media: list[dict[str, Any]] | None = None,
    article: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "tweet_id": tweet_id,
        "text": text,
        "author": {"id": username, "username": username, "display_name": username.title()},
        "created_at": f"2026-01-0{tweet_id}T00:00:00Z",
        "media": media or [],
        "urls": [],
        "article": article,
        "raw_json": {"legacy": {}},
    }


class SearchStore:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def export_rows(self, collection: str, *, sort: str, include_raw_json: bool):
        assert (collection, sort, include_raw_json) == ("all", "newest", True)
        return [dict(row) for row in reversed(self.rows)]

    def search_fts(
        self,
        query: str,
        *,
        limit: int,
        types: set[str] | None,
        collections: set[str] | None,
    ) -> list[dict[str, Any]]:
        assert types == {"post"}
        assert collections is None
        term = query.strip('"').casefold()
        matches = []
        for score, row in enumerate(reversed(self.rows), start=1):
            if term in row["text"].casefold():
                matches.append(
                    {
                        "tweet_id": row["tweet_id"],
                        "text": row["text"],
                        "created_at": row["created_at"],
                        "collections": ["bookmark"],
                        "match_score": float(score),
                    }
                )
        return matches[:limit]

    def fetch_tweets_by_ids(self, tweet_ids: list[str]) -> list[dict[str, Any]]:
        by_id = {row["tweet_id"]: row for row in self.rows}
        return [dict(by_id[tweet_id]) for tweet_id in tweet_ids]

    def _query(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"unexpected collection query: {kwargs}")


def test_parser_uses_implicit_and_and_adjacent_or_groups() -> None:
    parsed = parse_search_query("from:alice cats OR dogs has:image")

    assert [[clause.value for clause in group] for group in parsed.groups] == [
        ["alice"],
        ["cats", "dogs"],
        ["image"],
    ]
    assert parsed.has_or is True


def test_parser_accepts_explicit_and_but_keeps_lowercase_words_searchable() -> None:
    explicit = parse_search_query("cats AND dogs")
    lowercase = parse_search_query("cats and dogs or birds")

    assert [[clause.value for clause in group] for group in explicit.groups] == [
        ["cats"],
        ["dogs"],
    ]
    assert [group[0].value for group in lowercase.groups] == ["cats", "and", "dogs", "or", "birds"]


def test_parser_preserves_text_and_filter_negation() -> None:
    parsed = parse_search_query('cats -dogs -filter:videos "night sky"')
    clauses = [clause for group in parsed.groups for clause in group]

    assert [(clause.kind, clause.value, clause.negated) for clause in clauses] == [
        ("text", "cats", False),
        ("text", "dogs", True),
        ("filter", "videos", True),
        ("text", '"night sky"', False),
    ]


@pytest.mark.parametrize("query", ["OR cats", "cats OR", "cats AND", "unknown:value"])
def test_parser_rejects_malformed_or_unknown_operators(query: str) -> None:
    with pytest.raises(SearchQueryError):
        parse_search_query(query)


def test_grouped_search_requires_all_groups_and_any_or_alternative() -> None:
    photo = [{"type": "photo"}]
    store = SearchStore(
        [
            _row("1", "cats", media=photo),
            _row("2", "dogs", media=photo),
            _row("3", "dogs", username="bob", media=photo),
            _row("4", "cats"),
        ]
    )

    result = search_posts(store, "from:alice cats OR dogs has:image", limit=20)

    assert {row["tweet_id"] for row in result.rows} == {"1", "2"}
    assert result.total == 2


def test_explicit_or_makes_repeated_filters_alternatives() -> None:
    store = SearchStore(
        [
            _row("1", "one"),
            _row("2", "two", username="bob"),
            _row("3", "three", username="carol"),
        ]
    )

    result = search_posts(store, "from:alice OR from:bob", limit=20)

    assert {row["tweet_id"] for row in result.rows} == {"1", "2"}


def test_filter_articles_returns_posts_with_attached_articles() -> None:
    store = SearchStore(
        [
            _row("1", "article post", article={"title": "Long read"}),
            _row("2", "ordinary post"),
        ]
    )

    result = search_posts(store, "filter:articles", limit=20)

    assert [row["tweet_id"] for row in result.rows] == ["1"]


def test_real_store_shared_search_handles_or_and_article_filter(tmp_path) -> None:
    store = ArchiveStore(tmp_path / "archive.db", create=True)
    store._merge_records(
        [
            store._record(
                row_key="tweet:bookmark::1",
                record_type="tweet",
                tweet_id="1",
                collection_type="bookmark",
                text="cats in sqlite",
                author_id="alice",
                author_username="alice",
                author_display_name="Alice",
                created_at="Thu Jan 01 00:00:00 +0000 2026",
                created_at_ts=1,
                sort_index="1",
                raw_json=json.dumps({"legacy": {}}),
            ),
            store._record(
                row_key="tweet:bookmark::2",
                record_type="tweet",
                tweet_id="2",
                collection_type="bookmark",
                text="dogs in sqlite",
                author_id="bob",
                author_username="bob",
                author_display_name="Bob",
                created_at="Fri Jan 02 00:00:00 +0000 2026",
                created_at_ts=2,
                sort_index="2",
                raw_json=json.dumps({"legacy": {}}),
            ),
            store._record(
                row_key="article:1",
                record_type="article",
                tweet_id="1",
                title="Attached article",
                content_text="Long form text",
            ),
        ]
    )

    alternatives = search_posts(store, "cats OR dogs", limit=20)
    required_authors = search_posts(store, "from:alice from:bob", limit=20)
    alternative_authors = search_posts(store, "from:alice OR from:bob", limit=20)
    articles = search_posts(store, "filter:articles", limit=20)

    assert {row["tweet_id"] for row in alternatives.rows} == {"1", "2"}
    assert required_authors.rows == []
    assert {row["tweet_id"] for row in alternative_authors.rows} == {"1", "2"}
    assert [row["tweet_id"] for row in articles.rows] == ["1"]
    store.close()
