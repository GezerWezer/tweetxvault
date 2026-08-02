"""Archive statistics endpoints."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from tweetxvault.storage import ArchiveStore
from tweetxvault.storage.backend import AVAILABLE_ENRICHMENT_STATES
from tweetxvault.web.deps import get_store, verify_credentials

router = APIRouter(
    prefix="/api/stats",
    tags=["stats"],
    dependencies=[Depends(verify_credentials)],
)

UNAVAILABLE_REASON_LABELS = (
    ("protected_account", "Protected account"),
    ("suspended_account", "Suspended account"),
    ("account_missing", "Missing account"),
    ("withheld", "Withheld"),
    ("not_found", "Not found"),
    ("unavailable_unknown", "Unknown availability"),
    ("deleted_by_author", "Deleted by author"),
    ("archive_deleted", "Deleted in archive"),
)


def _percentage(count: int, total: int) -> float:
    return round((count / total) * 100, 1) if total else 0.0


def _archive_availability_counts(conn: Any) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT
            count(*),
            count(*) FILTER (WHERE enrichment_state = 'terminal_unavailable')
        FROM archive
        WHERE record_type = 'tweet_object'
        """
    ).fetchone()
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


def _unavailable_breakdown(
    conn: Any,
    *,
    archive_total: int,
    unavailable_total: int,
) -> dict[str, Any]:
    now = datetime.now(tz=UTC).isoformat()
    rows = conn.execute(
        """
        SELECT
            COALESCE(NULLIF(enrichment_reason, ''), 'unavailable_unknown') AS reason,
            count(*) AS count,
            sum(CASE
                WHEN deleted_at IS NOT NULL
                  OR enrichment_retry_eligible = 0
                  OR enrichment_reason IN ('archive_deleted', 'deleted_by_author')
                THEN 1 ELSE 0
            END) AS permanent,
            sum(CASE
                WHEN deleted_at IS NULL
                  AND enrichment_retry_eligible = 1
                  AND COALESCE(enrichment_reason, '') NOT IN (
                      'archive_deleted', 'deleted_by_author'
                  )
                THEN 1 ELSE 0
            END) AS retryable,
            sum(CASE
                WHEN deleted_at IS NULL
                  AND enrichment_retry_eligible = 1
                  AND COALESCE(enrichment_reason, '') NOT IN (
                      'archive_deleted', 'deleted_by_author'
                  )
                  AND (
                      enrichment_next_retry_at IS NULL
                      OR enrichment_next_retry_at <= ?
                  )
                THEN 1 ELSE 0
            END) AS due,
            sum(CASE
                WHEN deleted_at IS NULL
                  AND enrichment_retry_eligible = 1
                  AND COALESCE(enrichment_reason, '') NOT IN (
                      'archive_deleted', 'deleted_by_author'
                  )
                  AND enrichment_next_retry_at > ?
                THEN 1 ELSE 0
            END) AS delayed
        FROM archive
        WHERE record_type = 'tweet_object'
          AND enrichment_state = 'terminal_unavailable'
        GROUP BY COALESCE(NULLIF(enrichment_reason, ''), 'unavailable_unknown')
        """,
        (now, now),
    ).fetchall()
    counts_by_reason = {
        str(row[0]): {
            "count": int(row[1] or 0),
            "permanent": int(row[2] or 0),
            "retryable": int(row[3] or 0),
            "due": int(row[4] or 0),
            "delayed": int(row[5] or 0),
        }
        for row in rows
    }

    labels = dict(UNAVAILABLE_REASON_LABELS)
    ordered_reasons = [reason for reason, _label in UNAVAILABLE_REASON_LABELS]
    ordered_reasons.extend(sorted(set(counts_by_reason) - set(ordered_reasons)))
    reasons = []
    for reason in ordered_reasons:
        counts = counts_by_reason.get(
            reason,
            {"count": 0, "permanent": 0, "retryable": 0, "due": 0, "delayed": 0},
        )
        count = counts["count"]
        reasons.append(
            {
                "reason": reason,
                "label": labels.get(reason, reason.replace("_", " ").title()),
                "count": count,
                "percent_of_missing": _percentage(count, unavailable_total),
                "percent_of_archive": _percentage(count, archive_total),
                "retryable": counts["retryable"],
                "due": counts["due"],
                "delayed": counts["delayed"],
                "permanent": counts["permanent"],
            }
        )

    return {
        "total": unavailable_total,
        "percent_of_archive": _percentage(unavailable_total, archive_total),
        "retryable": sum(item["retryable"] for item in reasons),
        "due": sum(item["due"] for item in reasons),
        "delayed": sum(item["delayed"] for item in reasons),
        "permanent": sum(item["permanent"] for item in reasons),
        "reasons": reasons,
    }


def _format_ts(ts: int | float | str | None) -> str | None:
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = datetime.fromtimestamp(ts, tz=UTC)
    return parsed.astimezone(UTC).strftime("%b %d, %Y")


@router.get("/summary")
def get_stats_summary(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return the summary counts shown in the statistics view."""
    conn = store.conn

    unique_posts = (
        conn.execute(
            "SELECT count(DISTINCT tweet_id) FROM archive WHERE record_type = 'tweet'"
        ).fetchone()[0]
        or 0
    )
    article_count = (
        conn.execute("SELECT count(*) FROM archive WHERE record_type = 'article'").fetchone()[0]
        or 0
    )
    media_count = (
        conn.execute("SELECT count(*) FROM archive WHERE record_type = 'media'").fetchone()[0] or 0
    )
    url_count = (
        conn.execute("SELECT count(*) FROM archive WHERE record_type = 'url'").fetchone()[0] or 0
    )
    profile_count = (
        conn.execute(
            """
            SELECT count(DISTINCT author_id)
            FROM archive
            WHERE author_id IS NOT NULL AND author_id != ''
            """
        ).fetchone()[0]
        or 0
    )
    archive_tweets, missing_archive_tweets = _archive_availability_counts(conn)

    range_row = conn.execute(
        """
        SELECT min(created_at_ts), max(created_at_ts)
        FROM archive
        WHERE record_type = 'tweet' AND created_at_ts > 0
        """
    ).fetchone()
    oldest_ts = range_row[0] if range_row else None
    newest_ts = range_row[1] if range_row else None

    latest_sync_row = conn.execute(
        "SELECT max(updated_at) FROM archive WHERE record_type = 'sync_state'"
    ).fetchone()
    latest_sync_ts = latest_sync_row[0] if latest_sync_row else None

    owner_row = conn.execute(
        """
        SELECT value
        FROM archive
        WHERE record_type = 'metadata' AND key = 'owner_user_id'
        """
    ).fetchone()
    owner_user_id = owner_row[0] if owner_row else None

    return {
        "owner_user_id": owner_user_id or "Local Vault",
        "unique_posts": unique_posts,
        "articles": article_count,
        "media_rows": media_count,
        "urls": url_count,
        "profiles": profile_count,
        "archive_tweets": archive_tweets,
        "missing_archive_tweets": missing_archive_tweets,
        "missing_archive_pct": _percentage(missing_archive_tweets, archive_tweets),
        "oldest_post": _format_ts(oldest_ts),
        "newest_post": _format_ts(newest_ts),
        "latest_sync": _format_ts(latest_sync_ts),
    }


def _format_stats_timestamp(raw: str | None) -> str:
    if not raw:
        return "-"
    try:
        from tweetxvault.storage.backend import _parse_created_at

        parsed = _parse_created_at(raw)
        if parsed is None:
            parsed = datetime.fromisoformat(raw)
        local_dt = parsed.astimezone() if parsed.tzinfo is not None else parsed
        date_part = local_dt.strftime("%b %d, %Y")
        time_part = local_dt.strftime("%I:%M %p").lower().lstrip("0")
        return f"{date_part} {time_part}"
    except Exception:
        return raw


@router.get("/collections")
def get_stats_collections(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> list[dict[str, Any]]:
    """Return the per-collection statistics shown in the statistics view."""
    stats = store.archive_stats()
    result = []

    for coll in stats.collections:
        if coll.backfill_incomplete and coll.backfill_cursor:
            bf_status = "resume older"
        elif coll.backfill_incomplete:
            bf_status = "incomplete"
        elif coll.backfill_cursor:
            bf_status = "saved cursor"
        else:
            bf_status = "none saved"

        display_name = coll.collection_type.capitalize()
        if coll.collection_type == "bookmark":
            display_name = "Bookmarks"
        elif coll.collection_type == "like":
            display_name = "Likes"
        elif coll.collection_type == "tweet":
            display_name = "Authored Tweets"

        result.append(
            {
                "collection": display_name,
                "raw_type": coll.collection_type,
                "count": coll.post_count,
                "oldest": _format_stats_timestamp(coll.oldest_created_at),
                "newest": _format_stats_timestamp(coll.newest_created_at),
                "last_synced": _format_stats_timestamp(coll.last_synced_at),
                "backfill_status": bf_status,
            }
        )

    return result


@router.get("/health")
def get_stats_health(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return pipeline and processing health metrics."""
    conn = store.conn
    archive_tweets, missing_archive_tweets = _archive_availability_counts(conn)

    enrich_done = (
        conn.execute(
            """
            SELECT count(*)
            FROM archive
            WHERE record_type = 'tweet_object' AND enrichment_state = 'done'
            """
        ).fetchone()[0]
        or 0
    )
    available_placeholders = ", ".join("?" for _state in AVAILABLE_ENRICHMENT_STATES)
    enrich_available = (
        conn.execute(
            f"""
            SELECT count(*)
            FROM archive
            WHERE record_type = 'tweet_object'
              AND enrichment_state IN ({available_placeholders})
            """,
            AVAILABLE_ENRICHMENT_STATES,
        ).fetchone()[0]
        or 0
    )
    enrich_resurrected = enrich_available - enrich_done
    enrich_pending = (
        conn.execute(
            """
            SELECT count(*)
            FROM archive
            WHERE record_type = 'tweet_object' AND enrichment_state = 'pending'
            """
        ).fetchone()[0]
        or 0
    )
    enrich_transient = (
        conn.execute(
            """
            SELECT count(*)
            FROM archive
            WHERE record_type = 'tweet_object' AND enrichment_state = 'transient_failure'
            """
        ).fetchone()[0]
        or 0
    )
    enrich_terminal = missing_archive_tweets
    threads_expanded = (
        conn.execute(
            """
            SELECT count(DISTINCT conversation_id)
            FROM archive
            WHERE record_type = 'tweet' AND conversation_id IS NOT NULL
            """
        ).fetchone()[0]
        or 0
    )
    preview_articles = (
        conn.execute(
            """
            SELECT count(*)
            FROM archive
            WHERE record_type = 'article' AND status = 'preview_only'
            """
        ).fetchone()[0]
        or 0
    )

    return {
        "enrichment": {
            "available": enrich_available,
            "done": enrich_done,
            "resurrected": enrich_resurrected,
            "pending": enrich_pending,
            "transient": enrich_transient,
            "incomplete": enrich_pending + enrich_transient,
            "terminal": enrich_terminal,
            "unavailable": _unavailable_breakdown(
                conn,
                archive_total=archive_tweets,
                unavailable_total=enrich_terminal,
            ),
        },
        "threads_expanded": threads_expanded,
        "preview_articles": preview_articles,
    }


@router.get("/tags")
def get_stats_tags(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return AI tagging and metadata metrics."""
    conn = store.conn

    eligible_tweets = (
        conn.execute(
            """
            SELECT count(DISTINCT tweet_id)
            FROM archive
            WHERE record_type = 'media'
            """
        ).fetchone()[0]
        or 0
    )
    tagged_tweets = (
        conn.execute(
            """
            SELECT count(DISTINCT tweet_id)
            FROM archive
            WHERE record_type = 'media_tag'
              AND json_valid(raw_json)
              AND json_type(raw_json, '$.tags') = 'array'
              AND json_array_length(raw_json, '$.tags') > 0
            """
        ).fetchone()[0]
        or 0
    )
    unique_tags = (
        conn.execute(
            """
            SELECT count(DISTINCT LOWER(t.value))
            FROM (
                SELECT raw_json
                FROM archive
                WHERE record_type = 'media_tag' AND json_valid(raw_json)
            ) a, json_each(a.raw_json, '$.tags') AS t
            """
        ).fetchone()[0]
        or 0
    )
    total_tag_instances = (
        conn.execute(
            """
            SELECT count(t.value)
            FROM (
                SELECT raw_json
                FROM archive
                WHERE record_type = 'media_tag' AND json_valid(raw_json)
            ) a, json_each(a.raw_json, '$.tags') AS t
            """
        ).fetchone()[0]
        or 0
    )
    top_tags_rows = conn.execute(
        """
        SELECT LOWER(t.value) AS tag, count(*) AS count
        FROM (
            SELECT raw_json
            FROM archive
            WHERE record_type = 'media_tag' AND json_valid(raw_json)
        ) a, json_each(a.raw_json, '$.tags') AS t
        GROUP BY LOWER(t.value)
        ORDER BY count DESC
        LIMIT 20
        """
    ).fetchall()

    top_tags = [{"tag": row[0], "count": row[1]} for row in top_tags_rows]
    coverage_pct = round(tagged_tweets / eligible_tweets * 100, 1) if eligible_tweets > 0 else 0.0
    avg_tags_per_tweet = round(total_tag_instances / tagged_tweets, 1) if tagged_tweets > 0 else 0.0

    return {
        "eligible_tweets": eligible_tweets,
        "tagged_tweets": tagged_tweets,
        "untagged_eligible": max(0, eligible_tweets - tagged_tweets),
        "unique_tags": unique_tags,
        "total_tag_instances": total_tag_instances,
        "coverage_pct": coverage_pct,
        "avg_tags_per_tweet": avg_tags_per_tweet,
        "top_tags": top_tags,
    }
