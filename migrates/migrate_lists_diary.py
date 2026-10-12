"""
Database migration script for Lists and Diary features
Run this script to add the new tables to your database
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

from app import app, db  # noqa: E402
from models import UserList, UserListItem, DiaryEntry  # noqa: E402

def migrate():
    """Create new tables for Lists and Diary features"""
    with app.app_context():
        print("Starting database migration...")
        
        try:
            # Create all new tables
            db.create_all()
            print("✅ Successfully created tables:")
            print("   - user_list")
            print("   - user_list_item")
            print("   - diary_entry")
            print("\nMigration completed successfully!")
            
        except Exception as e:
            print(f"❌ Error during migration: {e}")
            raise

if __name__ == "__main__":
    migrate()
