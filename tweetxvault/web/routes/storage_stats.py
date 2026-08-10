"""Compatibility endpoint for the shared storage statistics section."""

from typing import Any

from fastapi import APIRouter, Depends

from tweetxvault.stats import build_stats_section
from tweetxvault.stats import format_bytes as _format_bytes
from tweetxvault.storage import ArchiveStore
from tweetxvault.web.deps import get_store, verify_credentials

router = APIRouter(
    prefix="/api/storage",
    tags=["storage"],
    dependencies=[Depends(verify_credentials)],
)


def format_bytes(size: float) -> str:
    """Retain the existing formatting helper for API callers and tests."""
    return _format_bytes(size)


@router.get("/breakdown")
def get_storage_breakdown(
    store: ArchiveStore = Depends(get_store),  # noqa: B008 - FastAPI dependency
) -> dict[str, Any]:
    """Return the legacy storage payload from the shared report collector."""
    return build_stats_section(store, "storage").data
