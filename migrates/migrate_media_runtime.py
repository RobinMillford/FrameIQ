#!/usr/bin/env python3
"""Migration: add media_item.runtime (Feature 05 — Smart Lists).

models/media.py declares `runtime = db.Column(db.Integer)` (runtime in
minutes, nullable), but production PostgreSQL tables created before that
column existed do NOT have it — db.create_all() never alters existing
tables, so detail-page queries fail with:

    psycopg2.errors.UndefinedColumn: column media_item.runtime does not exist

This script adds the missing column only when it is absent.

Idempotent — safe to run any number of times. No data is touched: the
column is added NULL, nothing is backfilled, and existing rows are not
rewritten (a nullable ADD COLUMN is metadata-only in PostgreSQL).

Run: python migrates/migrate_media_runtime.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402


def _column_exists(engine, table, column):
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return False
    return column in [c['name'] for c in inspector.get_columns(table)]


def _ensure_runtime_column(engine):
    """Add media_item.runtime when missing. Returns True if added here.

    Safe under concurrent runners too: if another process adds the column
    between our inspection and the ALTER, the duplicate-column error is
    re-inspected and treated as already-present.
    """
    if not _column_exists(engine, 'media_item', 'runtime'):
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE media_item ADD COLUMN runtime INTEGER"))
            return True
        except SQLAlchemyError:
            if _column_exists(engine, 'media_item', 'runtime'):
                return False
            raise
    return False


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)
        if 'media_item' not in inspector.get_table_names():
            print("[..] media_item table not found — fresh database;")
            print("     running db.create_all() (creates it WITH runtime).")
            db.create_all()

        added = _ensure_runtime_column(db.engine)
        if added:
            print("[OK] Added media_item.runtime (INTEGER NULL).")
        else:
            print("[OK] media_item.runtime already exists — nothing to do.")

        # Verify against a fresh inspection of the actual schema.
        if _column_exists(db.engine, 'media_item', 'runtime'):
            print("[OK] Verified: media_item.runtime is present.")
            return 0
        print("[FAIL] media_item.runtime is still missing after migration.")
        return 1


if __name__ == '__main__':
    raise SystemExit(migrate())
