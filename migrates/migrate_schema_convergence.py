#!/usr/bin/env python3
"""Migration: converge the live schema onto the declared model schema.

Why this exists
---------------
``app.py`` used to call ``db.create_all()`` on every start, which meant a
production web process silently owned the schema. That has been removed: startup
now performs no DDL and the schema guard refuses to boot on an unprepared
database.

Removing it exposed how far the live database had drifted. On production,
11 tables the models declare did not exist. They had been "created" only
because ``create_all()`` ran at boot — and on that database ``create_all()`` was
*failing*, rolling back, and leaving the schema short every time:

    taste_profile            MISSING
    import_source_mapping    MISSING
    continue_watching_item   MISSING
    director                 MISSING
    media_director           MISSING
    movie_release_date       MISSING
    notification             MISSING
    recommendation_feedback  MISSING
    smart_list               MISSING
    user_streaming_services  MISSING
    year_in_review_share     MISSING

The cause of the failure was a name collision, not a missing file.
``models/taste_profile.py`` declared the index ``idx_taste_profile_updated``,
while ``migrates/migrate_week4_discovery.py`` already owned that exact name on
the legacy ``user_taste_profile`` table. PostgreSQL scopes index names to the
schema, not the table, so creating the second one raised ``DuplicateTable`` and
the surrounding transaction rolled back — taking every other table creation in
the same call with it. One colliding index name had been blocking all eleven.

This migration creates the missing tables from the model metadata, which is the
single source of truth for the declared schema.

Safety properties
-----------------
- **Idempotent**: creates only tables that are absent; re-runs are no-ops.
- **Additive only**: never ALTERs an existing table, never DROPs anything, and
  never writes a single row. Tables not declared by the models
  (``user_wishlist``, ``user_taste_profile``, ``user_similarity``) are left
  completely alone.
- **Pre-flight collision check**: before any DDL it verifies that no index or
  constraint name this migration needs is already owned by a *different*
  table, and aborts with the conflicting names. That turns the failure mode
  that caused this drift — a mid-transaction rollback reported as a bare
  ``DuplicateTable`` — into a legible error before anything is attempted.
- **Self-verifying**: prints every table it created and re-inspects afterwards.
- Skips the read-only startup guard via ``SKIP_SCHEMA_GUARD=1``.

Run (one-off container, before the web process is recreated):

    SKIP_SCHEMA_GUARD=1 python migrates/migrate_schema_convergence.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The guard would refuse to import against exactly the database this script
# exists to repair, so it is disabled BEFORE `app` is imported.
os.environ.setdefault('SKIP_SCHEMA_GUARD', '1')

from app import app  # noqa: E402
from models import db  # noqa: E402
from sqlalchemy import inspect  # noqa: E402


def missing_tables():
    """Declared tables the live database does not have, in stable order."""
    inspector = inspect(db.engine)
    live = set(inspector.get_table_names())
    return [table for table in db.metadata.sorted_tables
            if table.name not in live]


def _declared_names(table):
    """The named index/constraint objects a table declares."""
    for index in table.indexes:
        if index.name:
            yield 'index', index.name
    for constraint in table.constraints:
        if constraint.name:
            yield 'constraint', constraint.name


def live_index_owners(inspector):
    """``{index_or_constraint_name: owning_table}`` read from the LIVE catalog.

    This has to come from the database rather than from model metadata, and the
    reason is the exact bug this migration exists to fix: the colliding owner
    (``user_taste_profile``) is **not declared by any model**. It was created by
    a legacy migration and survives only in the live schema. Asking the models
    who owns ``idx_taste_profile_updated`` returns nobody, which is precisely
    why the collision went unnoticed until a CREATE INDEX failed inside a
    transaction that rolled back eleven tables with it.
    """
    owners = {}
    for table_name in inspector.get_table_names():
        try:
            indexes = inspector.get_indexes(table_name)
        except Exception:  # noqa: BLE001 - a view or odd relation
            continue
        for index in indexes:
            name = index.get('name')
            if name:
                owners.setdefault(name, table_name)
        try:
            uniques = inspector.get_unique_constraints(table_name)
        except Exception:  # noqa: BLE001
            uniques = []
        for constraint in uniques:
            name = constraint.get('name')
            if name:
                owners.setdefault(name, table_name)
    return owners


def name_conflicts(tables):
    """``[(kind, name, wanted_table, owner_table)]`` for names already taken.

    An index or constraint name may only be used once per schema in
    PostgreSQL. If one of the tables we are about to create declares a name
    that some OTHER existing table already owns, creating it will raise
    ``DuplicateTable`` — and because the whole run is one transaction, that
    would roll back every other table in this migration too. Detecting it up
    front is the difference between a clear message and an unexplained
    all-or-nothing failure.
    """
    owned = live_index_owners(inspect(db.engine))

    conflicts = []
    for table in tables:
        for kind, name in _declared_names(table):
            owner = owned.get(name)
            if owner and owner != table.name:
                conflicts.append((kind, name, table.name, owner))
    return conflicts


def migrate():
    with app.app_context():
        inspector = inspect(db.engine)
        live_before = set(inspector.get_table_names())

        print("Schema convergence")
        print("  live tables before : %d" % len(live_before))
        print("  declared tables    : %d" % len(db.metadata.sorted_tables))

        wanted = missing_tables()
        if not wanted:
            print("[OK] every declared table already exists — nothing to do.")
            _verify()
            return 0

        print("  missing tables     : %d" % len(wanted))
        for table in wanted:
            print("      - %s" % table.name)
        print()

        conflicts = name_conflicts(wanted)
        if conflicts:
            print("[FAIL] schema name collision — refusing to run.")
            print("  PostgreSQL scopes index and constraint names to the schema,")
            print("  so the same name cannot be used by two tables:")
            for kind, name, wanted_by, owner in conflicts:
                print("      %-11s %-32s wanted by %-24s already owned by %s"
                      % (kind, name, wanted_by, owner))
            print()
            print("  Rename the newer declaration in the model, or drop the stale")
            print("  object on the owning table. Nothing was changed.")
            return 1

        # Metadata for exactly the missing tables — the single source of truth
        # for the declared schema, so the migration cannot drift from models/.
        created = []
        for table in wanted:
            table.create(bind=db.engine, checkfirst=True)
            created.append(table.name)
            print("[DONE] created %s" % table.name)

        db.session.commit()

        print()
        print("  created %d table(s)" % len(created))
        _verify()
    return 0


def _verify():
    """Re-inspect with a fresh Inspector and confirm the declared schema."""
    inspector = inspect(db.engine)
    live = set(inspector.get_table_names())
    declared = {t.name for t in db.metadata.sorted_tables}

    missing = sorted(declared - live)
    if missing:
        print("[FAIL] still missing after convergence: %s" % ", ".join(missing))
        return 1

    print("[OK] every declared table exists (%d total)." % len(live))

    # Tables the models do not declare must survive untouched. The wishlist
    # consolidation and the legacy discovery tables live here.
    extra = sorted(live - declared)
    if extra:
        print("[OK] left untouched (not declared by any model): %s"
              % ", ".join(extra))
    return 0


if __name__ == '__main__':
    raise SystemExit(migrate())
