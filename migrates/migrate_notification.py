"""Migration: create notification table (Feature 04).

Idempotent — safe to run multiple times. db.create_all() also creates the
table on fresh databases; this script is for existing production databases.
No existing data is modified: this is a new, standalone table.
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

        if 'notification' in inspector.get_table_names():
            print("[OK] notification already exists — nothing to do.")
            return

        db.session.execute(text("""
            CREATE TABLE notification (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES "user"(id),
                type VARCHAR(40) NOT NULL DEFAULT 'new_episode',
                title VARCHAR(200) NOT NULL,
                body TEXT,
                target_url VARCHAR(500),
                show_id INTEGER,
                season INTEGER,
                episode INTEGER,
                episode_name VARCHAR(200),
                poster_path VARCHAR(500),
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                read_at TIMESTAMP
            )
        """))
        db.session.execute(text(
            "CREATE INDEX ix_notification_user_id ON notification (user_id)"))
        db.session.execute(text(
            "CREATE INDEX ix_notification_created_at ON notification (created_at)"))
        db.session.execute(text(
            "CREATE INDEX ix_notification_read_at ON notification (read_at)"))
        db.session.execute(text(
            "CREATE INDEX idx_notification_unread "
            "ON notification (user_id, read_at)"))
        db.session.execute(text(
            "CREATE UNIQUE INDEX uq_notification_user_episode "
            "ON notification (user_id, type, show_id, season, episode)"))
        db.session.commit()
        print("[OK] Created notification table.")


if __name__ == '__main__':
    migrate()
