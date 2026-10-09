"""Schema parity guard — read-only drift detection at startup.

Prevents the class of production incident where code ships referencing a
model column that the database does not have (e.g. ``media_item.runtime``),
because ``db.create_all()`` creates NEW tables but never ALTERs existing
ones. In that state the app used to boot "successfully" and then serve
broken movie/TV pages.

This module is STRICTLY READ-ONLY. It never issues DDL/DML — its job is:

    detect drift → fail safely → tell the operator exactly what is missing

It compares SQLAlchemy's declared model metadata against the LIVE database
schema (via SQLAlchemy Inspector) and reports:

  - missing TABLES (declared by a model, absent in the database)
  - missing COLUMNS (declared on a model, absent on the existing table)

Intentionally NOT compared (false-positive sources):
  - exact DB-specific type formatting / dialect type rendering
  - indexes and constraints (implementation detail, not reliably comparable)
  - PostgreSQL internal metadata (sequences, ownership, etc.)
Extra database columns are tolerated: legacy/manual columns must not
block startup.

Operator escape hatch: ``SKIP_SCHEMA_GUARD=1`` disables enforcement for
the explicit case of running migration scripts against a known-drifted
database (``migrates/*.py`` import the app, which would otherwise refuse
to boot). Local development is unaffected — the guard passes whenever the
schema is compatible, and local DBs are recreated trivially.

Usage:
    from utils.schema_guard import ensure_schema_compatible
    ensure_schema_compatible()          # raises SchemaMismatchError on drift

    python -m utils.schema_guard        # read-only operator CLI
"""
import os

from sqlalchemy.inspection import inspect

# Type guard is intentionally loose on purpose: we compare STRUCTURE
# (table/column presence), never types — see module docstring.


class SchemaMismatchError(RuntimeError):
    """Raised at startup when the live DB schema cannot serve the models."""


def _declared_schema():
    """Model-declared requirements: {table_name: [column names...]}."""
    # Imported lazily: keeps this module importable before the app model
    # registry in exotic orderings and avoids import cycles.
    from models import db  # noqa: WPS433 (runtime import is deliberate)

    declared = {}
    for table in db.metadata.sorted_tables:
        declared[table.name] = [c.name for c in table.columns]
    return declared


def _live_schema(engine):
    """Live database requirements: {table_name: [column names...]}."""
    inspector = inspect(engine)
    live = {}
    for table_name in inspector.get_table_names():
        live[table_name] = [c["name"] for c in inspector.get_columns(table_name)]
    return live


def _load_migration(spec):
    """Import one registered migration module, or return None.

    Two import paths are attempted because callers reach this helper from three
    different working directories, and getting this wrong fails SILENTLY in the
    dangerous direction — an unimportable module claims no ownership, so the
    guard reverts to strict and the deploy aborts on the very objects the next
    step creates:

      * ``migrates.<module>`` — works when the repo root is importable, which is
        the case for ``python -m utils.schema_guard`` and for convergence;
      * ``<module>`` with ``migrates/`` prepended to ``sys.path`` — the form
        ``scripts/migrate.py`` uses, and the only one available to a caller
        whose cwd is ``migrates/``.

    Returning None on failure is deliberate and fail-closed: an unreadable
    migration must never be treated as owning anything.
    """
    import importlib
    import os
    import sys

    bare = spec.module.split('.')[-1]
    for dotted in ('migrates.%s' % bare, bare):
        try:
            return importlib.import_module(dotted)
        except ImportError:
            continue

    migrates_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrates')
    if os.path.isdir(migrates_dir) and migrates_dir not in sys.path:
        sys.path.insert(0, migrates_dir)
    try:
        return importlib.import_module(bare)
    except ImportError:
        return None


def _applied_versions(engine):
    """Versions recorded in the ledger, or an empty set when unknowable.

    An unreadable or absent ledger returns an empty set, which makes every
    registered non-destructive migration count as PENDING — the safe direction,
    because unrelated drift is fatal either way and the post-upgrade strict
    guard is what actually gates serving.
    """
    from sqlalchemy import inspect as _inspect
    from sqlalchemy import text as _text

    try:
        if not _inspect(engine).has_table('schema_migrations'):
            return set()
        # SQLAlchemy 2.0 removed Engine.execute(); a Connection is required.
        # Deliberately no explicit transaction — this is a read-only report.
        with engine.connect() as connection:
            return {row[0] for row in connection.execute(_text(
                'SELECT version FROM schema_migrations'))}
    except Exception:  # noqa: BLE001 - unknowable ledger != "everything applied"
        return set()


def _owned_by(module):
    """(tables, columns) a single migration module declares it owns."""
    tables = set(getattr(module, 'CREATES_TABLES', ()) or ())
    columns = set()
    for dotted in getattr(module, 'ADDS_COLUMNS', ()) or ():
        table, _, column = str(dotted).partition('.')
        if table and column:
            columns.add((table, column))
    return tables, columns


def pending_migration_objects(engine):
    """Objects owned by registered, PENDING, non-destructive migrations.

    Why this exists
    ---------------
    A release that adds a model declares schema the database does not have yet.
    The migration runner creates it — but the deploy sequence runs a guard
    BEFORE the runner, so that guard would abort the deploy on exactly the
    objects the very next step is about to create. (This is what happened to F9:
    ``person`` / ``media_cast`` / ``media_item.cast_enriched_at`` /
    ``taste_profile.actor_affinity_json``.) The pre-upgrade guard therefore needs
    to know which missing objects are *expected*, without becoming lenient about
    anything else.

    Ownership is declared by the migration itself, in ``CREATES_TABLES`` and
    ``ADDS_COLUMNS`` — the same declarations ``migrate_schema_convergence.py``
    reads, so the two can never disagree about who owns what.

    Narrow by construction. An object is tolerated ONLY when all of these hold:

    1. its migration is REGISTERED (it exists in the ledger's registry);
    2. that migration is NOT DESTRUCTIVE — a deferred legacy ``DROP`` never
       grants permission to skip a check;
    3. that migration is NOT YET APPLIED — if it is recorded and its object is
       still missing, that is genuine drift and stays fatal;
    4. the migration module actually imports — an unreadable module claims
       nothing, so its objects stay fatal too.

    Every branch that cannot prove ownership returns "not owned", which keeps
    the guard strict. Used ONLY by the pre-upgrade operator step; the
    in-application guard is never tolerant.
    """
    try:
        from migrates import registry
    except ImportError:  # pragma: no cover - registry ships with the repo
        return {'tables': frozenset(), 'columns': frozenset()}

    applied = _applied_versions(engine)
    owned_tables, owned_columns = set(), set()

    for spec in registry.ordered_migrations():
        if spec.version in applied:
            continue  # already applied: its objects must exist, or it is drift
        module = _load_migration(spec)
        if module is None:
            continue  # cannot prove ownership -> stays fatal
        if getattr(module, 'DESTRUCTIVE', False):
            continue  # a destructive migration never excuses a missing object
        tables, columns = _owned_by(module)
        owned_tables |= tables
        owned_columns |= columns

    return {'tables': frozenset(owned_tables),
            'columns': frozenset(owned_columns)}


def check_schema(engine, allow_pending_migrations=False):
    """Compare declared model metadata with the live schema.

    Returns a dict:
        {"ok": bool, "missing_tables": [...], "missing_columns": {table: [cols]}}
        plus, when ``allow_pending_migrations`` is set, ``pending_tables`` and
        ``pending_columns`` naming the drift a pending migration owns.

    Read-only: the only database operations are Inspector introspections and a
    single ledger SELECT.

    ``allow_pending_migrations`` must never be set on the in-application startup
    path. It exists for the one pre-upgrade deploy step, where the missing
    objects are applied moments later; an application must never boot on an
    incomplete schema.
    """
    declared = _declared_schema()
    live = _live_schema(engine)

    missing_tables = sorted(t for t in declared if t not in live)
    missing_columns = {}
    for table, columns in sorted(declared.items()):
        if table not in live:
            continue  # already reported as a missing table
        absent = [c for c in columns if c not in live[table]]
        if absent:
            missing_columns[table] = sorted(absent)

    pending_tables, pending_columns = [], {}
    if allow_pending_migrations:
        owned = pending_migration_objects(engine)
        pending_tables = [t for t in missing_tables if t in owned['tables']]
        missing_tables = [t for t in missing_tables if t not in owned['tables']]
        for table, columns in list(missing_columns.items()):
            owned_here = {c for (t, c) in owned['columns'] if t == table}
            tolerated = [c for c in columns if c in owned_here]
            if tolerated:
                pending_columns[table] = tolerated
                remaining = [c for c in columns if c not in owned_here]
                if remaining:
                    missing_columns[table] = remaining
                else:
                    del missing_columns[table]

    return {
        "ok": not missing_tables and not missing_columns,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "pending_tables": pending_tables,
        "pending_columns": pending_columns,
    }


def format_mismatch(report, allow_pending_migrations=False):
    """Human-readable, operator-facing drift report."""
    lines = ["Database schema mismatch detected."]
    if report["missing_tables"]:
        lines.append("Missing tables:")
        lines += ["  - %s" % t for t in report["missing_tables"]]
    if report["missing_columns"]:
        lines.append("Missing columns:")
        for table, columns in report["missing_columns"].items():
            lines += ["  - %s.%s" % (table, c) for c in columns]
    if allow_pending_migrations:
        pending_tables = report.get("pending_tables") or []
        pending_columns = report.get("pending_columns") or {}
        if pending_tables or pending_columns:
            lines.append("Pending a registered migration (NOT drift):")
            lines += ["  - table %s" % t for t in pending_tables]
            for table, columns in pending_columns.items():
                lines += ["  - column %s.%s" % (table, c) for c in columns]
    lines.append("Run the appropriate migration before starting the application.")
    lines.append("(Read-only check — see migrates/ for idempotent scripts.)")
    return "\n".join(lines)


def ensure_schema_compatible(app=None):
    """Raise :class:`SchemaMismatchError` if the DB cannot serve the models.

    Read-only, single introspection pass, no caching between calls — it is
    meant to run once at application startup and once per operator CLI run.

    Honors the ``SKIP_SCHEMA_GUARD=1`` escape hatch (explicitly set for
    migration runs against a known-drifted database).
    """
    if os.getenv("SKIP_SCHEMA_GUARD") == "1":
        return None

    if app is None:
        from app import app as flask_app
        app = flask_app

    with app.app_context():
        from models import db
        report = check_schema(db.engine)

    if not report["ok"]:
        raise SchemaMismatchError(format_mismatch(report))
    return report


def main(argv=None):
    """Operator CLI: print 'Schema OK' or the drift list; exit 0/1.

    Self-exempt from the in-app guard BEFORE importing the app: this CLI
    exists to REPORT drift, so it must be able to boot against a drifted
    database and print the report instead of crashing at import time.
    Still strictly read-only.

    ``--allow-pending-migrations`` is for ONE deploy step only: the pre-upgrade
    check, where the missing objects are applied by the runner moments later.
    It tolerates only objects a registered, pending, non-destructive migration
    declares it owns. Every other missing table or column remains fatal, and the
    application startup guard never accepts this flag.
    """
    import argparse

    import sys

    parser = argparse.ArgumentParser(
        prog='python -m utils.schema_guard',
        description='Read-only schema parity report. Never writes.')
    parser.add_argument(
        '--allow-pending-migrations', action='store_true',
        help='tolerate ONLY the objects a registered, pending, '
             'non-destructive migration declares it owns (pre-upgrade step)')
    args = parser.parse_args(argv)

    os.environ["SKIP_SCHEMA_GUARD"] = "1"
    from app import app

    with app.app_context():
        from models import db
        report = check_schema(db.engine,
                              allow_pending_migrations=(
                                  args.allow_pending_migrations))

    if report["ok"]:
        pending = report.get("pending_tables") or []
        pending += ["%s.%s" % (t, c)
                    for t, cols in (report.get("pending_columns") or {}).items()
                    for c in cols]
        if pending:
            print("Schema OK — no unexpected drift. Pending a registered "
                  "migration (applied by `scripts/migrate.py upgrade`):")
            for name in pending:
                print("  - %s" % name)
        else:
            print("Schema OK — database matches declared models.")
        return 0
    print(format_mismatch(report,
                          allow_pending_migrations=args.allow_pending_migrations),
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
