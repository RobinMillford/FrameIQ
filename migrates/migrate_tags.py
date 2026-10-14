#!/usr/bin/env python3
"""
Migration script to create the Tags tables in the database.
This adds Letterboxd-style tagging functionality to FrameIQ.

Run this script to create the new tables:
    python migrate_tags.py
"""

import os
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
from models import db, Tag, UserMediaTag  # noqa: E402

def migrate():
    """Create tags tables in the database"""
    with app.app_context():
        try:
            print("Creating tags tables...")
            
            # Create all tables (will only create if they don't exist)
            db.create_all()
            
            print("✅ Tags tables created successfully!")
            print("\nNew tables:")
            print("  - tag: Stores unique tags")
            print("  - user_media_tag: Junction table for user-media-tag relationships")
            
            # Check if tables were created
            inspector = db.inspect(db.engine)
            tables = inspector.get_table_names()
            
            if 'tag' in tables:
                print("\n✅ 'tag' table exists")
            else:
                print("\n⚠️  'tag' table not found")
                
            if 'user_media_tag' in tables:
                print("✅ 'user_media_tag' table exists")
            else:
                print("⚠️  'user_media_tag' table not found")
            
            print("\n🎉 Migration complete! You can now use tags in FrameIQ.")
            print("\nFeatures:")
            print("  - Add custom tags to movies and TV shows")
            print("  - Autocomplete suggestions from your tags and popular tags")
            print("  - Search and filter media by tags")
            print("  - Max 20 tags per media item")
            
        except Exception as e:
            print(f"❌ Error during migration: {e}")
            import traceback  # noqa: E402
            traceback.print_exc()

if __name__ == '__main__':
    migrate()
