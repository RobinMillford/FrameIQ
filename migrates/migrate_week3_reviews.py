"""
Database migration script for Week 3: Reviews System Enhancement  
Adds ReviewHelpful table and updates existing Review model
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

def migrate():
    """Enhance existing Review system with helpful votes"""
    with app.app_context():
        print("Starting Week 3 Reviews Enhancement migration...")
        print("=" * 60)
        
        try:
            # Add ReviewHelpful model if it doesn't exist
            from sqlalchemy import inspect, text  # noqa: E402
            inspector = inspect(db.engine)
            existing_tables = inspector.get_table_names()
            
            if 'review_helpful' not in existing_tables:
                print("\n📝 Creating review_helpful table...")
                db.session.execute(text("""
                    CREATE TABLE review_helpful (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES "user"(id),
                        review_id INTEGER NOT NULL REFERENCES review(id),
                        is_helpful BOOLEAN NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT unique_user_review_helpful UNIQUE (user_id, review_id)
                    );
                    CREATE INDEX idx_review_helpful_user ON review_helpful(user_id);
                    CREATE INDEX idx_review_helpful_review ON review_helpful(review_id);
                """))
                db.session.commit()
                print("✅ Created review_helpful table")
            else:
                print("✅ review_helpful table already exists")
            
            # Add new columns to review table if they don't exist
            review_columns = [col['name'] for col in inspector.get_columns('review')]
            
            columns_to_add = []
            if 'title' not in review_columns:
                columns_to_add.append(("title", "VARCHAR(200)"))
            if 'rewatch' not in review_columns:
                columns_to_add.append(("rewatch", "BOOLEAN DEFAULT FALSE"))
            if 'helpful_count' not in review_columns:
                columns_to_add.append(("helpful_count", "INTEGER DEFAULT 0"))
            if 'not_helpful_count' not in review_columns:
                columns_to_add.append(("not_helpful_count", "INTEGER DEFAULT 0"))
            
            if columns_to_add:
                print(f"\n📝 Adding {len(columns_to_add)} new columns to review table...")
                for col_name, col_type in columns_to_add:
                    db.session.execute(text(f"ALTER TABLE review ADD COLUMN IF NOT EXISTS {col_name} {col_type}"))
                    print(f"   ✅ Added column: {col_name}")
                db.session.commit()
            else:
                print("✅ All review columns already exist")
            
            # Get stats
            from models import Review, ReviewLike, ReviewComment  # noqa: E402
            review_count = Review.query.count()
            like_count = ReviewLike.query.count()
            comment_count = ReviewComment.query.count()
            
            print("\n📊 Current Review System Stats:")
            print(f"   Reviews: {review_count}")
            print(f"   Likes: {like_count}")
            print(f"   Comments: {comment_count}")
            
            print("\n" + "=" * 60)
            print("✅ Week 3 Reviews Enhancement completed successfully!")
            print("\nEnhanced Features:")
            print("   ⭐ Review Titles - Add titles to reviews")
            print("   🔁 Rewatch Tracking - Mark reviews as rewatches")
            print("   👍 Helpful Votes - 'Was this review helpful?'")
            print("   📊 Enhanced Metrics - Track helpful/not helpful counts")
            print("\nExisting Features:")
            print("   ⭐ Star Ratings (0.5-5.0)")
            print("   💬 Review Comments/Replies")
            print("   ❤️  Review Likes")
            print("\nAPI Endpoints (Enhanced):")
            print("   POST   /api/reviews                  - Create review")
            print("   GET    /api/reviews/<id>             - Get review")
            print("   PUT    /api/reviews/<id>             - Update review")
            print("   DELETE /api/reviews/<id>             - Delete review")
            print("   GET    /api/media/<id>/reviews       - Media reviews")
            print("   GET    /api/reviews/feed             - Reviews feed")
            print("   GET    /api/reviews/popular          - Popular reviews")
            print("   POST   /api/reviews/<id>/helpful     - Vote helpful")
            
        except Exception as e:
            print(f"\n❌ Error during migration: {e}")
            db.session.rollback()
            import traceback  # noqa: E402
            traceback.print_exc()
            raise

if __name__ == "__main__":
    migrate()
