from fastapi import APIRouter, Depends
from typing import Dict, Any, List

from tweetxvault.web.deps import get_store, verify_credentials
from tweetxvault.storage import ArchiveStore

router = APIRouter(prefix="/api/stats", tags=["stats"], dependencies=[Depends(verify_credentials)])

def _format_ts(ts: int | float | None) -> str | None:
    if not ts:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")

@router.get("/summary")
def get_stats_summary(store: ArchiveStore = Depends(get_store)) -> Dict[str, Any]:
    """Fast summary counts designed to resolve in <10ms."""
    conn = store.conn
    
    unique_posts = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'tweet'").fetchone()[0] or 0
    article_count = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'article'").fetchone()[0] or 0
    media_count = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'media'").fetchone()[0] or 0
    url_count = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'url'").fetchone()[0] or 0
    profile_count = conn.execute("SELECT count(DISTINCT author_id) FROM archive WHERE author_id IS NOT NULL AND author_id != ''").fetchone()[0] or 0
    
    range_row = conn.execute(
        "SELECT min(created_at_ts), max(created_at_ts) FROM archive WHERE record_type = 'tweet' AND created_at_ts > 0"
    ).fetchone()
    oldest_ts = range_row[0] if range_row else None
    newest_ts = range_row[1] if range_row else None

    latest_sync_row = conn.execute(
        "SELECT max(created_at_ts) FROM archive WHERE record_type = 'sync_state'"
    ).fetchone()
    latest_sync_ts = latest_sync_row[0] if latest_sync_row else None

    owner_row = conn.execute(
        "SELECT value FROM archive WHERE record_type = 'metadata' AND key = 'owner_user_id'"
    ).fetchone()
    owner_user_id = owner_row[0] if owner_row else None

    return {
        "owner_user_id": owner_user_id or "Local Vault",
        "unique_posts": unique_posts,
        "articles": article_count,
        "media_rows": media_count,
        "urls": url_count,
        "profiles": profile_count,
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
            from datetime import datetime
            parsed = datetime.fromisoformat(raw)
        local_dt = parsed.astimezone() if parsed.tzinfo is not None else parsed
        date_part = local_dt.strftime("%b %d, %Y")
        time_part = local_dt.strftime("%I:%M %p").lower().lstrip("0")
        return f"{date_part} {time_part}"
    except Exception:
        return raw or "-"

@router.get("/collections")
def get_stats_collections(store: ArchiveStore = Depends(get_store)) -> List[Dict[str, Any]]:
    """Per-collection breakdown matching tweetxvault stats CLI output."""
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

        result.append({
            "collection": display_name,
            "raw_type": coll.collection_type,
            "count": coll.post_count,
            "oldest": _format_stats_timestamp(coll.oldest_created_at),
            "newest": _format_stats_timestamp(coll.newest_created_at),
            "last_synced": _format_stats_timestamp(coll.last_synced_at),
            "backfill_status": bf_status
        })

    return result

@router.get("/health")
def get_stats_health(store: ArchiveStore = Depends(get_store)) -> Dict[str, Any]:
    """Pipeline and processing health metrics."""
    conn = store.conn

    enrich_done = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'tweet_object' AND enrichment_state = 'done'").fetchone()[0] or 0
    enrich_pending = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'tweet_object' AND enrichment_state = 'pending'").fetchone()[0] or 0
    enrich_terminal = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'tweet_object' AND enrichment_state = 'terminal_unavailable'").fetchone()[0] or 0

    threads_expanded = conn.execute("SELECT count(DISTINCT conversation_id) FROM archive WHERE record_type = 'tweet' AND conversation_id IS NOT NULL").fetchone()[0] or 0

    preview_articles = conn.execute("SELECT count(*) FROM archive WHERE record_type = 'article' AND status = 'preview'").fetchone()[0] or 0

    return {
        "enrichment": {
            "done": enrich_done,
            "pending": enrich_pending,
            "terminal": enrich_terminal,
        },
        "threads_expanded": threads_expanded,
        "preview_articles": preview_articles,
    }

@router.get("/tags")
def get_stats_tags(store: ArchiveStore = Depends(get_store)) -> Dict[str, Any]:
    """AI Tagging & Metadata Metrics."""
    conn = store.conn

    eligible_tweets = conn.execute(
        "SELECT count(DISTINCT tweet_id) FROM archive WHERE record_type = 'media'"
    ).fetchone()[0] or 0

    tagged_tweets = conn.execute(
        "SELECT count(DISTINCT tweet_id) FROM archive WHERE record_type = 'media_tag'"
    ).fetchone()[0] or 0

    unique_tags = conn.execute(
        """
        SELECT count(DISTINCT LOWER(t.value)) 
        FROM archive a, json_each(a.raw_json, '$.tags') as t 
        WHERE a.record_type = 'media_tag'
        """
    ).fetchone()[0] or 0

    total_tag_instances = conn.execute(
        """
        SELECT count(t.value) 
        FROM archive a, json_each(a.raw_json, '$.tags') as t 
        WHERE a.record_type = 'media_tag'
        """
    ).fetchone()[0] or 0

    top_tags_rows = conn.execute(
        """
        SELECT LOWER(t.value) as tag, count(*) as count 
        FROM archive a, json_each(a.raw_json, '$.tags') as t 
        WHERE a.record_type = 'media_tag'
        GROUP BY LOWER(t.value) 
        ORDER BY count DESC 
        LIMIT 20
        """
    ).fetchall()

    top_tags = [{"tag": r[0], "count": r[1]} for r in top_tags_rows]

    coverage_pct = round((tagged_tweets / eligible_tweets * 100), 1) if eligible_tweets > 0 else 0.0
    avg_tags_per_tweet = round((total_tag_instances / tagged_tweets), 1) if tagged_tweets > 0 else 0.0
    untagged_eligible = max(0, eligible_tweets - tagged_tweets)

    return {
        "eligible_tweets": eligible_tweets,
        "tagged_tweets": tagged_tweets,
        "untagged_eligible": untagged_eligible,
        "unique_tags": unique_tags,
        "total_tag_instances": total_tag_instances,
        "coverage_pct": coverage_pct,
        "avg_tags_per_tweet": avg_tags_per_tweet,
        "top_tags": top_tags,
    }
