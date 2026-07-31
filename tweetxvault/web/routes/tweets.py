"""Tweet query, thread, and quote endpoints."""

import json
import math
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from tweetxvault.export.common import normalize_collection_name
from tweetxvault.web.deps import get_store, verify_credentials

router = APIRouter()

_UNAVAILABLE_TWEET_TEXTS = frozenset(
    {
        "This Post is from a suspended account. {learnmore}",
        "This Post is from a private account. {learnmore}",
        "This Post is from an account that no longer exists. {learnmore}",
    }
)


def _sql_quote(value: object) -> str:
    """Quote a scalar for the store's expression-only query interface."""
    return "'" + str(value).replace("'", "''") + "'"


def _strip_quotes(s: str) -> str:
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _extract_advanced_filters(q: str | None) -> tuple[dict[str, list[str]], str]:
    token_pattern = re.compile(r"(-?[\w_]+):(\"[^\"]+\"|[^\s]+)|(-?\"[^\"]+\")|([^\s]+)")
    filters = {}
    text_query = []

    for match in token_pattern.finditer(q or ""):
        key, val, phrase, word = match.groups()
        if key and val:
            k = key.lower()
            v = _strip_quotes(val).lower()
            if k in filters:
                filters[k].append(v)
            else:
                filters[k] = [v]
        elif phrase:
            text_query.append(phrase)
        elif word:
            if word.startswith("#"):
                k = "hashtag"
                v = word[1:].lower()
                if k in filters:
                    filters[k].append(v)
                else:
                    filters[k] = [v]
            else:
                text_query.append(word)

    return filters, " ".join(text_query)


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
        else:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    except Exception:
        return None


def _row_created_at_timestamp(row: dict) -> float | None:
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


def _is_available_tweet(row: dict) -> bool:
    return row.get("text") not in _UNAVAILABLE_TWEET_TEXTS


def _apply_advanced_filters(rows: list[dict], filters: dict[str, list[str]]) -> list[dict]:
    if not filters:
        return rows
    filtered = []
    for r in rows:
        keep = True
        raw = r.get("raw_json") or {}
        legacy = raw.get("legacy") or {}

        for k, values in filters.items():
            is_negated = k.startswith("-")
            base_k = k[1:] if is_negated else k

            for val in values:
                match = False
                if base_k == "from":
                    match = r.get("author", {}).get("username", "").lower() == val.replace("@", "")
                elif base_k == "to":
                    match = (legacy.get("in_reply_to_screen_name") or "").lower() == val.replace(
                        "@", ""
                    )
                elif base_k in ("mentions", "@"):
                    mentions = [
                        m.get("screen_name", "").lower()
                        for m in legacy.get("entities", {}).get("user_mentions", [])
                    ]
                    match = val.replace("@", "") in mentions
                elif base_k == "since":
                    ts = _parse_twitter_date(val)
                    created_at_ts = _row_created_at_timestamp(r)
                    if ts is not None and created_at_ts is not None:
                        match = created_at_ts >= ts
                elif base_k == "until":
                    ts = _parse_twitter_date(val)
                    created_at_ts = _row_created_at_timestamp(r)
                    if ts is not None and created_at_ts is not None:
                        match = created_at_ts < ts
                elif base_k == "since_time":
                    try:
                        created_at_ts = _row_created_at_timestamp(r)
                        match = created_at_ts is not None and created_at_ts >= float(val)
                    except ValueError:
                        pass
                elif base_k == "until_time":
                    try:
                        created_at_ts = _row_created_at_timestamp(r)
                        match = created_at_ts is not None and created_at_ts < float(val)
                    except ValueError:
                        pass
                elif base_k == "since_id":
                    try:
                        match = int(r.get("tweet_id", 0)) > int(val)
                    except ValueError:
                        pass
                elif base_k == "max_id":
                    try:
                        match = int(r.get("tweet_id", 0)) <= int(val)
                    except ValueError:
                        pass
                elif base_k == "has":
                    if val == "media":
                        match = bool(r.get("media"))
                    elif val == "image":
                        match = any(m.get("type") == "photo" for m in r.get("media", []))
                    elif val == "video":
                        match = any(
                            m.get("type") in ("video", "animated_gif") for m in r.get("media", [])
                        )
                    elif val == "links":
                        match = bool(r.get("urls"))
                elif base_k == "is":
                    if val == "reply":
                        match = bool(legacy.get("in_reply_to_status_id_str"))
                elif base_k == "filter":
                    if val == "replies":
                        match = bool(legacy.get("in_reply_to_status_id_str"))
                    elif val == "quote":
                        match = bool(legacy.get("is_quote_status"))
                    elif val == "nativeretweets":
                        match = bool(legacy.get("retweeted_status_id_str"))
                    elif val in ("self_threads", "threads"):
                        match = (legacy.get("in_reply_to_screen_name") or "").lower() == r.get(
                            "author", {}
                        ).get("username", "").lower()
                    elif val == "media":
                        match = bool(r.get("media"))
                    elif val == "images":
                        match = any(m.get("type") == "photo" for m in r.get("media", []))
                    elif val == "videos" or val == "native_video":
                        match = any(
                            m.get("type") in ("video", "animated_gif") for m in r.get("media", [])
                        )
                    elif val == "links":
                        match = bool(r.get("urls"))
                    elif val == "verified":
                        user_result = raw.get("core", {}).get("user_results", {}).get("result", {})
                        match = user_result.get("is_blue_verified") or user_result.get(
                            "legacy", {}
                        ).get("verified")
                elif base_k == "min_retweets":
                    try:
                        match = int(legacy.get("retweet_count", 0)) >= int(val)
                    except ValueError:
                        pass
                elif base_k == "min_faves":
                    try:
                        match = int(legacy.get("favorite_count", 0)) >= int(val)
                    except ValueError:
                        pass
                elif base_k == "min_replies":
                    try:
                        match = int(legacy.get("reply_count", 0)) >= int(val)
                    except ValueError:
                        pass
                elif base_k == "conversation_id":
                    match = legacy.get("conversation_id_str") == val
                elif base_k == "quoted_tweet_id":
                    match = legacy.get("quoted_status_id_str") == val
                elif base_k == "url":
                    match = any(
                        val in (u.get("expanded_url") or "").lower()
                        or val in (u.get("display_url") or "").lower()
                        for u in r.get("urls", [])
                    )
                elif base_k == "source":
                    match = val.replace("_", " ") in raw.get("source", "").lower()
                elif base_k == "card_name":
                    match = raw.get("card", {}).get("name") == val
                elif base_k == "tag":
                    media_tags = r.get("media_tags", {})
                    tags = media_tags.get("tags", []) if media_tags else []
                    match = any(val.lower() == tag.lower() for tag in tags)
                elif base_k == "hashtag":
                    hashtags = [
                        h.get("text", "").lower()
                        for h in legacy.get("entities", {}).get("hashtags", [])
                    ]
                    match = val.replace("#", "").lower() in hashtags
                else:
                    match = True

                if is_negated:
                    if match:
                        keep = False
                else:
                    if not match:
                        keep = False

                if not keep:
                    break
            if not keep:
                break
        if keep:
            filtered.append(r)
    return filtered


@router.get("/api/tweets")
def api_tweets(
    q: str | None = None,
    collection: str = Query("all"),
    sort: str = Query("default"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    store=Depends(get_store),  # noqa: B008
    _auth: bool = Depends(verify_credentials),
):
    """Fetch tweets with pagination, collection filtering, and advanced search."""
    try:
        try:
            internal_col = normalize_collection_name(collection)
        except ValueError:
            internal_col = "all"

        filters, text_query = _extract_advanced_filters(q)

        start = (page - 1) * limit
        end = start + limit

        pushable_exprs = []
        post_filters = {}
        for k, v in filters.items():
            base_k = k[1:] if k.startswith("-") else k
            if not k.startswith("-") and base_k in {
                "from",
                "conversation_id",
                "since",
                "until",
                "since_time",
                "until_time",
                "tag",
            }:
                if base_k == "from":
                    vals = [val.replace("@", "") for val in v]
                    joined = " OR ".join(
                        f"LOWER(author_username) = {_sql_quote(val)}" for val in vals
                    )
                    pushable_exprs.append(f"({joined})")
                elif base_k == "conversation_id":
                    joined = " OR ".join(f"conversation_id = {_sql_quote(val)}" for val in v)
                    pushable_exprs.append(f"({joined})")
                elif base_k == "tag":
                    exprs = []
                    for val in v:
                        safe_val = val.replace("'", "''")
                        exprs.append(
                            "tweet_id IN (SELECT tweet_id FROM archive "
                            "WHERE record_type = 'media_tag' "
                            "AND LOWER(raw_json) LIKE "
                            f"""LOWER('%"' || '{safe_val}' || '"%'))"""
                        )
                    pushable_exprs.append(f"({' AND '.join(exprs)})")
                elif base_k == "since" or base_k == "since_time":
                    try:
                        ts = _parse_twitter_date(v[0]) if base_k == "since" else float(v[0])
                        if ts is not None:
                            pushable_exprs.append(f"created_at_ts >= {int(ts)}")
                    except Exception:
                        pass
                elif base_k == "until" or base_k == "until_time":
                    try:
                        ts = _parse_twitter_date(v[0]) if base_k == "until" else float(v[0])
                        if ts is not None:
                            pushable_exprs.append(f"created_at_ts < {int(ts)}")
                    except Exception:
                        pass
            else:
                post_filters[k] = v

        paginated_tweets = []
        total = 0

        effective_sort = "newest"
        if sort == "oldest":
            effective_sort = "oldest"
        elif sort == "random":
            effective_sort = "random"
        elif sort == "relevance" and text_query:
            effective_sort = "relevance"
        elif sort == "default":
            effective_sort = "relevance" if text_query else "newest"

        def sort_index_val(row):
            try:
                return int(row.get("sort_index") or 0)
            except Exception:
                return 0

        def newest_key(row):
            return (
                0,
                -(row.get("created_at_ts") or 0.0),
                -sort_index_val(row),
                row.get("tweet_id") or "",
            )

        def oldest_key(row):
            return (
                0,
                (row.get("created_at_ts") or 0.0),
                sort_index_val(row),
                row.get("tweet_id") or "",
            )

        if not post_filters and not text_query:
            filter_expr = "record_type = 'tweet'"
            if internal_col != "all":
                filter_expr += f" AND collection_type = {_sql_quote(internal_col)}"
            for expr in pushable_exprs:
                filter_expr += f" AND {expr}"
            unavailable_values = ", ".join(_sql_quote(text) for text in _UNAVAILABLE_TWEET_TEXTS)
            filter_expr += f" AND (text IS NULL OR text NOT IN ({unavailable_values}))"

            total = store._count_distinct("tweet_id", filter_expr)
            order_by = "created_at_ts DESC, CAST(sort_index AS INTEGER) DESC, tweet_id DESC"
            if effective_sort == "oldest":
                order_by = "created_at_ts ASC, CAST(sort_index AS INTEGER) ASC, tweet_id ASC"
            elif effective_sort == "random":
                order_by = "RANDOM()"

            tweet_rows = store._query(
                expr=filter_expr,
                cols=["DISTINCT tweet_id"],
                limit=limit,
                offset=start,
                order_by=order_by,
            )
            page_ids = [r["tweet_id"] for r in tweet_rows if r.get("tweet_id")]
            paginated_tweets = store.fetch_tweets_by_ids(page_ids)

        elif not post_filters and text_query and not pushable_exprs:
            coll_set = {internal_col} if internal_col != "all" else None
            search_results = store.search_fts(text_query, limit=1000, collections=coll_set)
            search_results = [row for row in search_results if _is_available_tweet(row)]

            if effective_sort == "newest" or effective_sort == "oldest":
                for r in search_results:
                    if r.get("created_at_ts") is None and r.get("created_at"):
                        try:
                            r["created_at_ts"] = datetime.strptime(
                                r.get("created_at") or "", "%a %b %d %H:%M:%S %z %Y"
                            ).timestamp()
                        except Exception:
                            r["created_at_ts"] = 0.0
                search_results.sort(key=oldest_key if effective_sort == "oldest" else newest_key)
            elif effective_sort == "random":
                import random

                random.shuffle(search_results)

            total = len(search_results)
            page_results = search_results[start:end]
            page_ids = [r["tweet_id"] for r in page_results if r.get("tweet_id")]
            paginated_tweets = store.fetch_tweets_by_ids(page_ids)

        else:
            all_rows = store.export_rows(internal_col, sort="newest", include_raw_json=True)
            all_rows = [row for row in all_rows if _is_available_tweet(row)]
            if text_query:
                coll_set = {internal_col} if internal_col != "all" else None
                search_results = store.search_fts(text_query, limit=1000, collections=coll_set)
                matched_ids = {r["tweet_id"] for r in search_results}
                all_rows = [r for r in all_rows if r["tweet_id"] in matched_ids]
                order = {r["tweet_id"]: i for i, r in enumerate(search_results)}
                if effective_sort == "relevance":
                    all_rows.sort(key=lambda x: order.get(x["tweet_id"], 9999))
                elif effective_sort == "random":
                    import random

                    random.shuffle(all_rows)
                elif effective_sort == "oldest":
                    all_rows.reverse()

            filtered_rows = _apply_advanced_filters(all_rows, filters)

            if effective_sort == "random":
                import random

                random.shuffle(filtered_rows)
            elif effective_sort == "oldest" and not text_query:
                filtered_rows.reverse()

            total = len(filtered_rows)
            paginated_tweets = filtered_rows[start:end]

        paginated_tweets = [tweet for tweet in paginated_tweets if _is_available_tweet(tweet)]

        qt_ids = set()
        for r in paginated_tweets:
            raw = r.get("raw_json", {})
            if isinstance(raw, dict):
                quote = raw.get("quoted_status_result", {}).get("result")
                if isinstance(quote, dict):
                    if quote.get("__typename") == "TweetWithVisibilityResults":
                        quote = quote.get("tweet", {})
                    qt_id = quote.get("rest_id")
                    if qt_id:
                        qt_ids.add(qt_id)

        if qt_ids:
            qt_media_rows = store._rows_for_values("media", "tweet_id", list(qt_ids))
            qt_media_by_id = {}
            for m in qt_media_rows:
                tid = m.get("tweet_id")
                if tid:
                    qt_media_by_id.setdefault(tid, []).append(m)

            for r in paginated_tweets:
                raw = r.get("raw_json", {})
                if isinstance(raw, dict):
                    quote = raw.get("quoted_status_result", {}).get("result")
                    if isinstance(quote, dict):
                        if quote.get("__typename") == "TweetWithVisibilityResults":
                            quote = quote.get("tweet", {})
                        qt_id = quote.get("rest_id")
                        if qt_id and qt_id in qt_media_by_id:
                            r["qt_media"] = [
                                {
                                    "type": m.get("media_type"),
                                    "width": m.get("width"),
                                    "height": m.get("height"),
                                    "duration_millis": m.get("duration_millis"),
                                    "download": {
                                        "local_path": m.get("local_path"),
                                        "thumbnail_local_path": m.get("thumbnail_local_path"),
                                    },
                                }
                                for m in qt_media_by_id[qt_id][:10]
                            ]

        return {
            "tweets": paginated_tweets,
            "total": total,
            "page": page,
            "pages": math.ceil(total / limit) if total > 0 else 1,
        }
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/tweets/{tweet_id}")
def api_tweet_thread(
    tweet_id: str,
    store=Depends(get_store),  # noqa: B008
    _auth: bool = Depends(verify_credentials),
):
    try:
        all_relations = []
        related_ids = {tweet_id}

        curr_id = tweet_id
        seen_ancestor_ids = {tweet_id}
        for _ in range(50):
            quoted_curr_id = _sql_quote(curr_id)
            p_rels = store._query(
                expr=(
                    "record_type = 'tweet_relation' AND ("
                    f"(tweet_id = {quoted_curr_id} "
                    "AND relation_type IN ('reply_to', 'thread_parent')) OR "
                    f"(target_tweet_id = {quoted_curr_id} "
                    "AND relation_type = 'thread_child'))"
                ),
                limit=10,
            )
            if not p_rels:
                break

            next_parent = None
            for r in p_rels:
                all_relations.append(r)
                if r.get("tweet_id"):
                    related_ids.add(r["tweet_id"])
                if r.get("target_tweet_id"):
                    related_ids.add(r["target_tweet_id"])
                if (
                    r.get("relation_type") in ("reply_to", "thread_parent")
                    and r.get("tweet_id") == curr_id
                ):
                    next_parent = r.get("target_tweet_id")
                elif (
                    r.get("relation_type") == "thread_child" and r.get("target_tweet_id") == curr_id
                ):
                    next_parent = r.get("tweet_id")

            if not next_parent or next_parent in seen_ancestor_ids:
                break
            seen_ancestor_ids.add(next_parent)
            curr_id = next_parent

        quoted_tweet_id = _sql_quote(tweet_id)
        c_rels = store._query(
            expr=(
                "record_type = 'tweet_relation' AND ("
                f"(target_tweet_id = {quoted_tweet_id} "
                "AND relation_type IN ('reply_to', 'thread_parent')) OR "
                f"(tweet_id = {quoted_tweet_id} "
                "AND relation_type = 'thread_child'))"
            ),
            limit=100,
        )
        child_candidates = set()
        for r in c_rels:
            all_relations.append(r)
            if r.get("tweet_id"):
                related_ids.add(r["tweet_id"])
            if r.get("target_tweet_id"):
                related_ids.add(r["target_tweet_id"])
            if (
                r.get("relation_type") in ("reply_to", "thread_parent")
                and r.get("target_tweet_id") == tweet_id
            ):
                child_candidates.add(r.get("tweet_id"))
            elif r.get("relation_type") == "thread_child" and r.get("tweet_id") == tweet_id:
                child_candidates.add(r.get("target_tweet_id"))

        if child_candidates:
            child_id_list = ", ".join(_sql_quote(cid) for cid in child_candidates if cid)
            sub_rels = store._query(
                expr=f"record_type = 'tweet_relation' AND target_tweet_id IN ({child_id_list})",
                limit=100,
            )
            for sr in sub_rels:
                all_relations.append(sr)
                if sr.get("tweet_id"):
                    related_ids.add(sr["tweet_id"])
                if sr.get("target_tweet_id"):
                    related_ids.add(sr["target_tweet_id"])

        id_list = ", ".join(_sql_quote(tid) for tid in related_ids)
        objs = store._query(
            expr=f"record_type = 'tweet_object' AND tweet_id IN ({id_list})", limit=100
        )
        media = store._query(expr=f"record_type = 'media' AND tweet_id IN ({id_list})", limit=100)
        col_rows = store._query(
            expr=f"record_type = 'tweet' AND tweet_id IN ({id_list})", limit=100
        )
        tag_rows = store._query(
            expr=f"record_type = 'media_tag' AND tweet_id IN ({id_list})", limit=100
        )

        col_dict = {}
        for c in col_rows:
            col_dict.setdefault(c["tweet_id"], []).append(c["collection_type"])

        tags_dict = {}
        for row in tag_rows:
            if not row.get("tweet_id") or not row.get("raw_json"):
                continue
            try:
                tags_dict[row["tweet_id"]] = json.loads(row["raw_json"])
            except (TypeError, json.JSONDecodeError):
                continue

        qt_ids = set()
        for obj in objs:
            if obj.get("raw_json"):
                raw_json = json.loads(obj["raw_json"])
                if isinstance(raw_json, dict):
                    quote = raw_json.get("quoted_status_result", {}).get("result")
                    if isinstance(quote, dict):
                        if quote.get("__typename") == "TweetWithVisibilityResults":
                            quote = quote.get("tweet", {})
                        qt_id = quote.get("rest_id")
                        if qt_id:
                            qt_ids.add(qt_id)

        qt_media_by_id = {}
        if qt_ids:
            qt_media_rows = store._rows_for_values("media", "tweet_id", list(qt_ids))
            for m in qt_media_rows:
                tid = m.get("tweet_id")
                if tid:
                    qt_media_by_id.setdefault(tid, []).append(m)

        formatted = {}
        for obj in objs:
            tid = obj["tweet_id"]
            t_media = [m for m in media if m.get("tweet_id") == tid]
            raw_json = json.loads(obj["raw_json"]) if obj.get("raw_json") else None

            qt_media_formatted = []
            if raw_json and isinstance(raw_json, dict):
                quote = raw_json.get("quoted_status_result", {}).get("result")
                if isinstance(quote, dict):
                    if quote.get("__typename") == "TweetWithVisibilityResults":
                        quote = quote.get("tweet", {})
                    qt_id = quote.get("rest_id")
                    if qt_id and qt_id in qt_media_by_id:
                        qt_media_formatted = [
                            {
                                "type": m.get("media_type"),
                                "width": m.get("width"),
                                "height": m.get("height"),
                                "duration_millis": m.get("duration_millis"),
                                "download": {
                                    "local_path": m.get("local_path"),
                                    "thumbnail_local_path": m.get("thumbnail_local_path"),
                                },
                            }
                            for m in qt_media_by_id[qt_id][:10]
                        ]

            formatted[tid] = {
                "tweet_id": tid,
                "text": obj.get("text", ""),
                "collections": col_dict.get(tid, []),
                "author": {
                    "id": obj.get("author_id"),
                    "username": obj.get("author_username"),
                    "display_name": obj.get("author_display_name"),
                },
                "created_at": obj.get("created_at"),
                "synced_at": obj.get("synced_at"),
                "media": [
                    {
                        "type": m.get("media_type"),
                        "width": m.get("width"),
                        "height": m.get("height"),
                        "duration_millis": m.get("duration_millis"),
                        "download": {
                            "local_path": m.get("local_path"),
                            "thumbnail_local_path": m.get("thumbnail_local_path"),
                        },
                    }
                    for m in t_media
                ],
                "raw_json": raw_json,
                "qt_media": qt_media_formatted,
                "media_tags": tags_dict.get(tid),
            }

        main_tweet = formatted.get(tweet_id)
        if not main_tweet:
            raise HTTPException(status_code=404, detail="Tweet not found")

        quote_rows = store._query(
            expr="record_type = 'tweet_relation' AND relation_type = 'quote_of' "
            f"AND target_tweet_id = {_sql_quote(tweet_id)}"
        )
        main_tweet["local_quote_count"] = len(
            set(r.get("tweet_id") for r in quote_rows if r.get("tweet_id"))
        )

        parents = []
        curr_id = tweet_id
        seen_parents = set()
        while True:
            next_parent = None
            for r in all_relations:
                rel_type = r.get("relation_type")
                src = r.get("tweet_id")
                tgt = r.get("target_tweet_id")

                if src == curr_id and rel_type in ("reply_to", "thread_parent"):
                    next_parent = tgt
                    break
                elif tgt == curr_id and rel_type == "thread_child":
                    next_parent = src
                    break

            if next_parent and next_parent in formatted and next_parent not in seen_parents:
                parents.append(formatted[next_parent])
                seen_parents.add(next_parent)
                curr_id = next_parent
            else:
                break

        children_map = {}
        for r in all_relations:
            rel_type = r.get("relation_type")
            src = r.get("tweet_id")
            tgt = r.get("target_tweet_id")

            if tgt == tweet_id and rel_type in ("reply_to", "thread_parent"):
                if src in formatted and src not in children_map:
                    children_map[src] = formatted[src]
                    children_map[src]["op_replies"] = []
            elif src == tweet_id and rel_type == "thread_child":
                if tgt in formatted and tgt not in children_map:
                    children_map[tgt] = formatted[tgt]
                    children_map[tgt]["op_replies"] = []

        main_author_id = main_tweet["author"]["id"]
        for r in all_relations:
            rel_type = r.get("relation_type")
            src = r.get("tweet_id")
            tgt = r.get("target_tweet_id")

            if tgt in children_map and rel_type in ("reply_to", "thread_parent"):
                grandchild = formatted.get(src)
                if grandchild and grandchild["author"]["id"] == main_author_id:
                    if grandchild not in children_map[tgt]["op_replies"]:
                        children_map[tgt]["op_replies"].append(grandchild)

        parents.sort(key=lambda x: x["created_at"] or "")
        children = list(children_map.values())

        def get_likes(t):
            raw = t.get("raw_json", {}) or {}
            return int(raw.get("legacy", {}).get("favorite_count", 0))

        children.sort(key=lambda x: (len(x.get("op_replies", [])) > 0, get_likes(x)), reverse=True)

        return {"main": main_tweet, "parents": parents, "children": children}
    except HTTPException:
        raise
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/tweets/{tweet_id}/quotes")
def api_tweet_quotes(
    tweet_id: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    store=Depends(get_store),  # noqa: B008
    _auth: bool = Depends(verify_credentials),
):
    """Fetch quotes of a specific tweet."""
    try:
        start = (page - 1) * limit

        expr = (
            "record_type = 'tweet_relation' AND relation_type = 'quote_of' "
            f"AND target_tweet_id = {_sql_quote(tweet_id)}"
        )
        total = store.conn.execute(
            """
            SELECT COUNT(DISTINCT tweet_id)
            FROM archive
            WHERE record_type = 'tweet_relation'
              AND relation_type = 'quote_of'
              AND target_tweet_id = ?
            """,
            (tweet_id,),
        ).fetchone()[0]

        rows = store._query(
            expr=expr,
            cols=["DISTINCT tweet_id"],
            order_by="tweet_id DESC",
            limit=limit,
            offset=start,
        )
        paginated_ids = [r.get("tweet_id") for r in rows if r.get("tweet_id")]
        paginated_tweets = store.fetch_tweets_by_ids(paginated_ids)

        return {"tweets": paginated_tweets, "total": total, "page": page, "limit": limit}
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/authors/search")
def search_authors_api(
    q: str = Query(""),
    store=Depends(get_store),  # noqa: B008
    _auth: bool = Depends(verify_credentials),
):
    """Search for authors by username or display name."""
    try:
        authors = store.search_authors(q, limit=10)
        return {"authors": authors}
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e)) from e
