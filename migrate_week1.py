#!/usr/bin/env python3
"""
Migration script for Week 1 features: Likes, Watchlist Priorities, Media Comments

Run this script to create the new tables and update existing ones:
    python migrate_week1.py
"""

import os
from app import app
from models import db, MediaLike, MediaComment

def migrate():
    """Create Week 1 feature tables"""
    with app.app_context():
        try:
            print("🎬 Migrating Week 1 Features...")
            print("=" * 60)
            
            # Create all tables
            db.create_all()
            
            print("\n✅ Tables created/updated successfully!")
            
            # Check tables
            inspector = db.inspect(db.engine)
            tables = inspector.get_table_names()
            
            print("\nNew Features:")
            print("-" * 60)
            
            # Check MediaLike table
            if 'media_like' in tables:
                print("✅ 'media_like' table exists - Likes (Hearts) ready!")
            else:
                print("⚠️  'media_like' table not found")
            
            # Check MediaComment table
            if 'media_comment' in tables:
                print("✅ 'media_comment' table exists - Film Comments ready!")
            else:
                print("⚠️  'media_comment' table not found")
            
            # Check watchlist priority column
            if 'user_watchlist' in tables:
                print("✅ 'user_watchlist' table exists")
                columns = [col['name'] for col in inspector.get_columns('user_watchlist')]
                if 'priority' in columns:
                    print("   ✅ Priority column added!")
                else:
                    print("   ⚠️  Priority column not found - may need manual migration")
            
            print("\n" + "=" * 60)
            print("🎉 Week 1 Features Migration Complete!")
            print("=" * 60)
            
            print("\nFeatures Ready:")
            print("  1. ❤️  Likes (Hearts) - Quick appreciation for movies/shows")
            print("  2. 🎯 Watchlist Priorities - High/Medium/Low flags")
            print("  3. 💬 Film Page Comments - Community discussion")
            
            print("\nNext Steps:")
            print("  1. Add UI elements to movie/TV detail pages")
            print("  2. Test the new endpoints")
            print("  3. Enjoy the new features!")
            
        except Exception as e:
            print(f"❌ Error during migration: {e}")
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    migrate()
