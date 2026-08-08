from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from tweetxvault.config import load_config
from tweetxvault.pipeline import current_pipeline
from tweetxvault.storage import open_archive_store

WORKER_END_OF_TABLE = 42
DEFAULT_BATCH_SIZE = 5000
MAX_CONSECUTIVE_FAILURES = 100

WORKER_CODE = """
import sys
import lancedb
import sqlite3

def worker():
    lance_path = sys.argv[1]
    db_path = sys.argv[2]
    batch_size = int(sys.argv[3])
    offset = int(sys.argv[4])
    
    ldb = lancedb.connect(lance_path)
    table = ldb.open_table("archive")
    rows = table.search().limit(batch_size).offset(offset).to_list()
    
    if not rows:
        sys.exit(42)
        
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    
    cols = [
        "row_key", "record_type", "tweet_id", "collection_type", "folder_id", "sort_index",
        "operation", "cursor_in", "cursor_out", "captured_at", "http_status", "source",
        "text", "author_id", "author_username", "author_display_name", "created_at",
        "deleted_at", "conversation_id", "lang", "note_tweet_text", "enrichment_state",
        "enrichment_checked_at", "enrichment_http_status", "enrichment_reason", "raw_json",
        "first_seen_at", "last_seen_at", "added_at", "synced_at", "relation_type",
        "target_tweet_id", "position", "media_key", "media_type", "media_url", "thumbnail_url",
        "width", "height", "duration_millis", "variants_json", "download_state", "local_path",
        "provenance_source", "sha256", "byte_size", "content_type", "thumbnail_local_path",
        "thumbnail_sha256", "thumbnail_byte_size", "thumbnail_content_type", "downloaded_at",
        "download_error", "url_hash", "url", "expanded_url", "final_url", "canonical_url",
        "display_url", "url_host", "description", "site_name", "unfurl_state", "last_fetched_at",
        "article_id", "title", "summary_text", "content_text", "published_at", "status",
        "archive_digest", "archive_generation_date", "import_started_at", "import_completed_at",
        "warnings_json", "counts_json", "last_head_tweet_id", "backfill_cursor",
        "backfill_incomplete",
        "updated_at", "key", "value"
    ]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    # A rerun must never overwrite newer data already present in SQLite.
    sql = f"INSERT OR IGNORE INTO archive ({col_names}) VALUES ({placeholders})"
    
    params = []
    for record in rows:
        row = []
        for col in cols:
            row.append(record.get(col))
        params.append(row)
        
    with conn:
        conn.executemany(sql, params)
    conn.close()

    sys.exit(0)

if __name__ == "__main__":
    worker()
"""


@dataclass(slots=True)
class MigrationResult:
    status: str
    total_rows: int = 0
    migrated_rows: int = 0
    skipped_rows: int = 0
    worker_calls: int = 0
    final_offset: int = 0
    fts_rebuilt: bool = False


def _import_lancedb() -> Any | None:
    """Load the legacy dependency only when the migration command is used."""
    try:
        return importlib.import_module("lancedb")
    except ImportError:
        return None


def _create_progress(total_rows: int) -> Any | None:
    if current_pipeline() is not None:
        return None
    try:
        from tqdm import tqdm
    except ImportError:
        return None
    return tqdm(total=total_rows, desc="Migrating to SQLite", unit="rows")


def _run_worker(
    lance_path: Path,
    database_path: Path,
    batch_size: int,
    offset: int,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        sys.executable,
        "-c",
        WORKER_CODE,
        str(lance_path),
        str(database_path),
        str(batch_size),
        str(offset),
    ]
    return subprocess.run(command, capture_output=True, check=False)


def _progress_warning(progress: Any | None, message: str) -> None:
    pipeline = current_pipeline()
    if pipeline is not None:
        pipeline.issue(message.removeprefix("Warning: "), dedupe_key="migration:worker-warning")
        return
    if progress is not None:
        progress.write(message)
    else:
        print(message)


def _advance_progress(progress: Any | None, total_rows: int, batch_size: int) -> None:
    pipeline = current_pipeline()
    if pipeline is not None and pipeline.has_step("migration-copy"):
        step = pipeline.active_step
        completed = min(
            (step.completed if step is not None and step.key == "migration-copy" else 0)
            + batch_size,
            total_rows,
        )
        pipeline.update_step(
            "migration-copy",
            completed=completed,
            counters=f"{completed:,}/{total_rows:,} legacy rows examined",
        )
    if progress is None:
        return
    remaining = max(0, total_rows - int(progress.n))
    if remaining:
        progress.update(min(batch_size, remaining))


def _copy_batches(
    *,
    lance_path: Path,
    database_path: Path,
    total_rows: int,
    batch_size: int,
    progress: Any | None,
) -> MigrationResult:
    result = MigrationResult(status="complete", total_rows=total_rows)
    offset = 0
    consecutive_failures = 0

    while True:
        try:
            worker_result = _run_worker(
                lance_path,
                database_path,
                batch_size,
                offset,
            )
            return_code = worker_result.returncode
        except Exception as error:
            return_code = 1
            _progress_warning(
                progress,
                f"Warning: Migration worker failed at offset {offset}: {error}",
            )

        result.worker_calls += 1
        if return_code == WORKER_END_OF_TABLE:
            break

        rows_in_chunk = min(batch_size, max(0, total_rows - offset))
        if return_code != 0:
            consecutive_failures += 1
            result.skipped_rows += rows_in_chunk
            _progress_warning(
                progress,
                "Warning: Corrupted chunk detected at "
                f"offset {offset}. Skipping {batch_size} rows to recover data...",
            )
            if consecutive_failures > MAX_CONSECUTIVE_FAILURES:
                result.status = "aborted"
                pipeline = current_pipeline()
                if pipeline is not None:
                    pipeline.issue(
                        "Too many consecutive migration worker failures; aborting.",
                        level="error",
                        dedupe_key="migration:aborted",
                    )
                else:
                    print("Too many consecutive failures. Aborting migration.")
                break
        else:
            consecutive_failures = 0
            result.migrated_rows += rows_in_chunk

        offset += batch_size
        result.final_offset = offset
        _advance_progress(progress, total_rows, batch_size)

    return result


def _rebuild_fts(config: Any, paths: Any, *, console: Console | None = None) -> bool:
    pipeline = current_pipeline()
    if pipeline is not None:
        pipeline.status("migration-index", "Rebuilding the SQLite full-text search index")
    else:
        print("Rebuilding Full-Text Search index (this may take a few moments)...")
    try:
        store = open_archive_store(paths, create=True, config=config)
    except Exception as error:
        message = f"Failed to open SQLite store for FTS rebuild: {error}"
        if pipeline is not None:
            pipeline.issue(message, level="error", dedupe_key="migration:index-open")
        elif console is not None:
            console.print(f"Warning: {message}")
        else:
            print(f"Warning: {message}")
        return False
    if store is None:
        message = "Failed to open SQLite store for FTS rebuild."
        if pipeline is not None:
            pipeline.issue(message, level="error", dedupe_key="migration:index-open")
        else:
            print(f"Warning: {message}")
        return False
    try:
        store.rebuild_search_index()
        return True
    except Exception as error:
        message = f"Failed to rebuild FTS index: {error}"
        if pipeline is not None:
            pipeline.issue(message, level="error", dedupe_key="migration:index-rebuild")
        else:
            print(f"Warning: {message}")
        return False
    finally:
        store.close()


def run_migration(
    *, batch_size: int = DEFAULT_BATCH_SIZE, console: Console | None = None
) -> MigrationResult:
    config, paths = load_config()
    pipeline = current_pipeline()
    if pipeline is not None:
        pipeline.add_step(
            "migration-inspect",
            "Inspect source",
            total=1,
            unit="archive",
            detail="legacy LanceDB archive table and destination validation",
            show_rate=False,
            show_eta=False,
        )
        pipeline.add_step(
            "migration-copy",
            "Copy rows",
            total=1,
            unit="rows",
            detail=f"isolated worker chunks of {batch_size:,} rows · existing SQLite rows kept",
            rate_unit="rows/s",
        )
        pipeline.add_step(
            "migration-index",
            "Search index",
            total=1,
            unit="index",
            detail="rebuild SQLite FTS after imported rows are durable",
            show_rate=False,
            show_eta=False,
        )
        pipeline.start_step(
            "migration-inspect",
            activity="Locating the legacy LanceDB archive",
        )
    lance_path = paths.data_dir / "archive.lancedb"
    if not lance_path.exists():
        if pipeline is not None:
            pipeline.skip_step("migration-inspect", "no legacy LanceDB archive being present")
            pipeline.skip_step("migration-copy", "no legacy source archive being present")
            pipeline.skip_step("migration-index", "no rows being migrated")
            pipeline.final_note(f"No legacy LanceDB archive found at {lance_path}.")
        else:
            print(f"No old LanceDB archive found at {lance_path}.")
        return MigrationResult(status="source_missing")

    lancedb = _import_lancedb()
    if lancedb is None:
        if pipeline is not None:
            message = "lancedb and pyarrow are required to read the legacy archive"
            pipeline.fail_step("migration-inspect", message)
            pipeline.issue(message, level="error", dedupe_key="migration:dependencies")
            pipeline.skip_step("migration-copy", "legacy migration dependencies being unavailable")
            pipeline.skip_step("migration-index", "no rows being migrated")
        else:
            print("Error: lancedb and pyarrow are required to run the migration.")
            print("Please reinstall tweetxvault with its migration dependencies.")
        return MigrationResult(status="dependency_missing")

    if pipeline is not None:
        pipeline.status("migration-inspect", f"Reading the archive table at {lance_path}")
    else:
        print(f"Reading LanceDB at {lance_path}...")
    try:
        legacy_database = lancedb.connect(lance_path)
        table = legacy_database.open_table("archive")
        total_rows = int(table.count_rows())
    except Exception as error:
        if pipeline is not None:
            message = f"Legacy archive table is unreadable: {error}"
            pipeline.fail_step("migration-inspect", message)
            pipeline.issue(message, level="error", dedupe_key="migration:source-table")
            pipeline.skip_step("migration-copy", "the legacy archive table being unreadable")
            pipeline.skip_step("migration-index", "no rows being migrated")
        else:
            print(f"LanceDB archive table not found or unreadable: {error}")
        return MigrationResult(status="table_missing")

    if pipeline is not None:
        pipeline.complete_step("migration-inspect", f"{total_rows:,} legacy rows ready to migrate")
    else:
        print(f"Found {total_rows} rows to migrate.")
    database_path = paths.database_path
    if pipeline is not None:
        pipeline.add_step(
            "migration-copy",
            "Copy rows",
            total=max(total_rows, 1),
            unit="rows",
            detail=f"{lance_path.name} → {database_path.name} · {batch_size:,}-row worker chunks",
            rate_unit="rows/s",
        )
        pipeline.start_step(
            "migration-copy",
            activity="Initializing the native SQLite destination",
            counters=f"0/{total_rows:,} legacy rows examined",
        )
    else:
        print(f"Inserting into native SQLite database at {database_path}...")

    try:
        store = open_archive_store(paths, create=True, config=config)
    except Exception as error:
        if pipeline is not None:
            pipeline.fail_step("migration-copy", f"Failed to initialize SQLite: {error}")
            pipeline.skip_step("migration-index", "the SQLite destination failing to initialize")
        else:
            print(f"Failed to initialize SQLite store: {error}")
        return MigrationResult(status="destination_failed", total_rows=total_rows)
    if store is None:
        if pipeline is not None:
            pipeline.fail_step("migration-copy", "Failed to open the SQLite destination")
            pipeline.skip_step("migration-index", "the SQLite destination failing to initialize")
        else:
            print("Failed to open SQLite store.")
        return MigrationResult(status="destination_failed", total_rows=total_rows)
    store.close()

    progress = _create_progress(total_rows)
    try:
        result = _copy_batches(
            lance_path=lance_path,
            database_path=database_path,
            total_rows=total_rows,
            batch_size=batch_size,
            progress=progress,
        )
    finally:
        if progress is not None:
            progress.close()

    if result.status == "aborted":
        if pipeline is not None:
            pipeline.fail_step(
                "migration-copy",
                f"stopped after {result.final_offset:,} rows due to repeated worker failures",
            )
            pipeline.skip_step("migration-index", "row migration aborting before completion")
            pipeline.final_note("Migration stopped before all readable chunks were processed.")
        else:
            print("Migration stopped before all readable chunks were processed.")
        return result

    if pipeline is not None:
        pipeline.complete_step(
            "migration-copy",
            f"{result.migrated_rows:,} migrated · {result.skipped_rows:,} skipped",
        )
        pipeline.start_step(
            "migration-index",
            activity="Rebuilding the SQLite full-text search index",
        )
    result.fts_rebuilt = _rebuild_fts(config, paths, console=console)
    if pipeline is not None:
        if result.fts_rebuilt:
            pipeline.complete_step("migration-index", "SQLite full-text search index rebuilt")
        else:
            pipeline.fail_step("migration-index", "SQLite full-text search index rebuild failed")
        pipeline.final_note(
            f"Migration complete: {result.migrated_rows:,} rows copied into {database_path}."
        )
        return result

    print("Migration complete! You can now run `tweetxvault stats`.")
    print(
        "If it works correctly, you may safely backup and delete the original "
        f"`{lance_path}` directory."
    )
    return result


if __name__ == "__main__":
    migration_console = Console(stderr=True)
    from tweetxvault.pipeline import PipelineReporter

    with PipelineReporter(migration_console, "tweetxvault migrate"):
        run_migration(console=migration_console)
