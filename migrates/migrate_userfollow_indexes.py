"""
Migration: add composite indexes on user_follow(following_id, is_active)
and user_follow(follower_id, is_active).

These cover the hot query pattern:
  UserFollow.query.filter_by(following_id=X, is_active=True)
  UserFollow.query.filter_by(follower_id=X, is_active=True)
"""
import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()


def run_migration():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set")
        return

    engine = create_engine(database_url)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_uf_following_active
                ON user_follow(following_id, is_active)
            """))
            print("  idx_uf_following_active created")

            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_uf_follower_active
                ON user_follow(follower_id, is_active)
            """))
            print("  idx_uf_follower_active created")

            trans.commit()
            print("Migration complete.")
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")
            raise


if __name__ == '__main__':
    run_migration()
