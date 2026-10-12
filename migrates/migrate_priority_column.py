#!/usr/bin/env python3
"""
Migration script to add priority column to user_watchlist table

This script specifically adds the 'priority' column if it doesn't exist.
Run this script: python migrate_priority_column.py
"""

# F22: authorize the database target BEFORE any application import, engine
# construction or DDL/DML. `import app` builds an engine and runs the schema
# guard, so a check placed after it would already be too late.
import os as _f22_os  # F22: make `utils` importable regardless of cwd  # noqa: E402
import sys as _f22_sys  # noqa: E402
_f22_sys.path.insert(0, _f22_os.path.dirname(
    _f22_os.path.dirname(_f22_os.path.abspath(__file__))))
from utils.legacy_migration_guard import authorize_legacy_migration  # noqa: E402

authorize_legacy_migration(__file__)

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import text  # noqa: E402

def migrate_priority_column():
    """Add priority column to user_watchlist table"""
    with app.app_context():
        try:
            print("🎯 Adding Priority Column to Watchlist...")
            print("=" * 60)
            
            # Check if column exists
            inspector = db.inspect(db.engine)
            columns = [col['name'] for col in inspector.get_columns('user_watchlist')]
            
            if 'priority' in columns:
                print("✅ Priority column already exists!")
            else:
                print("⚙️  Adding priority column...")
                
                # Add the priority column with default value
                with db.engine.connect() as conn:
                    conn.execute(text("""
                        ALTER TABLE user_watchlist 
                        ADD COLUMN IF NOT EXISTS priority VARCHAR(10) DEFAULT 'medium'
                    """))
                    conn.commit()
                
                print("✅ Priority column added successfully!")
            
            # Verify the column was added
            columns = [col['name'] for col in inspector.get_columns('user_watchlist')]
            if 'priority' in columns:
                print("\n🎉 Migration Complete!")
                print("=" * 60)
                print("\nWatchlist priorities are now available!")
                print("Values: 'high', 'medium' (default), 'low'")
            else:
                print("\n⚠️  Warning: Priority column still not found")
                
        except Exception as e:
            print(f"❌ Error during migration: {e}")
            import traceback  # noqa: E402
            traceback.print_exc()

if __name__ == '__main__':
    migrate_priority_column()
