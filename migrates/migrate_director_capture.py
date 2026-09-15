#!/usr/bin/env python3
"""Migration: director-capture schema (Feature #6, Phase 10).

models/director.py declares:
  - director            — person identity keyed by the STABLE TMDb person
                          id (tmdb_person_id unique) so display-name
                          changes never fork a person
  - media_director      — unique (media_item_id, director_id) association
  - media_item.directors_enriched_at (column) — distinguishes "not yet
    enriched" (NULL) from "enriched, no director found" (set). Declared
    in models/media.py; production tables created before this column
    existed do NOT have it — db.create_all() never alters existing
    tables, so enrichment would fail with UndefinedColumn.

Safety properties (post-incident conventions):
  - Idempotent: creates/alters only when objects are absent; re-runs
    are no-ops.
  - Never drops anything, never populates rows, never calls business
    logic. The only ALTER is the documented, deliberate nullable-column
    addition (same shape as migrates/migrate_media_runtime.py).
  - Never touches taste_profile, recommendation_feedback, or the legacy
    user_taste_profile / user_similarity tables.
  - Skips the read-only startup schema guard via SKIP_SCHEMA_GUARD=1
    (this script exists precisely to bring a database the guard would
    refuse up to parity). All other app code keeps the guard enabled.

Run: python migrates/migrate_director_capture.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

TABLES = ('director', 'media_director')
ALTER_COLUMN = ('media_item', 'directors_enriched_at')


def _ensure_column(engine, table, column):
    """Add the enrichment-status column when missing (nullable, metadata-
    only in PostgreSQL — no table rewrite, no data touched)."""
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    if column in [c['name'] for c in inspector.get_columns(table)]:
        print(f"[OK] {table}.{column} already exists.")
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} TIMESTAMP"))
        print(f"[OK] Added {table}.{column}.")
        return True
    except Exception:  # noqa: BLE001 — concurrent-runner race
        if column in [c['name'] for c in inspect(engine).get_columns(table)]:
            print(f"[OK] {table}.{column} already exists (concurrent run).")
            return False
        raise


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)
        for table in TABLES:
            if table in inspector.get_table_names():
                print(f"[OK] {table} exists.")
                continue
            # Targeted create: only the declared table, exactly like the
            # other safe migrations. Idempotent for objects that already
            # exist; a concurrent create between check and DDL is handled
            # by re-inspection below.
            try:
                db.metadata.create_all(
                    db.engine, tables=[db.metadata.tables[table]])
                print(f"[OK] Created {table}.")
            except Exception:  # noqa: BLE001 — concurrent-runner race
                inspector = inspect(db.engine)
                if table not in inspector.get_table_names():
                    raise
                print(f"[OK] {table} exists (concurrent run).")

        _ensure_column(db.engine, *ALTER_COLUMN)

        # Fresh inspection — verify against the actual schema, not memory.
        inspector = inspect(db.engine)
        cols = {c['name'] for c in inspector.get_columns('director')}
        required = {'id', 'tmdb_person_id', 'name', 'source',
                    'created_at', 'updated_at'}
        missing = required - cols
        if missing:
            print(f"[FAIL] director missing columns: {sorted(missing)}")
            return 1
        pair = {c['name'] for c in inspector.get_columns('media_director')}
        if not {'media_item_id', 'director_id'} <= pair:
            print("[FAIL] media_director missing association columns.")
            return 1
        status_col = {c['name'] for c in
                      inspector.get_columns('media_item')}
        if 'directors_enriched_at' not in status_col:
            print("[FAIL] media_item.directors_enriched_at missing.")
            return 1

        print("[STATUS] Director-capture schema OK.")
        return 0


if __name__ == '__main__':
    sys.exit(migrate())
