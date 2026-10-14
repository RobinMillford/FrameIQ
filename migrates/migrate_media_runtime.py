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

Scope
-----
This script owns exactly one column: ``media_item.runtime``. It adds a
column to a table that already exists and creates nothing else. If
``media_item`` is absent it refuses and names the mechanism that does own
fresh-database bootstrap (``scripts/bootstrap_dev_schema.py``) rather than
bootstrapping the schema itself — see ``migrate()`` for why.

Run: python migrates/migrate_media_runtime.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Escape the startup schema guard BEFORE importing the app: importing `app`
# already executes create_app() (incl. the read-only guard), and this
# migration exists precisely to repair a database the guard would refuse.
# SKIP_SCHEMA_GUARD disables only the guard (read-only); the ALTER below is
# this script's explicit, deliberate write. All other migrates/*.py scripts
# keep their existing behavior (they create/alter only on missing objects,
# so the guard passes on their target databases).
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")

# F22: authorize the database target BEFORE any application import, engine
# construction or DDL/DML. `import app` builds an engine and runs the schema
# guard, so a check placed after it would already be too late.
from utils.legacy_migration_guard import authorize_legacy_migration  # noqa: E402

authorize_legacy_migration(__file__)

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
            # F31: this used to fall back to `db.create_all()`. That call is
            # unscoped — it creates EVERY table the models declare — for a
            # migration whose entire subject is one column on one existing
            # table. Being authorized to write to a target (the
            # `authorize_legacy_migration` call above) is permission to perform
            # THIS alteration, not permission to bootstrap a database from it.
            #
            # A database with no `media_item` is also not the incident this
            # script exists for: the documented failure was a deployed
            # PostgreSQL database that HAD media_item and lacked only
            # `runtime`. So refusing here loses nothing, and it matches
            # migrations_0003_cast_persistence, which refuses the same way when
            # the table it alters does not exist.
            #
            # Each failure mode below is named, because they need different
            # remedies and "it failed" would not tell an operator which.
            print("[FAIL] media_item does not exist on this database, so there "
                  "is no table to add the column to.")
            print("       This script adds ONE column to an EXISTING table. It "
                  "does not bootstrap the application schema, and it will not "
                  "create every table the models declare as a side effect.")
            print("       * fresh / scratch database -> python "
                  "scripts/bootstrap_dev_schema.py")
            print("       * deployed database        -> python scripts/migrate.py "
                  "upgrade")
            print("       * already has the column    -> nothing to do; this "
                  "script is historical")
            return 1

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
