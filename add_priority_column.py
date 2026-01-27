"""
Migration script to add priority column to user_watchlist and user_wishlist tables
"""
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get database URL
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found in environment variables")

# Create engine
engine = create_engine(DATABASE_URL)

def add_priority_columns():
    """Add priority column to watchlist and wishlist tables"""
    with engine.connect() as conn:
        # Start transaction
        trans = conn.begin()
        
        try:
            # Check and add priority to user_watchlist if not exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='user_watchlist' AND column_name='priority'
            """))
            
            if not result.fetchone():
                print("Adding priority column to user_watchlist...")
                conn.execute(text("""
                    ALTER TABLE user_watchlist 
                    ADD COLUMN priority VARCHAR(10) DEFAULT 'medium'
                """))
                print("✅ Added priority column to user_watchlist")
            else:
                print("✓ Priority column already exists in user_watchlist")
            
            # Check and add priority to user_wishlist if not exists
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='user_wishlist' AND column_name='priority'
            """))
            
            if not result.fetchone():
                print("Adding priority column to user_wishlist...")
                conn.execute(text("""
                    ALTER TABLE user_wishlist 
                    ADD COLUMN priority VARCHAR(10) DEFAULT 'medium'
                """))
                print("✅ Added priority column to user_wishlist")
            else:
                print("✓ Priority column already exists in user_wishlist")
            
            # Commit transaction
            trans.commit()
            print("\n✅ Migration completed successfully!")
            
        except Exception as e:
            trans.rollback()
            print(f"\n❌ Migration failed: {e}")
            raise

if __name__ == "__main__":
    print("Starting migration to add priority columns...")
    add_priority_columns()
