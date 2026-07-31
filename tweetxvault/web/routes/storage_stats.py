"""Storage statistics endpoints."""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from tweetxvault.storage import ArchiveStore
from tweetxvault.web.deps import get_store, verify_credentials

router = APIRouter(
    prefix="/api/storage",
    tags=["storage"],
    dependencies=[Depends(verify_credentials)],
)


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024.0 or unit == "PB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.2f} PB"


@router.get("/breakdown")
def get_storage_breakdown(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return database, media, and index storage usage."""
    segments: list[dict[str, Any]] = []

    def get_db_stats(query_count: str, query_bytes: str) -> tuple[int, int]:
        count = store.conn.execute(query_count).fetchone()[0] or 0
        bytes_value = store.conn.execute(query_bytes).fetchone()[0] or 0
        return count, bytes_value

    core_ids_sql = """
        SELECT tweet_id FROM archive WHERE record_type = 'tweet'
        UNION
        SELECT target_tweet_id AS tweet_id
        FROM archive
        WHERE record_type = 'tweet_relation'
          AND relation_type IN ('quote_of', 'quoted', 'links_to_status')
          AND tweet_id IN (
              SELECT tweet_id FROM archive WHERE record_type = 'tweet'
          )
    """

    core_media_rows = store.conn.execute(
        f"""
        SELECT local_path, thumbnail_local_path, media_type
        FROM archive
        WHERE record_type = 'media'
          AND tweet_id IN ({core_ids_sql})
        """
    ).fetchall()
    context_media_rows = store.conn.execute(
        f"""
        SELECT local_path, thumbnail_local_path, media_type
        FROM archive
        WHERE record_type = 'media'
          AND tweet_id NOT IN ({core_ids_sql})
        """
    ).fetchall()

    core_file_set: set[str] = set()
    for row in core_media_rows:
        if row[0]:
            core_file_set.add(Path(row[0]).name)
        if row[1]:
            core_file_set.add(Path(row[1]).name)

    media_dir = store.db_path.parent / "media"
    avatars_dir = media_dir / "avatars"

    core_photos = 0
    core_videos = 0
    core_bytes = 0
    context_photos = 0
    context_videos = 0
    context_bytes = 0
    avatars_count = 0
    avatars_file_bytes = 0

    if media_dir.exists():
        for path in media_dir.rglob("*"):
            if not path.is_file():
                continue
            if avatars_dir in path.parents or path.parent == avatars_dir:
                avatars_count += 1
                avatars_file_bytes += path.stat().st_size
                continue

            extension = path.suffix.lower()
            size = path.stat().st_size
            is_video = extension in {".mp4", ".mov", ".m3u8", ".ts", ".webm"}
            if path.name in core_file_set:
                core_bytes += size
                if is_video:
                    core_videos += 1
                else:
                    core_photos += 1
            else:
                context_bytes += size
                if is_video:
                    context_videos += 1
                else:
                    context_photos += 1

    # Preserve media counts when local files are absent or zero-byte.
    if core_photos == 0 and core_videos == 0 and core_media_rows:
        for row in core_media_rows:
            if row[2] in {"video", "animated_gif"}:
                core_videos += 1
            else:
                core_photos += 1

    if context_photos == 0 and context_videos == 0 and context_media_rows:
        for row in context_media_rows:
            if row[2] in {"video", "animated_gif"}:
                context_videos += 1
            else:
                context_photos += 1

    core_total_files = core_photos + core_videos
    segments.append(
        {
            "id": "core_media",
            "group": "media",
            "name": "Core Media (Bookmarked & Quoted)",
            "bytes": core_bytes,
            "count": core_total_files,
            "unit": "files",
            "formatted_count": (f"{core_photos:,} photos · {core_videos:,} videos/gifs"),
            "description": (
                "Locally downloaded high-resolution images, videos, and GIFs "
                "attached directly to your primary bookmarked/liked posts and "
                "their quoted posts."
            ),
        }
    )

    context_total_files = context_photos + context_videos
    segments.append(
        {
            "id": "context_media",
            "group": "media",
            "name": "Context Media (Thread Extensions)",
            "bytes": context_bytes,
            "count": context_total_files,
            "unit": "files",
            "formatted_count": (f"{context_photos:,} photos · {context_videos:,} videos/gifs"),
            "description": (
                "Images and video files attached to surrounding conversation "
                "threads, replies, and parent context chains fetched during "
                "timeline thread expansion."
            ),
        }
    )

    core_db_count, core_db_bytes = get_db_stats(
        "SELECT count(*) FROM archive WHERE record_type = 'tweet'",
        """
        SELECT sum(ifnull(length(raw_json), 0) + ifnull(length(text), 0))
        FROM archive
        WHERE record_type = 'tweet'
        """,
    )
    segments.append(
        {
            "id": "core_db",
            "group": "database",
            "name": "Core Tweet Database",
            "bytes": core_db_bytes,
            "count": core_db_count,
            "unit": "tweets",
            "description": (
                "Contains raw JSON payloads, canonical tweet text, timestamps, "
                "author metadata, and engagement statistics for all saved "
                "tweets in your primary archive."
            ),
        }
    )

    threads_count, threads_bytes = get_db_stats(
        "SELECT count(*) FROM archive WHERE record_type = 'tweet_object'",
        """
        SELECT sum(length(raw_json))
        FROM archive
        WHERE record_type = 'tweet_object'
        """,
    )
    segments.append(
        {
            "id": "threads",
            "group": "database",
            "name": "Thread Extensions & Context",
            "bytes": threads_bytes,
            "count": threads_count,
            "unit": "objects",
            "description": (
                "Stores parent context chains, surrounding conversation "
                "threads, and direct replies fetched during timeline expansion "
                "to present complete conversation views for bookmarked posts."
            ),
        }
    )

    articles_count, articles_bytes = get_db_stats(
        "SELECT count(*) FROM archive WHERE record_type = 'article'",
        """
        SELECT sum(
            ifnull(length(content_text), 0) + ifnull(length(summary_text), 0)
        )
        FROM archive
        WHERE record_type = 'article'
        """,
    )
    segments.append(
        {
            "id": "articles",
            "group": "database",
            "name": "Article Content & Cards",
            "bytes": articles_bytes,
            "count": articles_count,
            "unit": "articles",
            "description": (
                "Stores extracted full-text articles, linked web URL summaries, "
                "and unfurled rich media cards attached to archived posts."
            ),
        }
    )

    tags_count, tags_bytes = get_db_stats(
        "SELECT count(*) FROM archive WHERE record_type = 'media_tag'",
        """
        SELECT sum(length(raw_json))
        FROM archive
        WHERE record_type = 'media_tag'
        """,
    )
    segments.append(
        {
            "id": "tags",
            "group": "database",
            "name": "Tagging & Topic Metadata",
            "bytes": tags_bytes,
            "count": tags_count,
            "unit": "tags",
            "description": (
                "Holds topic tags, categories, and structured metadata assigned "
                "to archived posts to enable filtering and automatic topic "
                "organization."
            ),
        }
    )

    profiles_count, profiles_bytes = get_db_stats(
        """
        SELECT count(DISTINCT author_id)
        FROM archive
        WHERE author_id IS NOT NULL AND author_id != ''
        """,
        """
        SELECT sum(
            ifnull(length(author_username), 0)
            + ifnull(length(author_display_name), 0)
        )
        FROM archive
        WHERE record_type = 'tweet'
        """,
    )
    segments.append(
        {
            "id": "user_profiles",
            "group": "database",
            "name": "User Profiles & Handles",
            "bytes": profiles_bytes,
            "count": profiles_count,
            "unit": "profiles",
            "description": (
                "Author profile metadata, display names, screen names, and "
                "handle attributes stored for all account authors in your "
                "archive."
            ),
        }
    )

    segments.append(
        {
            "id": "avatars",
            "group": "media",
            "name": "Avatar Image Cache",
            "bytes": avatars_file_bytes,
            "count": avatars_count,
            "unit": "avatars",
            "description": (
                "Locally cached user profile picture image files downloaded to "
                "render author avatars across the interface without external "
                "network requests."
            ),
        }
    )

    total_db_bytes = store.db_path.stat().st_size
    wal_path = store.db_path.with_name(store.db_path.name + "-wal")
    if wal_path.exists():
        total_db_bytes += wal_path.stat().st_size
    shm_path = store.db_path.with_name(store.db_path.name + "-shm")
    if shm_path.exists():
        total_db_bytes += shm_path.stat().st_size

    sum_db_segments = core_db_bytes + threads_bytes + articles_bytes + tags_bytes + profiles_bytes
    search_index_bytes = max(0, total_db_bytes - sum_db_segments)
    segments.append(
        {
            "id": "search_index",
            "group": "database",
            "name": "Search Index & DB Overhead",
            "bytes": search_index_bytes,
            "count": 1,
            "unit": "index",
            "description": (
                "SQLite Full-Text Search (FTS5) indexes, primary key B-trees, "
                "scalar indexes, and Write-Ahead Logging (WAL) files that "
                "enable fast search across millions of records."
            ),
        }
    )

    total_bytes = total_db_bytes + core_bytes + context_bytes + avatars_file_bytes
    segments = [segment for segment in segments if segment["bytes"] > 0 or segment["count"] > 0]
    segments.sort(key=lambda segment: segment["bytes"], reverse=True)

    for segment in segments:
        segment["formatted_size"] = format_bytes(segment["bytes"])
        segment["percent"] = (
            round(segment["bytes"] / total_bytes * 100, 2) if total_bytes > 0 else 0.0
        )

    media_bytes = core_bytes + context_bytes + avatars_file_bytes
    total_photos = core_photos + context_photos
    total_videos = core_videos + context_videos
    simplified_segments: list[dict[str, Any]] = []

    if total_db_bytes > 0:
        simplified_segments.append(
            {
                "id": "database",
                "name": "Database & Indexes",
                "bytes": total_db_bytes,
                "count": core_db_count,
                "unit": "records",
                "formatted_count": "SQLite Archive & Full-Text Search Indexes",
                "formatted_size": format_bytes(total_db_bytes),
                "percent": (
                    round(total_db_bytes / total_bytes * 100, 2) if total_bytes > 0 else 0.0
                ),
                "description": (
                    "Primary SQLite storage file containing raw JSON, tweet "
                    "records, threads, articles, full-text search indexes, and "
                    "WAL logging."
                ),
            }
        )

    media_count = core_total_files + context_total_files + avatars_count
    if media_bytes > 0 or media_count > 0:
        simplified_segments.append(
            {
                "id": "media",
                "name": "Media Files & Avatars",
                "bytes": media_bytes,
                "count": media_count,
                "unit": "files",
                "formatted_count": (
                    f"{total_photos:,} photos · {total_videos:,} videos · {avatars_count:,} avatars"
                ),
                "formatted_size": format_bytes(media_bytes),
                "percent": (round(media_bytes / total_bytes * 100, 2) if total_bytes > 0 else 0.0),
                "description": (
                    "Locally saved media files including high-resolution tweet "
                    "images, videos, animated GIFs, thread attachments, and "
                    "cached profile avatars."
                ),
            }
        )

    simplified_segments.sort(key=lambda segment: segment["bytes"], reverse=True)

    return {
        "total_bytes": total_bytes,
        "formatted_total": format_bytes(total_bytes),
        "segments": segments,
        "simplified_segments": simplified_segments,
    }
