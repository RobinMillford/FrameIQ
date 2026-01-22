#!/usr/bin/env python3
"""
Migration script to add priority column to user_watchlist table

This script specifically adds the 'priority' column if it doesn't exist.
Run this script: python migrate_priority_column.py
"""

from app import app
from models import db
from sqlalchemy import text

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
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    migrate_priority_column()
