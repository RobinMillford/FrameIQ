"""Forward migration 0005 — ``media_item.runtime`` (Feature F32).

Why this migration exists
-------------------------
``models/media.py`` declares ``runtime = db.Column(db.Integer)`` (runtime in
minutes, nullable). Tables created before that column existed do not have it,
and ``create_all()`` never alters an existing table, so detail-page queries
fail with::

    psycopg2.errors.UndefinedColumn: column media_item.runtime does not exist

The column was repaired in the past by the one-off legacy script
``migrates/migrate_media_runtime.py``, which is **not** a deployment mechanism:
it is not registered, so it produces no ledger row, and the schema guard has no
declared owner for the column it repairs. A deployed database missing this
column therefore had no ledgered remedy — the guard refused to boot and the
only fix was an unrecorded manual script.

Registering the change here closes that gap: the column now has an owner the
runner applies, the ledger records, and the pre-upgrade guard tolerates
(``utils/schema_guard.pending_migration_objects`` reads ``ADDS_COLUMNS``).

Schema added
------------
    media_item  + runtime INTEGER NULL

``NULL`` for every existing row. Nothing is backfilled: a runtime is known only
where enrichment recorded it, and inventing one would be a fabricated value
that downstream statistics would then treat as real. On PostgreSQL a nullable
ADD COLUMN with no default is metadata-only, so this does not rewrite the table.

Scope
-----
Exactly one column, additively. No ``DROP``, no data rewrite, no default, no
backfill, no index, no other column. It touches no canonical watch history
(``diary_entry``, ``user_viewed``), so it declares ``DESTRUCTIVE = False`` and
takes the ordinary execution path.

It also does **not** bootstrap. If ``media_item`` does not exist this refuses:
creating it here would mean one column's migration silently creating the whole
application schema, which is the defect F32 was raised to remove (the same
reason ``0003`` refuses to bootstrap). A fresh database belongs to
``scripts/bootstrap_dev_schema.py``, which is deliberately loud and
target-classified.

Contract
--------
``run(connection)`` executes inside a transaction owned by the runner. It MUST
NOT commit and MUST NOT open its own transaction — the ledger row is written in
the same transaction, so on PostgreSQL (where DDL is transactional) a failure
here leaves neither schema change nor ledger row behind. See
docs/migration-inventory.md for the SQLite caveat, where pysqlite commits before
DDL: the ledger guarantee still holds, but that DDL guarantee is
PostgreSQL-only.
"""
from sqlalchemy import inspect

# Declared to the runner. Additive only: no DROP, no data rewrite, no default,
# no touch to canonical watch history. Keeps the ordinary execution path.
DESTRUCTIVE = False
DESTRUCTIVE_REASON = ''

#: No table is created. This is an ADD COLUMN migration and nothing else.
CREATES_TABLES = ()

#: The single column this migration owns. Read by utils/schema_guard.py so the
#: migration-aware pre-upgrade check tolerates exactly this drift — and nothing
#: else — between deploying the code and running the migration.
ADDS_COLUMNS = ('media_item.runtime',)

_TABLE = 'media_item'
_COLUMN = 'runtime'


def _models():
    """Import the declared models lazily.

    Lazy so that merely importing this module (the runner does, to read
    ``DESTRUCTIVE``) never pulls in the whole model registry.
    """
    import models
    from models.base import db
    return models, db


def _declared_column():
    """The model-declared column object for ``media_item.runtime``.

    Reading the declaration — rather than hard-coding ``INTEGER`` — means the
    DDL can never drift from the model it is repairing. If the model changes,
    this migration follows it and the mismatch surfaces in review, not as a
    runtime type error against production.
    """
    _package, db = _models()
    try:
        return db.metadata.tables[_TABLE].c[_COLUMN]
    except KeyError as exc:  # pragma: no cover - would be a model/registry bug
        raise RuntimeError(
            'no model declares %s.%s; the migration and the models have '
            'drifted apart, so there is nothing to repair'
            % (_TABLE, _COLUMN)) from exc


def _live_column(connection, table_name, column_name):
    """The reflected column object, or ``None`` when it does not exist."""
    for column in inspect(connection).get_columns(table_name):
        if column['name'] == column_name:
            return column
    return None


def _describe(column):
    """Human-readable ``(type, nullability)`` of a reflected column."""
    if column.get('nullable'):
        nullability = 'NULL'
    else:
        nullability = 'NOT NULL'
    return '%s %s' % (str(column['type']).strip().upper(), nullability)


def _expected_description():
    """The ``(type, nullability)`` the model declares, rendered the same way."""
    declared = _declared_column()
    declared_nullability = 'NULL' if declared.nullable else 'NOT NULL'
    return '%s %s' % (str(declared.type).strip().upper(), declared_nullability)


def _is_integer_family(type_):
    """True when ``type_`` stores a Python int.

    Compares the Python type object, not the rendered type name, because the
    same logical type is reported under different names by different backends
    (``INTEGER`` by the SQLite reflector, ``INTEGER`` by PostgreSQL, and
    ``INT``/``INT4``/``INT8`` by hand-written DDL on other engines). Comparing
    names produced spurious mismatches for the ledger in scripts/migrate.py, and
    the fix there was to compare type objects instead.

    SmallInteger/Integer/BigInteger all derive from Integer and all read back
    as ``int``, so all three are accepted: a ``BIGINT`` runtime column is
    schema-drifted but not data-incompatible, and refusing it would block a
    deploy for no safety gain. Everything else — TEXT, VARCHAR, DATETIME,
    NUMERIC, BOOLEAN, FLOAT — is genuinely incompatible and is refused.
    """
    from sqlalchemy import types as sqltypes
    return isinstance(type_, sqltypes.Integer)


def _assert_compatible(connection):
    """Refuse an existing column that is not the one this migration declares.

    Silently accepting a ``runtime`` column of the wrong type would be worse
    than refusing: the column's whole purpose is to hold minutes, and a
    ``VARCHAR`` one would let non-numeric data through, or a ``NOT NULL`` one
    would make every subsequent row insert fail in a way that looks like an
    application bug rather than a schema one.

    This migration is additive by declaration and owns no ``ALTER COLUMN`` or
    ``DROP COLUMN`` operation, so it cannot repair the mismatch itself — and it
    must not: rewriting a column an operator has not inspected is exactly the
    destructive behaviour this migration exists to avoid. It refuses and names
    the remedy instead.
    """
    live = _live_column(connection, _TABLE, _COLUMN)
    if live is None:
        return

    problems = []
    if not _is_integer_family(live['type']):
        problems.append('type is %s, expected an integer type'
                        % str(live['type']).strip().upper())
    if not live.get('nullable'):
        problems.append('the column is NOT NULL, expected NULL so existing '
                        'rows need no value')

    if not problems:
        return

    raise RuntimeError(
        'Refusing to run: %s.%s already exists but is not the column 0005 '
        'declares.\n'
        '  found   : %s\n'
        '  declared: %s\n'
        '  reason  : %s\n'
        '  This is NOT repaired automatically. 0005 declares an ADD COLUMN '
        'only -- it owns no ALTER COLUMN and no DROP COLUMN -- and rewriting a '
        'column an operator has not inspected is precisely the destructive '
        'behaviour this migration exists to avoid.\n'
        '  Inspect the live column first, then either correct it to %s by hand, '
        'or write a separate forward migration for the corrected schema. Do '
        'not delete the ledger row to force this migration to re-run: it has '
        'not run.'
        % (_TABLE, _COLUMN, _describe(live), _expected_description(),
           '; '.join(problems), _expected_description()))


def _add_column(connection):
    """ADD COLUMN, rendered through SQLAlchemy's DDL compiler.

    Compiling ``CreateColumn`` keeps the rendered type dialect-correct instead
    of hard-coding ``INTEGER``. Returns 'added'.
    """
    column = _declared_column()
    from sqlalchemy.schema import CreateColumn
    rendered = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.exec_driver_sql(
        'ALTER TABLE %s ADD COLUMN %s' % (_TABLE, rendered))
    return 'added'


def run(connection):
    """Ensure ``media_item.runtime`` exists. Safe to re-run."""
    print('[0005] media_item.runtime (additive)')

    if not inspect(connection).has_table(_TABLE):
        # Refuse BEFORE any DDL. Bootstrapping here is the defect F32 removed
        # from the legacy script: one column's migration creating the entire
        # application schema.
        raise RuntimeError(
            'Refusing to run: cannot add %s.%s because the %s table does not '
            'exist on this database.\n'
            '  This migration adds ONE column to an EXISTING table. It does not '
            'bootstrap the application schema and it creates nothing else.\n'
            '  * fresh / scratch database -> python '
            'scripts/bootstrap_dev_schema.py, then re-run the upgrade\n'
            '  * deployed database       -> a missing %s is schema drift; '
            'converge it first (see docs/migration-inventory.md), then re-run\n'
            '  Nothing has been applied and no ledger row has been written.'
            % (_TABLE, _COLUMN, _TABLE, _TABLE))

    # Refuse an incompatible existing column BEFORE touching anything.
    _assert_compatible(connection)

    dotted = '%s.%s' % (_TABLE, _COLUMN)
    if _live_column(connection, _TABLE, _COLUMN) is None:
        outcome = _add_column(connection)
    else:
        outcome = 'present'
    print('      column %-24s %s' % (dotted, outcome))

    print('      no rows written: runtime stays NULL for every existing row. A '
          'runtime is known only where enrichment recorded it, so backfilling '
          'one would store a fabricated value that downstream statistics would '
          'treat as real.')
    return {'tables': list(CREATES_TABLES), 'columns': list(ADDS_COLUMNS)}


def verify(connection):
    """Post-apply assertions: the declared column is actually present.

    Separate from the ledger row on purpose — the runner only records the
    migration as applied if this returns no problems, so a partial apply can
    never be recorded as complete.
    """
    problems = []

    if not inspect(connection).has_table(_TABLE):
        problems.append('table %s is missing after the migration' % _TABLE)
        return problems

    live = _live_column(connection, _TABLE, _COLUMN)
    if live is None:
        problems.append('column %s.%s is missing after the migration'
                        % (_TABLE, _COLUMN))
        return problems

    if not _is_integer_family(live['type']):
        problems.append('column %s.%s is %s after the migration, expected an '
                        'integer type' % (_TABLE, _COLUMN,
                                          str(live['type']).strip().upper()))
    if not live.get('nullable'):
        problems.append('column %s.%s is NOT NULL after the migration, expected '
                        'NULL' % (_TABLE, _COLUMN))
    return problems
