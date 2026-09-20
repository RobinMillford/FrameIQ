"""Migration: create movie_release_date table (Feature 10B).

Idempotent — safe to run multiple times. db.create_all() also creates
the table on fresh databases; this script is for existing production
databases. No existing data is modified: this is a new, standalone
table (watchlist semantics and MediaItem are untouched).

Run with SKIP_SCHEMA_GUARD=1 (the schema guard blocks the app import
while the table is still missing):
    SKIP_SCHEMA_GUARD=1 python migrates/migrate_movie_release_dates.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)

        if 'movie_release_date' in inspector.get_table_names():
            print("[OK] movie_release_date already exists — nothing to do.")
            return

        db.session.execute(text("""
            CREATE TABLE movie_release_date (
                id SERIAL PRIMARY KEY,
                tmdb_id INTEGER NOT NULL,
                region VARCHAR(2) NOT NULL DEFAULT 'US',
                release_type INTEGER NOT NULL,
                release_date DATE NOT NULL,
                fetched_at TIMESTAMP
            )
        """))
        db.session.execute(text(
            "CREATE UNIQUE INDEX uq_release_tmdb_region_type "
            "ON movie_release_date (tmdb_id, region, release_type)"))
        db.session.execute(text(
            "CREATE INDEX idx_release_tmdb_region "
            "ON movie_release_date (tmdb_id, region)"))
        db.session.commit()
        print("[OK] Created movie_release_date table.")


if __name__ == '__main__':
    migrate()
