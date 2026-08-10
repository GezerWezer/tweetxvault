"""Shared archive post search used by the CLI and Web API."""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from tweetxvault.export.common import normalize_collection_name

_UNAVAILABLE_TWEET_TEXTS = frozenset(
    {
        "This Post is from a suspended account. {learnmore}",
        "This Post is from a private account. {learnmore}",
        "This Post is from an account that no longer exists. {learnmore}",
    }
)
_FILTER_KEYS = frozenset(
    {
        "from",
        "to",
        "mentions",
        "since",
        "until",
        "since_time",
        "until_time",
        "since_id",
        "max_id",
        "has",
        "is",
        "filter",
        "min_retweets",
        "min_faves",
        "min_replies",
        "conversation_id",
        "quoted_tweet_id",
        "url",
        "source",
        "card_name",
        "tag",
        "hashtag",
    }
)
_FILTER_VALUES = {
    "has": frozenset({"media", "image", "video", "links"}),
    "is": frozenset({"reply"}),
    "filter": frozenset(
        {
            "articles",
            "images",
            "links",
            "media",
            "native_video",
            "nativeretweets",
            "quote",
            "replies",
            "self_threads",
            "threads",
            "verified",
            "videos",
        }
    ),
}
_NUMERIC_FILTERS = frozenset(
    {
        "since_time",
        "until_time",
        "since_id",
        "max_id",
        "min_retweets",
        "min_faves",
        "min_replies",
    }
)
_TOKEN_PATTERN = re.compile(r'-?[\w_]+:(?:"[^"]*"|[^\s]+)|-?"[^"]*"|[^\s]+')


class SearchQueryError(ValueError):
    """Raised when a shared search query is malformed."""


@dataclass(frozen=True, slots=True)
class SearchClause:
    kind: Literal["text", "filter"]
    value: str
    key: str | None = None
    negated: bool = False


@dataclass(frozen=True, slots=True)
class ParsedSearchQuery:
    groups: tuple[tuple[SearchClause, ...], ...]

    @property
    def has_or(self) -> bool:
        return any(len(group) > 1 for group in self.groups)

    @property
    def has_text(self) -> bool:
        return any(clause.kind == "text" for group in self.groups for clause in group)

    @property
    def has_positive_text(self) -> bool:
        return any(
            clause.kind == "text" and not clause.negated
            for group in self.groups
            for clause in group
        )

    @property
    def has_negative_text(self) -> bool:
        return any(
            clause.kind == "text" and clause.negated for group in self.groups for clause in group
        )

    def positive_text_terms(self) -> list[str]:
        terms: list[str] = []
        for group in self.groups:
            for clause in group:
                if clause.kind != "text" or clause.negated:
                    continue
                value = clause.value
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                terms.extend(part for part in value.split() if part)
        return list(dict.fromkeys(terms))

    def conjunctive_parts(self) -> tuple[dict[str, list[str]], str]:
        """Return the legacy filter/text split for a query without OR or negative text."""
        filters: dict[str, list[str]] = {}
        text: list[str] = []
        for group in self.groups:
            clause = group[0]
            if clause.kind == "text":
                text.append(clause.value)
                continue
            key = f"-{clause.key}" if clause.negated else str(clause.key)
            filters.setdefault(key, []).append(clause.value)
        return filters, " ".join(text)


@dataclass(slots=True)
class SearchPage:
    rows: list[dict[str, Any]]
    total: int
    page: int
    pages: int
    truncated: bool = False


def _strip_quotes(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _validate_filter(key: str, value: str) -> None:
    if key not in _FILTER_KEYS:
        raise SearchQueryError(f"Unsupported search filter: {key}:")
    if not value:
        raise SearchQueryError(f"Search filter {key}: requires a value.")
    allowed = _FILTER_VALUES.get(key)
    if allowed is not None and value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise SearchQueryError(f"Unsupported {key}: value {value!r}. Expected one of: {choices}.")
    if key in {"since", "until"} and _parse_twitter_date(value) is None:
        raise SearchQueryError(f"Search filter {key}: expects YYYY-MM-DD.")
    if key in _NUMERIC_FILTERS:
        try:
            float(value) if key.endswith("_time") else int(value)
        except ValueError as exc:
            raise SearchQueryError(f"Search filter {key}: expects a number.") from exc


def parse_search_query(query: str | None) -> ParsedSearchQuery:
    """Parse implicit-AND search text with adjacent-clause OR groups."""
    groups: list[list[SearchClause]] = []
    join_with_or = False
    negate_next = False
    expecting_clause = False

    for token in _TOKEN_PATTERN.findall(query or ""):
        if token == "OR":
            if not groups or join_with_or or expecting_clause:
                raise SearchQueryError("OR must appear between two search clauses.")
            join_with_or = True
            expecting_clause = True
            continue
        if token == "AND":
            if not groups or join_with_or or expecting_clause:
                raise SearchQueryError("AND must appear between two search clauses.")
            expecting_clause = True
            continue
        if token == "NOT":
            if negate_next:
                raise SearchQueryError("NOT must be followed by a search clause.")
            negate_next = True
            expecting_clause = True
            continue

        negated = token.startswith("-")
        if negated:
            token = token[1:]
        negated = negated ^ negate_next
        negate_next = False

        filter_match = re.fullmatch(r'([\w_]+):("[^"]*"|[^\s]+)', token)
        if filter_match:
            key, raw_value = filter_match.groups()
            key = key.lower()
            value = _strip_quotes(raw_value).lower()
            _validate_filter(key, value)
            clause = SearchClause("filter", value, key=key, negated=negated)
        elif token.startswith("#") and len(token) > 1:
            clause = SearchClause("filter", token[1:].lower(), key="hashtag", negated=negated)
        else:
            if not token:
                raise SearchQueryError("Search terms cannot be empty.")
            clause = SearchClause("text", token, negated=negated)

        if join_with_or:
            groups[-1].append(clause)
        else:
            groups.append([clause])
        join_with_or = False
        expecting_clause = False

    if join_with_or or negate_next or expecting_clause:
        raise SearchQueryError("Search query cannot end with an operator.")
    return ParsedSearchQuery(tuple(tuple(group) for group in groups))


def _extract_advanced_filters(q: str | None) -> tuple[dict[str, list[str]], str]:
    """Compatibility projection used by route-level tests and older callers."""
    parsed = parse_search_query(q)
    filters: dict[str, list[str]] = {}
    text: list[str] = []
    for group in parsed.groups:
        for clause in group:
            if clause.kind == "text":
                prefix = "-" if clause.negated else ""
                text.append(f"{prefix}{clause.value}")
            else:
                key = f"-{clause.key}" if clause.negated else str(clause.key)
                filters.setdefault(key, []).append(clause.value)
    return filters, " ".join(text)


def _parse_twitter_date(date_str: str) -> float | None:
    if not date_str:
        return None
    try:
        if "_" in date_str:
            parts = date_str.split("_")
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
            return dt.timestamp()
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    except (ValueError, IndexError):
        return None


def _row_created_at_timestamp(row: dict[str, Any]) -> float | None:
    raw_ts = row.get("created_at_ts")
    if isinstance(raw_ts, int | float):
        return float(raw_ts)
    raw = row.get("created_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").timestamp()
        except ValueError:
            return None


def _is_available_tweet(row: dict[str, Any]) -> bool:
    return row.get("text") not in _UNAVAILABLE_TWEET_TEXTS


def _filter_matches(row: dict[str, Any], key: str, value: str) -> bool:
    raw = row.get("raw_json") or {}
    if not isinstance(raw, dict):
        raw = {}
    legacy = raw.get("legacy") or {}
    author = row.get("author") or {}

    if key == "from":
        return author.get("username", "").lower() == value.replace("@", "")
    if key == "to":
        return (legacy.get("in_reply_to_screen_name") or "").lower() == value.replace("@", "")
    if key == "mentions":
        mentions = [
            mention.get("screen_name", "").lower()
            for mention in legacy.get("entities", {}).get("user_mentions", [])
        ]
        return value.replace("@", "") in mentions
    if key in {"since", "until"}:
        boundary = _parse_twitter_date(value)
        created = _row_created_at_timestamp(row)
        if boundary is None or created is None:
            return False
        return created >= boundary if key == "since" else created < boundary
    if key in {"since_time", "until_time"}:
        created = _row_created_at_timestamp(row)
        if created is None:
            return False
        boundary = float(value)
        return created >= boundary if key == "since_time" else created < boundary
    if key == "since_id":
        return int(row.get("tweet_id", 0)) > int(value)
    if key == "max_id":
        return int(row.get("tweet_id", 0)) <= int(value)
    if key == "has":
        if value == "media":
            return bool(row.get("media"))
        if value == "image":
            return any(media.get("type") == "photo" for media in row.get("media", []))
        if value == "video":
            return any(
                media.get("type") in {"video", "animated_gif"} for media in row.get("media", [])
            )
        if value == "links":
            return bool(row.get("urls"))
    if key == "is":
        return value == "reply" and bool(legacy.get("in_reply_to_status_id_str"))
    if key == "filter":
        if value == "articles":
            return bool(row.get("article"))
        if value == "replies":
            return bool(legacy.get("in_reply_to_status_id_str"))
        if value == "quote":
            return bool(legacy.get("is_quote_status"))
        if value == "nativeretweets":
            return bool(legacy.get("retweeted_status_id_str"))
        if value in {"self_threads", "threads"}:
            return (legacy.get("in_reply_to_screen_name") or "").lower() == author.get(
                "username", ""
            ).lower()
        if value == "media":
            return bool(row.get("media"))
        if value == "images":
            return any(media.get("type") == "photo" for media in row.get("media", []))
        if value in {"videos", "native_video"}:
            return any(
                media.get("type") in {"video", "animated_gif"} for media in row.get("media", [])
            )
        if value == "links":
            return bool(row.get("urls"))
        if value == "verified":
            user_result = raw.get("core", {}).get("user_results", {}).get("result", {})
            return bool(
                user_result.get("is_blue_verified") or user_result.get("legacy", {}).get("verified")
            )
    if key == "min_retweets":
        return int(legacy.get("retweet_count", 0)) >= int(value)
    if key == "min_faves":
        return int(legacy.get("favorite_count", 0)) >= int(value)
    if key == "min_replies":
        return int(legacy.get("reply_count", 0)) >= int(value)
    if key == "conversation_id":
        return legacy.get("conversation_id_str") == value
    if key == "quoted_tweet_id":
        return legacy.get("quoted_status_id_str") == value
    if key == "url":
        return any(
            value in (url.get("expanded_url") or "").lower()
            or value in (url.get("display_url") or "").lower()
            for url in row.get("urls", [])
        )
    if key == "source":
        return value.replace("_", " ") in raw.get("source", "").lower()
    if key == "card_name":
        return raw.get("card", {}).get("name") == value
    if key == "tag":
        tags = [
            *(row.get("media_tags") or {}).get("tags", []),
            *(row.get("qt_media_tags") or {}).get("tags", []),
        ]
        return any(value == tag.lower() for tag in tags)
    if key == "hashtag":
        hashtags = [
            hashtag.get("text", "").lower()
            for hashtag in legacy.get("entities", {}).get("hashtags", [])
        ]
        return value.replace("#", "").lower() in hashtags
    return False


def _apply_advanced_filters(
    rows: list[dict[str, Any]], filters: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """Apply AND-by-default structured filters to hydrated post rows."""
    if not filters:
        return rows
    filtered: list[dict[str, Any]] = []
    for row in rows:
        keep = True
        for raw_key, values in filters.items():
            negated = raw_key.startswith("-")
            key = raw_key[1:] if negated else raw_key
            for value in values:
                try:
                    matched = _filter_matches(row, key, value)
                except (TypeError, ValueError):
                    matched = False
                if matched == negated:
                    keep = False
                    break
            if not keep:
                break
        if keep:
            filtered.append(row)
    return filtered


def _sql_quote(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _collection_expr(collections: set[str] | None) -> str:
    if not collections:
        return ""
    if len(collections) == 1:
        return f"collection_type = {_sql_quote(next(iter(collections)))}"
    values = ", ".join(_sql_quote(value) for value in sorted(collections))
    return f"collection_type IN ({values})"


def _normalized_filter_expr(key: str, value: str, *, negated: bool = False) -> str | None:
    """Translate filters backed by normalized archive rows into correlated SQL."""
    condition = None
    if (key, value) in {("has", "media"), ("filter", "media")}:
        condition = "related.record_type = 'media'"
    elif (key, value) in {("has", "image"), ("filter", "images")}:
        condition = "related.record_type = 'media' AND related.media_type = 'photo'"
    elif (key, value) in {
        ("has", "video"),
        ("filter", "videos"),
        ("filter", "native_video"),
    }:
        condition = (
            "related.record_type = 'media' AND related.media_type IN ('video', 'animated_gif')"
        )
    elif (key, value) in {("has", "links"), ("filter", "links")}:
        condition = "related.record_type = 'url_ref'"
    elif (key, value) == ("filter", "articles"):
        condition = "related.record_type = 'article'"
    if condition is None:
        return None

    expression = (
        "EXISTS (SELECT 1 FROM archive AS related "
        "WHERE related.tweet_id = archive.tweet_id "
        f"AND {condition})"
    )
    return f"NOT ({expression})" if negated else expression


def _filter_candidate_ids(store: Any, tweet_ids: list[str], expressions: list[str]) -> set[str]:
    """Apply SQL predicates to a bounded list of post candidate IDs."""
    if not expressions:
        return set(tweet_ids)
    matched: set[str] = set()
    for chunk_start in range(0, len(tweet_ids), 100):
        chunk = tweet_ids[chunk_start : chunk_start + 100]
        ids = ", ".join(_sql_quote(tweet_id) for tweet_id in chunk)
        expr = f"record_type = 'tweet' AND tweet_id IN ({ids})"
        for expression in expressions:
            expr += f" AND {expression}"
        rows = store._query(expr=expr, cols=["DISTINCT tweet_id"])
        matched.update(row["tweet_id"] for row in rows if row.get("tweet_id"))
    return matched


def _effective_sort(sort: str, *, has_positive_text: bool) -> str:
    if sort == "oldest":
        return "oldest"
    if sort == "random":
        return "random"
    if sort == "relevance" and has_positive_text:
        return "relevance"
    if sort == "default":
        return "relevance" if has_positive_text else "newest"
    return "newest"


def _sort_rows(rows: list[dict[str, Any]], sort: str) -> None:
    if sort == "random":
        random.shuffle(rows)
        return

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        timestamp = _row_created_at_timestamp(row)
        missing = timestamp is None
        timestamp = timestamp or 0.0
        tweet_id = row.get("tweet_id") or ""
        return (missing, timestamp if sort == "oldest" else -timestamp, tweet_id)

    rows.sort(key=sort_key)


def _hydrate_metadata(
    rows: list[dict[str, Any]], hits: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    hit_by_id = {hit.get("tweet_id"): hit for hit in hits}
    for row in rows:
        hit = hit_by_id.get(row.get("tweet_id"), {})
        row["type"] = "post"
        if hit.get("collections") is not None:
            row["collections"] = hit["collections"]
        if hit.get("match_score") is not None:
            row["match_score"] = hit["match_score"]
    return rows


def _page(rows: list[dict[str, Any]], *, page: int, limit: int, truncated: bool) -> SearchPage:
    total = len(rows)
    start = (page - 1) * limit
    return SearchPage(
        rows=rows[start : start + limit],
        total=total,
        page=page,
        pages=math.ceil(total / limit) if total else 1,
        truncated=truncated,
    )


def _collection_ids(store: Any, collections: set[str] | None) -> set[str] | None:
    expr = _collection_expr(collections)
    if not expr:
        return None
    rows = store._query(
        expr=f"record_type = 'tweet' AND {expr}",
        cols=["DISTINCT tweet_id"],
    )
    return {row["tweet_id"] for row in rows if row.get("tweet_id")}


def _export_candidates(store: Any, collections: set[str] | None) -> list[dict[str, Any]]:
    rows = store.export_rows("all", sort="newest", include_raw_json=True)
    allowed_ids = _collection_ids(store, collections)
    if allowed_ids is not None:
        rows = [row for row in rows if row.get("tweet_id") in allowed_ids]
    return [row for row in rows if _is_available_tweet(row)]


def _search_grouped(
    store: Any,
    parsed: ParsedSearchQuery,
    *,
    collections: set[str] | None,
    sort: str,
    page: int,
    limit: int,
    candidate_limit: int,
) -> SearchPage:
    rows = _export_candidates(store, collections)
    text_matches: dict[SearchClause, dict[str, float]] = {}
    truncated = False
    for group in parsed.groups:
        for clause in group:
            if clause.kind != "text" or clause in text_matches:
                continue
            hits = store.search_fts(
                clause.value,
                limit=candidate_limit,
                types={"post"},
                collections=collections,
            )
            truncated = truncated or len(hits) >= candidate_limit
            text_matches[clause] = {
                hit["tweet_id"]: float(hit.get("match_score") or 0.0)
                for hit in hits
                if hit.get("tweet_id")
            }

    scores: dict[str, float] = {}
    matched_rows: list[dict[str, Any]] = []
    for row in rows:
        tweet_id = row.get("tweet_id")
        row_score = 0.0
        keep = True
        for group in parsed.groups:
            group_match = False
            group_score = 0.0
            for clause in group:
                if clause.kind == "text":
                    matched = tweet_id in text_matches[clause]
                    if clause.negated:
                        matched = not matched
                    elif matched:
                        group_score = max(group_score, text_matches[clause][str(tweet_id)])
                else:
                    matched = _filter_matches(row, str(clause.key), clause.value)
                    if clause.negated:
                        matched = not matched
                group_match = group_match or matched
            if not group_match:
                keep = False
                break
            row_score += group_score
        if keep:
            matched_rows.append(row)
            if isinstance(tweet_id, str):
                scores[tweet_id] = row_score

    effective_sort = _effective_sort(sort, has_positive_text=parsed.has_positive_text)
    if effective_sort == "relevance":
        matched_rows.sort(
            key=lambda row: (scores.get(str(row.get("tweet_id")), 0.0), row.get("tweet_id") or ""),
            reverse=True,
        )
    else:
        _sort_rows(matched_rows, effective_sort)
    for row in matched_rows:
        row["type"] = "post"
        if row.get("tweet_id") in scores:
            row["match_score"] = scores[str(row["tweet_id"])]
    return _page(matched_rows, page=page, limit=limit, truncated=truncated)


def search_posts(
    store: Any,
    query: str | None,
    *,
    collections: set[str] | None = None,
    sort: str = "default",
    page: int = 1,
    limit: int = 20,
    candidate_limit: int = 1000,
) -> SearchPage:
    """Search hydrated archive posts using the canonical Web query language."""
    if page < 1:
        raise ValueError("page must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if collections:
        collections = {normalize_collection_name(value) for value in collections}
        collections.discard("all")
        collections = collections or None

    parsed = parse_search_query(query)
    if parsed.has_or or parsed.has_negative_text:
        return _search_grouped(
            store,
            parsed,
            collections=collections,
            sort=sort,
            page=page,
            limit=limit,
            candidate_limit=candidate_limit,
        )

    filters, text_query = parsed.conjunctive_parts()
    pushable_exprs: list[str] = []
    post_filters: dict[str, list[str]] = {}
    for raw_key, values in filters.items():
        negated = raw_key.startswith("-")
        key = raw_key[1:] if negated else raw_key
        normalized_exprs = [
            _normalized_filter_expr(key, value, negated=negated) for value in values
        ]
        if all(expression is not None for expression in normalized_exprs):
            pushable_exprs.extend(str(expression) for expression in normalized_exprs)
            continue
        if negated or key not in {
            "from",
            "conversation_id",
            "since",
            "until",
            "since_time",
            "until_time",
            "tag",
        }:
            post_filters[raw_key] = values
            continue
        if key == "from":
            pushable_exprs.extend(
                f"LOWER(author_username) = {_sql_quote(value.replace('@', ''))}" for value in values
            )
        elif key == "conversation_id":
            pushable_exprs.extend(f"conversation_id = {_sql_quote(value)}" for value in values)
        elif key == "tag":
            for value in values:
                escaped_value = value.replace("'", "''")
                pushable_exprs.append(
                    "tweet_id IN ("
                    "SELECT direct_tag.tweet_id FROM archive direct_tag "
                    "WHERE direct_tag.record_type = 'media_tag' "
                    f"AND LOWER(direct_tag.raw_json) LIKE LOWER('%\"{escaped_value}\"%') "
                    "UNION SELECT relation.tweet_id FROM archive relation "
                    "JOIN archive quoted_tag ON quoted_tag.tweet_id = relation.target_tweet_id "
                    "AND quoted_tag.record_type = 'media_tag' "
                    "WHERE relation.record_type = 'tweet_relation' "
                    "AND relation.relation_type = 'quote_of' "
                    f"AND LOWER(quoted_tag.raw_json) LIKE LOWER('%\"{escaped_value}\"%'))"
                )
        elif key in {"since", "since_time", "until", "until_time"}:
            for value in values:
                boundary = _parse_twitter_date(value) if key in {"since", "until"} else float(value)
                comparison = ">=" if key in {"since", "since_time"} else "<"
                pushable_exprs.append(f"created_at_ts {comparison} {int(boundary)}")

    effective_sort = _effective_sort(sort, has_positive_text=bool(text_query))
    start = (page - 1) * limit
    collection_expr = _collection_expr(collections)

    if not post_filters and not text_query:
        filter_expr = "record_type = 'tweet'"
        if collection_expr:
            filter_expr += f" AND {collection_expr}"
        for expression in pushable_exprs:
            filter_expr += f" AND {expression}"
        unavailable = ", ".join(_sql_quote(text) for text in _UNAVAILABLE_TWEET_TEXTS)
        filter_expr += f" AND (text IS NULL OR text NOT IN ({unavailable}))"
        total = store._count_distinct("tweet_id", filter_expr)
        order_by = "created_at_ts DESC, CAST(sort_index AS INTEGER) DESC, tweet_id DESC"
        if effective_sort == "oldest":
            order_by = "created_at_ts ASC, CAST(sort_index AS INTEGER) ASC, tweet_id ASC"
        elif effective_sort == "random":
            order_by = "RANDOM()"
        id_rows = store._query(
            expr=filter_expr,
            cols=["DISTINCT tweet_id"],
            limit=limit,
            offset=start,
            order_by=order_by,
        )
        ids = [row["tweet_id"] for row in id_rows if row.get("tweet_id")]
        hydrated = _hydrate_metadata(store.fetch_tweets_by_ids(ids), [])
        return SearchPage(
            rows=hydrated,
            total=total,
            page=page,
            pages=math.ceil(total / limit) if total else 1,
        )

    if not post_filters and text_query and not pushable_exprs:
        hits = store.search_fts(
            text_query,
            limit=candidate_limit,
            types={"post"},
            collections=collections,
        )
        hits = [hit for hit in hits if _is_available_tweet(hit)]
        truncated = len(hits) >= candidate_limit
        if effective_sort != "relevance":
            _sort_rows(hits, effective_sort)
        total = len(hits)
        page_hits = hits[start : start + limit]
        ids = [hit["tweet_id"] for hit in page_hits if hit.get("tweet_id")]
        hydrated = _hydrate_metadata(store.fetch_tweets_by_ids(ids), page_hits)
        return SearchPage(
            rows=hydrated,
            total=total,
            page=page,
            pages=math.ceil(total / limit) if total else 1,
            truncated=truncated,
        )

    hits: list[dict[str, Any]] = []
    truncated = False
    if text_query:
        hits = store.search_fts(
            text_query,
            limit=candidate_limit,
            types={"post"},
            collections=collections,
        )
        hits = [hit for hit in hits if _is_available_tweet(hit)]
        truncated = len(hits) >= candidate_limit
        candidate_ids = [hit["tweet_id"] for hit in hits if hit.get("tweet_id")]
        matched_ids = _filter_candidate_ids(store, candidate_ids, pushable_exprs)
        hits = [hit for hit in hits if hit.get("tweet_id") in matched_ids]
        if not post_filters:
            if effective_sort != "relevance":
                _sort_rows(hits, effective_sort)
            total = len(hits)
            page_hits = hits[start : start + limit]
            ids = [hit["tweet_id"] for hit in page_hits if hit.get("tweet_id")]
            hydrated = _hydrate_metadata(store.fetch_tweets_by_ids(ids), page_hits)
            return SearchPage(
                rows=hydrated,
                total=total,
                page=page,
                pages=math.ceil(total / limit) if total else 1,
                truncated=truncated,
            )
        rows = store.fetch_tweets_by_ids([hit["tweet_id"] for hit in hits])
        rows = [row for row in rows if _is_available_tweet(row)]
    else:
        rows = _export_candidates(store, collections)
    rows = _apply_advanced_filters(rows, post_filters if text_query else filters)
    if effective_sort == "relevance" and hits:
        order = {hit["tweet_id"]: index for index, hit in enumerate(hits)}
        rows.sort(key=lambda row: order.get(row.get("tweet_id"), candidate_limit + 1))
    else:
        _sort_rows(rows, effective_sort)
    hit_by_id = {hit.get("tweet_id"): hit for hit in hits}
    for row in rows:
        row["type"] = "post"
        hit = hit_by_id.get(row.get("tweet_id"), {})
        if hit.get("match_score") is not None:
            row["match_score"] = hit["match_score"]
        if hit.get("collections") is not None:
            row["collections"] = hit["collections"]
    return _page(rows, page=page, limit=limit, truncated=truncated)
