"""Shared archive statistics report used by the CLI and Web UI."""

from tweetxvault.stats.models import (
    StatItem,
    StatsReport,
    StatsSection,
    StatsSectionSpec,
    TableColumn,
)
from tweetxvault.stats.service import (
    STATS_SECTION_SPECS,
    build_stats_report,
    build_stats_section,
    format_bytes,
    get_enrichment_incomplete_count,
)

__all__ = [
    "STATS_SECTION_SPECS",
    "StatItem",
    "StatsReport",
    "StatsSection",
    "StatsSectionSpec",
    "TableColumn",
    "build_stats_report",
    "build_stats_section",
    "format_bytes",
    "get_enrichment_incomplete_count",
]
