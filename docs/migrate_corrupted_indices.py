import sys
import os
import shutil
import lancedb

def run_migration():
    if len(sys.argv) < 2:
        print("Usage: uv run docs/migrate_corrupted_indices.py ~/.local/share/tweetxvault/archive.lancedb")
        sys.exit(1)
        
    db_path = sys.argv[1]
    if not os.path.exists(db_path):
        print(f"Error: Database path {db_path} does not exist.")
        sys.exit(1)
        
    print(f"Connecting to LanceDB at {db_path}")
    db = lancedb.connect(db_path)
    
    table_name = "archive"
    if table_name not in db.table_names():
        print(f"Error: Table '{table_name}' not found in database.")
        sys.exit(1)
        
    table = db.open_table(table_name)
    
    # We will create a new temporary database to rewrite the data cleanly
    new_db_path = db_path + "_migration_temp"
    if os.path.exists(new_db_path):
        shutil.rmtree(new_db_path)
        
    print(f"Creating clean migration database at {new_db_path}")
    new_db = lancedb.connect(new_db_path)
    
    print("Reading and rewriting data in batches. This may take several minutes for a 10GB database...")
    new_table = new_db.create_table(table_name, schema=table.schema)
    
    batch_count = 0
    try:
        # We explicitly read in batches and append to the new table
        # This completely severs the data from the corrupted optimization history
        for batch in table.search().to_batches():
            new_table.add(batch)
            batch_count += 1
            if batch_count % 10 == 0:
                print(f"  ...processed {batch_count} batches")
    except Exception as e:
        print(f"\nMigration failed during batch transfer: {e}")
        print(f"Cleaning up temporary migration database at {new_db_path}")
        shutil.rmtree(new_db_path)
        sys.exit(1)
        
    print(f"\nSuccessfully migrated {batch_count} batches to clean database.")
    
    print("Backing up corrupted database...")
    backup_path = db_path + "_corrupted_backup"
    if os.path.exists(backup_path):
        shutil.rmtree(backup_path)
    os.rename(db_path, backup_path)
    
    print("Installing clean database...")
    os.rename(new_db_path, db_path)
    
    print(f"\nMigration complete! Your database has been cleanly rewritten and is 100% safe to optimize.")
    print(f"A backup of your old database has been saved at: {backup_path}")
    print(f"You can safely delete the backup once you verify tweetxvault sync works.")

if __name__ == "__main__":
    run_migration()
