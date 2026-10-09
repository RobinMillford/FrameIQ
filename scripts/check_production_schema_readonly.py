#!/usr/bin/env python3
"""READ-ONLY production schema diagnostic. Never imports the application.

Why this script exists as a separate file
-----------------------------------------
Importing ``app`` executes ``create_app()``, which calls ``db.create_all()``.
During Task F7 a diagnostic ``python -c "from app import app ..."`` was run
while ``DATABASE_URL`` pointed at production Neon, so the import attempted DDL
against the live database. It rolled back, but reaching a production database
through a module import is a hazard that must be designed out, not remembered.

This script therefore:

* **never imports ``app``** — it opens a plain driver connection from
  ``DATABASE_URL`` and nothing else;
* **sets the session READ ONLY** (``psycopg2`` ``set_session(readonly=True)``),
  so the server itself rejects any write for the life of the connection;
* issues only ``SELECT`` against ``pg_catalog`` / ``information_schema``.

If a statement were ever to attempt a write, PostgreSQL would raise
``cannot execute ... in a read-only transaction`` rather than modify anything.

What it reports
---------------
* current database and user (no password, no host, no connection string);
* whether the repository's declared tables exist;
* which relation actually owns a named index (``pg_indexes`` +
  ``pg_class``/``pg_depend``), because an index name in PostgreSQL is
  schema-scoped and can only be attributed through the catalog;
* the F7 and taste-profile specifics this task is diagnosing.

Usage:
    python scripts/check_production_schema_readonly.py
    DATABASE_URL=... python scripts/check_production_schema_readonly.py --index idx_taste_profile_updated
"""
import argparse
import os
import sys

# Tables this repository declares. Kept as data (not imported from models) so
# that running this script can never trigger application initialization.
TABLES_OF_INTEREST = (
    'taste_profile',
    'import_source_mapping',
    'user_taste_profile',
    'user_similarity',
    'diary_entry',
    'media_item',
)

# Index names whose owning relation is in question. Both spellings of the
# taste-profile index are tracked: the legacy `user_taste_profile` index and the
# model-declared one. They must never be owned by the same relation name, and
# PostgreSQL scopes index names per schema, so a collision is invisible until a
# CREATE INDEX fails.
INDEXES_OF_INTEREST = (
    'idx_taste_profile_updated',
    'idx_taste_profile_updated_at',
    'idx_import_mapping_user_source',
    'unique_user_source_mapping',
)


def _connect():
    """A plain driver connection. No app import, no SQLAlchemy engine."""
    try:
        import psycopg2
    except ImportError:  # pragma: no cover
        sys.exit("psycopg2 is required for this diagnostic")

    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit("DATABASE_URL is not set; nothing to inspect.")

    connection = psycopg2.connect(url)
    # Server-enforced read-only for every subsequent statement on this
    # connection. This is the guarantee, not a promise in a docstring.
    connection.set_session(readonly=True, autocommit=False)
    return connection


def _query(connection, sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params or ())
        return cursor.fetchall()


def _one(connection, sql, params=None):
    rows = _query(connection, sql, params)
    return rows[0][0] if rows else None


def _yesno(value):
    return "present" if value else "MISSING"


def _print_identity(connection):
    print("=" * 68)
    print("READ-ONLY production schema diagnostic")
    print("=" * 68)
    print("database          : %s" % _one(connection, "SELECT current_database()"))
    print("user              : %s" % _one(connection, "SELECT current_user"))
    print("server version    : %s" % _one(connection, "SHOW server_version"))
    print("transaction       : READ ONLY = %s"
          % _one(connection, "SHOW transaction_read_only"))
    print()


def _print_tables(connection):
    print("-- declared tables " + "-" * 50)
    for table in TABLES_OF_INTEREST:
        regclass = _one(connection,
                        "SELECT to_regclass(%s)", ("public." + table,))
        print("  %-26s %s" % (table, _yesno(bool(regclass))))
    print()


def _print_index_owners(connection):
    """Which relation actually owns each name of interest.

    An index name in PostgreSQL is schema-scoped, so it can only be attributed
    through the catalog — never inferred from the models, which is how the
    taste_profile collision stayed invisible.
    """
    print("-- indexes: which relation actually owns the name? " + "-" * 19)
    for index in INDEXES_OF_INTEREST:
        rows = _query(
            connection,
            """
            SELECT indexname, tablename, indexdef
              FROM pg_indexes
             WHERE schemaname = 'public' AND indexname = %s
            """, (index,))
        if not rows:
            print("  %s" % index)
            print("      -> no such index in schema 'public'")
            continue
        for indexname, tablename, indexdef in rows:
            print("  %s" % indexname)
            print("      owned by table : %s" % tablename)
            print("      definition    : %s" % indexdef)
            owner = _one(connection,
                         """
                         SELECT t.relname
                           FROM pg_class c
                           JOIN pg_index i ON i.indexrelid = c.oid
                           JOIN pg_class t ON t.oid = i.indrelid
                          WHERE c.relname = %s
                         """, (index,))
            print("      catalog owner : %s"
                  % (owner or "(none - inconsistent)"))


def _print_columns(connection, table):
    print("-- %s columns " % table + "-" * max(1, 50 - len(table)))
    rows = _query(
        connection,
        """
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = %s
         ORDER BY ordinal_position
        """, (table,))
    if not rows:
        print("  (table absent — nothing to list)")
        return
    for name, dtype, nullable in rows:
        print("  %-26s %-14s nullable=%s" % (name, dtype, nullable))


def _print_counts(connection):
    print("-- row counts (SELECT only; proves the table is live) " + "-" * 25)
    for table in ('taste_profile', 'user_taste_profile',
                  'import_source_mapping'):
        if not _one(connection, "SELECT to_regclass(%s)",
                    ("public." + table,)):
            continue
        print("  %-26s %s row(s)"
              % (table, _one(connection,
                             'SELECT COUNT(*) FROM public.%s' % table)))


def report(connection):
    _print_identity(connection)
    _print_tables(connection)
    _print_index_owners(connection)
    print()
    _print_columns(connection, 'taste_profile')
    print()
    _print_columns(connection, 'import_source_mapping')
    print()
    _print_counts(connection)
    print()
    print("No writes were attempted; the session was read-only throughout.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', action='append', default=None,
                        help='extra index name to attribute')
    args = parser.parse_args()
    if args.index:
        global INDEXES_OF_INTEREST
        INDEXES_OF_INTEREST = tuple(args.index)

    connection = _connect()
    try:
        report(connection)
    finally:
        # Roll back rather than commit: nothing was written, and ending the
        # session with an explicit rollback makes that unambiguous.
        connection.rollback()
        connection.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())