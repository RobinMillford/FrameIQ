"""
Database migration script for Lists and Diary features
Run this script to add the new tables to your database
"""
from app import app, db
from models import UserList, UserListItem, DiaryEntry

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
