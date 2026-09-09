"""Migration: create continue_watching_item table.

Feature: intent-based Continue Watching ("FrameIQ remembers WHAT I started;
the provider remembers WHERE I stopped").

Idempotent — safe to run multiple times. db.create_all() also creates the
table on fresh databases; this script is for existing production databases.
No existing data is modified: WatchProgress rows are NOT deleted, converted,
or rewritten (they remain available for provider resume-time lookup).
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

        if 'continue_watching_item' in inspector.get_table_names():
            print("[OK] continue_watching_item already exists — nothing to do.")
            return

        db.session.execute(text("""
            CREATE TABLE continue_watching_item (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES "user"(id),
                media_type VARCHAR(10) NOT NULL,
                tmdb_id INTEGER NOT NULL,
                season INTEGER,
                episode INTEGER,
                title VARCHAR(255),
                poster_path VARCHAR(500),
                started_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_continue_watching_user_item UNIQUE
                    (user_id, media_type, tmdb_id, season, episode)
            )
        """))
        db.session.execute(text(
            "CREATE INDEX ix_continue_watching_item_user_id "
            "ON continue_watching_item (user_id)"))
        db.session.execute(text(
            "CREATE INDEX ix_continue_watching_item_tmdb_id "
            "ON continue_watching_item (tmdb_id)"))
        db.session.commit()
        print("[OK] Created continue_watching_item table.")


if __name__ == '__main__':
    migrate()
