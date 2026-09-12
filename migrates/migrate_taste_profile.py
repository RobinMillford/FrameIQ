#!/usr/bin/env python3
"""Migration: create taste_profile (Feature #6 — Taste Profile, Phase 1).

models/taste_profile.py declares TasteProfile — one row per user holding
the COMPUTED personalization state (genre/decade weights, director
affinity, runtime preference, confidence). Phase 1 establishes the schema
only; no profile computation, no population, no consumers.

Safety properties (post-incident conventions):
  - Idempotent: creates the table only when absent; re-runs are no-ops.
  - Never alters/drops existing user/media/history data.
  - Never populates profiles (no backfill) — the table starts empty.
  - Never touches the legacy user_taste_profile / user_similarity tables.
  - Never calls application business logic — DDL only.
  - Skips the read-only startup schema guard (this script exists precisely
    to bring a database the guard would refuse up to parity).

Run: python migrates/migrate_taste_profile.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Escape the startup schema guard BEFORE importing the app: importing `app`
# already executes create_app() (incl. the read-only guard), and this
# migration may run against a database the guard would refuse. Only this
# script's explicit DDL writes; the guard stays enabled everywhere else.
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import inspect  # noqa: E402


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        if 'taste_profile' in tables:
            print("[OK] taste_profile table already exists — nothing to do.")
        else:
            print("[..] Creating taste_profile ...")
            # Targeted DDL: create only the declared TasteProfile table
            # (create_all with tables=[...] is a no-op for tables that
            # already exist and never touches other tables).
            db.metadata.create_all(db.engine, tables=[
                db.metadata.tables['taste_profile'],
            ])
            print("[OK] taste_profile created.")

        # Fresh inspection of the actual database schema.
        inspector = inspect(db.engine)
        if 'taste_profile' not in inspector.get_table_names():
            print("[FAIL] taste_profile is still missing after migration.")
            return 1

        cols = [c['name'] for c in inspector.get_columns('taste_profile')]
        expected = {
            'id', 'user_id', 'genre_weights_json', 'decade_weights_json',
            'director_affinity_json', 'runtime_pref_json',
            'media_type_pref_json', 'mood_tags_json', 'confidence',
            'signal_count', 'distinct_title_count', 'profile_version',
            'created_at', 'updated_at',
        }
        missing = expected - set(cols)
        if missing:
            print(f"[FAIL] taste_profile missing columns: {sorted(missing)}")
            return 1

        # Never touched by this script — verify they were not disturbed.
        tables = inspector.get_table_names()
        for legacy in ('user_taste_profile', 'user_similarity'):
            state = ("present (untouched)" if legacy in tables
                     else "absent (never managed by this script)")
            print(f"[OK] legacy {legacy}: {state}")

        print("\n[DONE] Feature #6 Phase 1 schema ready — taste_profile exists.")
        return 0


if __name__ == '__main__':
    raise SystemExit(migrate())
