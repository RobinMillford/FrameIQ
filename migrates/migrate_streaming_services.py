#!/usr/bin/env python3
"""
Migration: Feature 03 — Streaming Intelligence.

1. Adds user.streaming_region (nullable VARCHAR(2)) for the Where-to-Watch
   region preference.
2. Creates the user_streaming_services table (My Services) if missing.
   db.create_all() already handles the new table on startup, so this script
   mainly matters for adding the streaming_region column to existing user
   tables and for verifying both on production Postgres.

Idempotent — safe to re-run. Run: python migrates/migrate_streaming_services.py
"""

from app import app
from models import db
from sqlalchemy import text


def migrate():
    with app.app_context():
        inspector = db.inspect(db.engine)

        # ── 1. user.streaming_region ──────────────────────────────────────
        user_cols = [c['name'] for c in inspector.get_columns('user')]
        if 'streaming_region' in user_cols:
            print("✅ user.streaming_region already exists")
        else:
            print("⚙️  Adding user.streaming_region...")
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE \"user\" ADD COLUMN streaming_region VARCHAR(2)"
                ))
                conn.commit()
            print("✅ user.streaming_region added")

        # ── 2. user_streaming_services table ──────────────────────────────
        tables = inspector.get_table_names()
        if 'user_streaming_services' in tables:
            print("✅ user_streaming_services table already exists")
        else:
            print("⚙️  Creating user_streaming_services...")
            db.create_all()
            print("✅ user_streaming_services created")

        # ── Verify ─────────────────────────────────────────────────────────
        inspector = db.inspect(db.engine)
        ok_region = 'streaming_region' in [
            c['name'] for c in inspector.get_columns('user')]
        ok_table = 'user_streaming_services' in inspector.get_table_names()
        if ok_region and ok_table:
            print("\n🎉 Migration complete — Feature 03 schema ready.")
            return 0
        print("\n❌ Migration verification failed "
              f"(region={ok_region}, table={ok_table})")
        return 1


if __name__ == '__main__':
    raise SystemExit(migrate())
