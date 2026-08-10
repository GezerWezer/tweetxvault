"""Archive statistics endpoints backed by the shared report registry."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends

from tweetxvault.stats import (
    build_stats_report,
    build_stats_section,
    get_enrichment_incomplete_count,
)
from tweetxvault.storage import ArchiveStore
from tweetxvault.web.deps import get_store, verify_credentials

router = APIRouter(
    prefix="/api/stats",
    tags=["stats"],
    dependencies=[Depends(verify_credentials)],
)


def _format_ts(ts: int | float | str | None) -> str | None:
    if not ts:
        return None
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            from tweetxvault.storage.backend import _parse_created_at

            parsed = _parse_created_at(ts)
            if parsed is None:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = datetime.fromtimestamp(ts, tz=UTC)
    return parsed.astimezone(UTC).strftime("%b %d, %Y")


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


@router.get("/report")
def get_stats_report(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return the complete ordered report consumed by both interfaces."""
    return build_stats_report(store).as_dict()


@router.get("/enrichment-incomplete")
def get_enrichment_incomplete(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, int]:
    """Return the inexpensive status-banner count."""
    return {"incomplete": get_enrichment_incomplete_count(store)}


@router.get("/summary")
def get_stats_summary(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return the legacy overview payload."""
    data = dict(build_stats_section(store, "overview").data)
    for key in ("oldest_post", "newest_post", "latest_sync"):
        data[key] = _format_ts(data[key])
    data.pop("collection_memberships", None)
    data.pop("raw_captures", None)
    data.pop("latest_capture", None)
    return data


@router.get("/collections")
def get_stats_collections(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> list[dict[str, Any]]:
    """Return the legacy collections payload."""
    rows = build_stats_section(store, "collections").rows
    return [
        {
            **row,
            "oldest": _format_stats_timestamp(row["oldest"]),
            "newest": _format_stats_timestamp(row["newest"]),
            "last_synced": _format_stats_timestamp(row["last_synced"]),
        }
        for row in rows
    ]


@router.get("/health")
def get_stats_health(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return the legacy archive-status payload."""
    data = dict(build_stats_section(store, "archive_status").data)
    data.pop("missing_tweet_objects", None)
    data.pop("pending_thread_memberships", None)
    data.pop("pending_linked_statuses", None)
    data["enrichment"] = dict(data["enrichment"])
    data["enrichment"].pop("transient_due", None)
    data["enrichment"].pop("transient_delayed", None)
    return data


@router.get("/tags")
def get_stats_tags(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return the legacy tagging payload."""
    return build_stats_section(store, "tagging").data
