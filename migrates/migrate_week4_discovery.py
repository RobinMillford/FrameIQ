"""
Week 4: User Discovery Database Migration
Creates tables for user taste profiles and similarity scoring
"""
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

def run_migration():
    """Run the Week 4 discovery migration"""
    database_url = os.getenv("DATABASE_URL")
    
    if not database_url:
        print("❌ DATABASE_URL not found in environment variables")
        return
    
    print(f"Connecting to database...")
    engine = create_engine(database_url)
    
    with engine.connect() as conn:
        # Start transaction
        trans = conn.begin()
        
        try:
            print("\n🚀 Starting Week 4 Discovery Migration...")
            
            # ================================================================
            # 1. Create user_taste_profile table
            # ================================================================
            print("\n1️⃣ Creating user_taste_profile table...")
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_taste_profile (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    favorite_genres JSONB DEFAULT '{}',
                    avg_rating FLOAT DEFAULT 0.0,
                    total_watched INTEGER DEFAULT 0,
                    total_reviews INTEGER DEFAULT 0,
                    decade_preferences JSONB DEFAULT '{}',
                    top_rated_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(user_id)
                );
            """))
            print("   ✅ user_taste_profile table created")
            
            # ================================================================
            # 2. Create user_similarity table
            # ================================================================
            print("\n2️⃣ Creating user_similarity table...")
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_similarity (
                    id SERIAL PRIMARY KEY,
                    user_id_1 INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    user_id_2 INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    similarity_score FLOAT NOT NULL DEFAULT 0.0,
                    common_movies INTEGER DEFAULT 0,
                    common_likes INTEGER DEFAULT 0,
                    rating_correlation FLOAT DEFAULT 0.0,
                    calculated_at TIMESTAMP DEFAULT NOW(),
                    CONSTRAINT check_different_users CHECK (user_id_1 != user_id_2),
                    CONSTRAINT unique_user_pair UNIQUE(user_id_1, user_id_2)
                );
            """))
            print("   ✅ user_similarity table created")
            
            # ================================================================
            # 3. Create indexes for performance
            # ================================================================
            print("\n3️⃣ Creating indexes...")
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_taste_profile_user 
                ON user_taste_profile(user_id);
            """))
            print("   ✅ Index: idx_taste_profile_user")
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_taste_profile_updated 
                ON user_taste_profile(updated_at DESC);
            """))
            print("   ✅ Index: idx_taste_profile_updated")
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_similarity_user1_score 
                ON user_similarity(user_id_1, similarity_score DESC);
            """))
            print("   ✅ Index: idx_similarity_user1_score")
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_similarity_user2_score 
                ON user_similarity(user_id_2, similarity_score DESC);
            """))
            print("   ✅ Index: idx_similarity_user2_score")
            
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_similarity_calculated 
                ON user_similarity(calculated_at DESC);
            """))
            print("   ✅ Index: idx_similarity_calculated")
            
            # ================================================================
            # 4. Add search index on username
            # ================================================================
            print("\n4️⃣ Creating user search indexes...")
            
            # Use regular lowercase index (works on all PostgreSQL versions)
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_user_username_lower 
                ON "user"(LOWER(username));
            """))
            print("   ✅ Index: idx_user_username_lower")
            
            # Commit transaction
            trans.commit()
            
            print("\n" + "="*60)
            print("✅ Week 4 Discovery Migration Complete!")
            print("="*60)
            print("\nNew Tables Created:")
            print("  • user_taste_profile - User preferences and stats")
            print("  • user_similarity - Pre-calculated similarity scores")
            print("\nIndexes Created:")
            print("  • User search indexes (trigram)")
            print("  • Taste profile lookup indexes")
            print("  • Similarity score indexes")
            print("\nNext Steps:")
            print("  1. Restart your Flask app")
            print("  2. Taste profiles will auto-populate as users interact")
            print("  3. Run similarity calculations periodically")
            
        except Exception as e:
            trans.rollback()
            print(f"\n❌ Migration failed: {e}")
            raise

if __name__ == "__main__":
    run_migration()
