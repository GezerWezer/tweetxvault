"""User-facing reminders for incomplete archive maintenance."""

from __future__ import annotations

from rich.console import Console

from tweetxvault.storage import ArchiveStore


def print_archive_migration_report(console: Console, store: ArchiveStore) -> bool:
    report = getattr(store, "migration_report", None)
    if report is None:
        return False
    console.print(
        f"Archive schema migrated from v{report.from_version} to v{report.to_version}.",
        highlight=False,
    )
    if report.backup_path is not None:
        console.print(f"Validated backup: {report.backup_path}", highlight=False)
    if report.search_index_rebuilt:
        console.print(
            "Rebuilt the derived full-text index with searchable posts only; "
            "canonical archive rows were unchanged.",
            highlight=False,
        )
        return True
    console.print(
        "Legacy unavailable-row repair: "
        f"{report.legacy_terminal_rows_scanned:,} scanned, "
        f"{report.content_rows_repaired:,} enriched, "
        f"{report.author_rows_repaired:,} authors recovered, "
        f"{report.rows_still_missing_author:,} still missing authors.",
        highlight=False,
    )
    if report.rows_without_richer_source:
        console.print(
            "Run `tweetxvault repair legacy-tombstones --scan-captures` for an explicit "
            "deeper repair pass.",
            highlight=False,
        )
    return True


def print_pending_archive_enrichment_reminder(
    console: Console,
    store: ArchiveStore,
) -> bool:
    incomplete = store.count_incomplete_initial_enrichment()
    if incomplete <= 0:
        return False
    remainder = "tweet remains" if incomplete == 1 else "tweets remain"
    console.print(f"Archive enrichment is incomplete: {incomplete:,} {remainder}.", highlight=False)
    console.print("Run `tweetxvault import enrich` to continue.", highlight=False)
    return True
