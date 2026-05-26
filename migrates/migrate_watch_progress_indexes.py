"""
Migration: watch_progress partial unique indexes
Replaces the NULL-unsafe UniqueConstraint with partial unique indexes
that correctly enforce uniqueness for movie rows (season/episode IS NULL).
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
            print("Adding partial unique indexes to watch_progress...")

            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_wp_movie_uniq
                ON watch_progress(user_id, tmdb_id, media_type)
                WHERE season IS NULL AND episode IS NULL
            """))
            print("  idx_wp_movie_uniq created")

            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_wp_tv_uniq
                ON watch_progress(user_id, tmdb_id, media_type, season, episode)
                WHERE season IS NOT NULL AND episode IS NOT NULL
            """))
            print("  idx_wp_tv_uniq created")

            trans.commit()
            print("Migration complete.")
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")
            raise


if __name__ == '__main__':
    run_migration()
