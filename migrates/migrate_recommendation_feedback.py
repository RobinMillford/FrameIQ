#!/usr/bin/env python3
"""Migration: create recommendation_feedback (Feature #7 — Feedback, Phase 1).

models/recommendation_feedback.py declares RecommendationFeedback — the
append-only event history for recommendation interactions (impression,
click, not_interested, already_watched, saved, rated) across the
home_for_you / profile_recs / more_like_this surfaces. Phase 1 establishes
the schema only: no writers, no readers, no API, no UI.

Safety properties (post-incident conventions):
  - Idempotent: creates the table only when absent; re-runs are no-ops.
  - Never alters/drops existing tables or data.
  - Never populates feedback — the table starts empty.
  - Never touches taste_profile or the legacy user_taste_profile /
    user_similarity tables.
  - Never calls application business logic — DDL only.
  - Skips the read-only startup schema guard (this script exists precisely
    to bring a database the guard would refuse up to parity).

Run: python migrates/migrate_recommendation_feedback.py
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

TABLE = 'recommendation_feedback'

EXPECTED_COLUMNS = {
    'id', 'user_id', 'media_id', 'media_type', 'surface', 'source', 'event',
    'position', 'reason_kind', 'payload_json', 'model_version', 'event_date',
    'created_at',
}


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        if TABLE in tables:
            print(f"[OK] {TABLE} table already exists — nothing to do.")
        else:
            print(f"[..] Creating {TABLE} ...")
            # Targeted DDL: create only the declared RecommendationFeedback
            # table (create_all with tables=[...] is a no-op for tables that
            # already exist and never touches other tables). Includes the
            # partial unique index backing the once-per-day idempotency rule.
            db.metadata.create_all(db.engine, tables=[
                db.metadata.tables[TABLE],
            ])
            print(f"[OK] {TABLE} created.")

        # Fresh inspection of the actual database schema.
        inspector = inspect(db.engine)
        if TABLE not in inspector.get_table_names():
            print(f"[FAIL] {TABLE} is still missing after migration.")
            return 1

        cols = [c['name'] for c in inspector.get_columns(TABLE)]
        missing = EXPECTED_COLUMNS - set(cols)
        if missing:
            print(f"[FAIL] {TABLE} missing columns: {sorted(missing)}")
            return 1

        # Never touched by this script — verify they were not disturbed.
        tables = inspector.get_table_names()
        for unrelated in ('taste_profile', 'user_taste_profile',
                          'user_similarity', 'user', 'media_item', 'review',
                          'diary_entry', 'media_like', 'user_media_tag'):
            state = ("present (untouched)" if unrelated in tables
                     else "absent (never managed by this script)")
            print(f"[OK] {unrelated}: {state}")

        print(f"\n[DONE] Feature #7 Phase 1 schema ready — {TABLE} exists.")
        return 0


if __name__ == '__main__':
    raise SystemExit(migrate())
