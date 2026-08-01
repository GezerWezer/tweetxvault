from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tweetxvault.config import load_config
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
    if progress is not None:
        progress.write(message)
    else:
        print(message)


def _advance_progress(progress: Any | None, total_rows: int, batch_size: int) -> None:
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
                print("Too many consecutive failures. Aborting migration.")
                break
        else:
            consecutive_failures = 0
            result.migrated_rows += rows_in_chunk

        offset += batch_size
        result.final_offset = offset
        _advance_progress(progress, total_rows, batch_size)

    return result


def _rebuild_fts(config: Any, paths: Any) -> bool:
    print("Rebuilding Full-Text Search index (this may take a few moments)...")
    try:
        store = open_archive_store(paths, create=True, config=config)
    except Exception as error:
        print(f"Warning: Failed to open SQLite store for FTS rebuild: {error}")
        return False
    if store is None:
        print("Warning: Failed to open SQLite store for FTS rebuild.")
        return False
    try:
        store.rebuild_search_index()
        return True
    except Exception as error:
        print(f"Warning: Failed to rebuild FTS index: {error}")
        return False
    finally:
        store.close()


def run_migration(*, batch_size: int = DEFAULT_BATCH_SIZE) -> MigrationResult:
    config, paths = load_config()
    lance_path = paths.data_dir / "archive.lancedb"
    if not lance_path.exists():
        print(f"No old LanceDB archive found at {lance_path}.")
        return MigrationResult(status="source_missing")

    lancedb = _import_lancedb()
    if lancedb is None:
        print("Error: lancedb and pyarrow are required to run the migration.")
        print("Please reinstall tweetxvault with its migration dependencies.")
        return MigrationResult(status="dependency_missing")

    print(f"Reading LanceDB at {lance_path}...")
    try:
        legacy_database = lancedb.connect(lance_path)
        table = legacy_database.open_table("archive")
        total_rows = int(table.count_rows())
    except Exception as error:
        print(f"LanceDB archive table not found or unreadable: {error}")
        return MigrationResult(status="table_missing")

    print(f"Found {total_rows} rows to migrate.")
    database_path = paths.database_path
    print(f"Inserting into native SQLite database at {database_path}...")

    try:
        store = open_archive_store(paths, create=True, config=config)
    except Exception as error:
        print(f"Failed to initialize SQLite store: {error}")
        return MigrationResult(status="destination_failed", total_rows=total_rows)
    if store is None:
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

    result.fts_rebuilt = _rebuild_fts(config, paths)
    if result.status == "aborted":
        print("Migration stopped before all readable chunks were processed.")
        return result

    print("Migration complete! You can now run `tweetxvault stats`.")
    print(
        "If it works correctly, you may safely backup and delete the original "
        f"`{lance_path}` directory."
    )
    return result


if __name__ == "__main__":
    run_migration()
