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


def check_schema(engine):
    """Compare declared model metadata with the live schema.

    Returns a dict:
        {"ok": bool, "missing_tables": [...], "missing_columns": {table: [cols]}}
    Read-only: the only database operations are Inspector introspections.
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

    return {
        "ok": not missing_tables and not missing_columns,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
    }


def format_mismatch(report):
    """Human-readable, operator-facing drift report."""
    lines = ["Database schema mismatch detected."]
    if report["missing_tables"]:
        lines.append("Missing tables:")
        lines += [f"  - {t}" for t in report["missing_tables"]]
    if report["missing_columns"]:
        lines.append("Missing columns:")
        for table, columns in report["missing_columns"].items():
            lines += [f"  - {table}.{c}" for c in columns]
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


def main():
    """Operator CLI: print 'Schema OK' or the drift list; exit 0/1.

    Self-exempt from the in-app guard BEFORE importing the app: this CLI
    exists to REPORT drift, so it must be able to boot against a drifted
    database and print the report instead of crashing at import time.
    Still strictly read-only.
    """
    import sys

    os.environ["SKIP_SCHEMA_GUARD"] = "1"
    from app import app

    with app.app_context():
        from models import db
        report = check_schema(db.engine)

    if report["ok"]:
        print("Schema OK — database matches declared models.")
        return 0
    print(format_mismatch(report), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
