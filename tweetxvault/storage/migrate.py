import sys
from pathlib import Path
try:
    import lancedb
except ImportError:
    print("Error: lancedb package is required to run the migration.")
    print("Please reinstall tweetxvault with the lancedb dependency, or run: pip install lancedb pyarrow")
    sys.exit(1)

from tweetxvault.config import load_config
from tweetxvault.storage import open_archive_store

def run_migration() -> None:
    config, paths = load_config()
    
    # Old path
    lance_path = paths.data_dir / "archive.lancedb"
    if not lance_path.exists():
        print(f"No old LanceDB archive found at {lance_path}.")
        return
        
    print(f"Reading LanceDB at {lance_path}...")
    ldb = lancedb.connect(lance_path)
    try:
        table = ldb.open_table("archive")
    except Exception as e:
        print("LanceDB archive table not found.", e)
        return
        
    total_rows = table.count_rows()
    print(f"Found {total_rows} rows to migrate.")
    
    # Initialize SQLite database (which will be at paths.database_path -> archive.db)
    new_db_path = paths.database_path
    print(f"Inserting into native SQLite database at {new_db_path}...")
    
    store = open_archive_store(paths, create=True)
    if not store:
        print("Failed to open SQLite store.")
        return
        
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
        "warnings_json", "counts_json", "last_head_tweet_id", "backfill_cursor", "backfill_incomplete",
        "updated_at", "key", "value"
    ]
    
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT OR REPLACE INTO archive ({col_names}) VALUES ({placeholders})"
    
    # Attempt to import tqdm for progress bar
    try:
        from tqdm import tqdm
        pbar = tqdm(total=total_rows, desc="Migrating to SQLite", unit="rows")
    except ImportError:
        pbar = None

    # Process in batches using offset and limit to prevent OOM
    batch_size = 1000
    offset = 0
    
    while True:
        rows = table.search().limit(batch_size).offset(offset).to_list()
        if not rows:
            break
            
        params = []
        for record in rows:
            row = []
            for col in cols:
                row.append(record.get(col))
            params.append(row)
            
        with store.conn:
            store.conn.executemany(sql, params)
            
        if pbar:
            pbar.update(len(rows))
            
        offset += len(rows)
            
    if pbar:
        pbar.close()
        
    store.close()
    print("Migration complete! You can now run `tweetxvault stats`.")
    print(f"If it works correctly, you may safely backup and delete the original `{lance_path}` directory.")

if __name__ == "__main__":
    run_migration()
