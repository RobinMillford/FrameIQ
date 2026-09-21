"""Migration: consolidate Wishlist into the canonical Watchlist.

Idempotent — safe to run multiple times. On the first run it:

    1. copies every ``user_wishlist`` row that does NOT already exist in
       the same user's ``user_watchlist`` into ``user_watchlist``
       (preserving ``priority`` and ``date_added``; an existing
       Watchlist row always wins — it is never overwritten), then
    2. drops the ``user_wishlist`` table.

On every later run (or a fresh database where the table never existed)
it is a no-op. After this migration the application metadata and the
live database agree: ``user_watchlist`` only, no ``user_wishlist``.

Run with SKIP_SCHEMA_GUARD=1 (the schema guard blocks the app import
while the legacy table still exists in models metadata terms):

    SKIP_SCHEMA_GUARD=1 python migrates/migrate_remove_wishlist.py
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
        tables = inspector.get_table_names()

        if 'user_wishlist' not in tables:
            print("[OK] user_wishlist does not exist — nothing to do.")
            return

        # ── Step 1: merge wishlist-only rows into user_watchlist ──
        # Existing Watchlist rows win: a (user_id, media_id, media_type)
        # already present in user_watchlist is left untouched. Wishlist-
        # only rows are inserted with their original priority and
        # date_added preserved (COALESCE guards legacy NULLs).
        merge_sql = text("""
            INSERT INTO user_watchlist
                (user_id, media_id, media_type, date_added, priority)
            SELECT w.user_id, w.media_id, w.media_type,
                   COALESCE(w.date_added, CURRENT_TIMESTAMP),
                   COALESCE(w.priority, 'medium')
            FROM user_wishlist w
            WHERE NOT EXISTS (
                SELECT 1 FROM user_watchlist wl
                WHERE wl.user_id = w.user_id
                  AND wl.media_id = w.media_id
                  AND wl.media_type = w.media_type
            )
        """)
        result = db.session.execute(merge_sql)
        merged = result.rowcount or 0
        print(f"[OK] Merged {merged} wishlist-only row(s) into user_watchlist.")

        # ── Step 2: drop the legacy table ──
        db.session.execute(text("DROP TABLE user_wishlist"))
        db.session.commit()
        print("[OK] Dropped user_wishlist table.")


if __name__ == '__main__':
    migrate()
