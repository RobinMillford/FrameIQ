"""
Database migration script for Week 2: Enhanced Lists
Adds cover_image and slug columns to user_list table
"""
from app import app, db
from models import UserList
import re

def generate_slug(title, list_id):
    """Generate a URL-friendly slug from list title"""
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^\w\s-]', '', title.lower())
    slug = re.sub(r'[-\s]+', '-', slug)
    slug = slug.strip('-')
    
    # Add list ID to ensure uniqueness
    return f"{slug}-{list_id}"

def migrate():
    """Add cover_image and slug columns to user_list table"""
    with app.app_context():
        print("Starting Week 2 Lists migration...")
        
        try:
            # Add columns using raw SQL
            with db.engine.connect() as conn:
                # Add cover_image column
                try:
                    conn.execute(db.text("""
                        ALTER TABLE user_list 
                        ADD COLUMN IF NOT EXISTS cover_image VARCHAR(500);
                    """))
                    conn.commit()
                    print("✅ Added cover_image column")
                except Exception as e:
                    print(f"⚠️  cover_image column might already exist: {e}")
                
                # Add slug column
                try:
                    conn.execute(db.text("""
                        ALTER TABLE user_list 
                        ADD COLUMN IF NOT EXISTS slug VARCHAR(250) UNIQUE;
                    """))
                    conn.commit()
                    print("✅ Added slug column")
                except Exception as e:
                    print(f"⚠️  slug column might already exist: {e}")
                
                # Create index on slug
                try:
                    conn.execute(db.text("""
                        CREATE INDEX IF NOT EXISTS idx_user_list_slug 
                        ON user_list(slug);
                    """))
                    conn.commit()
                    print("✅ Created index on slug column")
                except Exception as e:
                    print(f"⚠️  Index might already exist: {e}")
            
            # Backfill slugs for existing lists
            print("\n📝 Generating slugs for existing lists...")
            lists = UserList.query.filter(UserList.slug.is_(None)).all()
            
            for user_list in lists:
                user_list.slug = generate_slug(user_list.title, user_list.id)
            
            db.session.commit()
            print(f"✅ Generated slugs for {len(lists)} existing lists")
            
            print("\n✅ Week 2 Lists migration completed successfully!")
            print("\nNew fields added to user_list:")
            print("   - cover_image (VARCHAR 500) - URL for list cover image")
            print("   - slug (VARCHAR 250, UNIQUE) - SEO-friendly shareable URL")
            
        except Exception as e:
            print(f"❌ Error during migration: {e}")
            db.session.rollback()
            raise

if __name__ == "__main__":
    migrate()
