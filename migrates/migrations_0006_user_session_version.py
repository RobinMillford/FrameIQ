"""Registered migration ``0006_user_session_version`` (Feature F55).

Adds ``user.session_version`` — the only server-side handle this application
has for revoking authentication credentials.

Why a column is needed
----------------------
Flask's session is a client-side signed cookie and Flask-Login's remember-me
cookie is a signed payload containing nothing but the user id. Neither is
recorded server-side, so there is no session row to delete when a password
changes. The value of a per-user counter is embedded in both cookies by
``utils.session_auth.session_identity`` and compared in the ``user_loader``, so
raising it invalidates every outstanding session and remember-me cookie at once.

Purely additive: ``INTEGER NOT NULL DEFAULT 0``. Existing rows read 0, which is
not the version any pre-F55 cookie carries — those cookies are rejected as
un-versioned, logging each existing user out exactly once on deploy. That is the
intended fail-closed behaviour and is not a data hazard: no row is read or
rewritten, and the operation is safe to re-run.

Like every other registered migration this is a library module. The runner owns
the transaction, the ledger row and the destructive gate; this module owns the
ADD COLUMN and the shape assertions.
"""
from sqlalchemy import inspect

# Declared to the runner. Additive only: no DROP, no data rewrite.
DESTRUCTIVE = False
DESTRUCTIVE_REASON = ''

#: No table is created. This is an ADD COLUMN migration and nothing else.
CREATES_TABLES = ()

#: The single column this migration owns. Read by utils/schema_guard.py so the
#: migration-aware pre-upgrade check tolerates exactly this drift — and nothing
#: else — between deploying the code and running the migration.
ADDS_COLUMNS = ('user.session_version',)

_TABLE = 'user'
_COLUMN = 'session_version'


def _models():
    """Import the declared models lazily.

    Lazy so that merely importing this module (the runner does, to read
    ``DESTRUCTIVE``) never pulls in the whole model registry.
    """
    import models
    from models.base import db
    return models, db


def _declared_column():
    """The model-declared column object for ``user.session_version``.

    Reading the declaration rather than hard-coding ``INTEGER`` means the DDL
    cannot drift from the model it repairs. If the model changes, this follows
    and the mismatch surfaces in review rather than as a runtime type error
    against production.
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
    """The reflected column dict, or ``None`` when it does not exist."""
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


def _server_default_of(column):
    """The reflected DEFAULT, whichever key the backend reports it under.

    This is a real dialect difference, not a cosmetic one. SQLAlchemy's SQLite
    reflector returns the parsed literal under ``default`` and omits
    ``server_default`` entirely; PostgreSQL returns a ``DefaultClause`` under
    ``server_default`` and leaves ``default`` unset. Asserting on one key alone
    would make this migration report a correctly-shaped SQLite column as broken
    while passing on PostgreSQL — the exact SQLite/PostgreSQL divergence that
    has bitten this project before.
    """
    for key in ('server_default', 'default'):
        value = column.get(key)
        if value is not None:
            return value
    return None


def _has_default(column):
    """True when the column carries a DEFAULT clause at all."""
    return _server_default_of(column) is not None


def _expected_description():
    """The shape the model declares, rendered the same way."""
    declared = _declared_column()
    declared_nullability = 'NULL' if declared.nullable else 'NOT NULL'
    return '%s %s' % (str(declared.type).strip().upper(), declared_nullability)


def _is_integer_family(type_):
    """True when ``type_`` stores a Python int.

    Compares the Python type object rather than the rendered name, because
    backends report the same logical type under different names (``INTEGER``
    from the SQLite reflector, ``INTEGER``/``INT4`` from PostgreSQL). All three
    integer widths read back as ``int``, so all three are accepted.
    """
    from sqlalchemy import types as sqltypes
    return isinstance(type_, sqltypes.Integer)


def _assert_compatible(connection):
    """Refuse an existing column that is not the one this migration declares.

    Unlike 0005, this column is NOT NULL with a default, so the failure modes
    are the mirror image and both are worth refusing:

      * a non-integer column would compare unequal to every issued cookie and
        lock the account out entirely;
      * a NULLABLE column would let ``session_version`` be NULL, which
        compares unequal to every cookie — the same lockout, reached silently
        by any code path that forgets the column.

    A missing server default is equally fatal: an insert that omits the column
    would raise rather than defaulting to 0.

    This migration is additive by declaration and owns no ``ALTER COLUMN``, so
    it cannot repair the mismatch itself — and must not: rewriting a column an
    operator has not inspected is the destructive behaviour this architecture
    exists to prevent.
    """
    live = _live_column(connection, _TABLE, _COLUMN)
    if live is None:
        return

    problems = []
    if not _is_integer_family(live['type']):
        problems.append('type is %s, expected an integer type'
                        % str(live['type']).strip().upper())
    if live.get('nullable'):
        problems.append('the column is NULLABLE, expected NOT NULL — a null '
                        'version compares unequal to every issued cookie and '
                        'would lock the account out')
    if not _has_default(live):
        problems.append('the column has no DEFAULT, expected DEFAULT 0 '
                        '— inserts that omit it would fail')

    if not problems:
        return

    raise RuntimeError(
        'Refusing to run: %s.%s already exists but is not the column 0006 '
        'declares.\n'
        '  found   : %s\n'
        '  declared: %s\n'
        '  reason  : %s\n'
        '  This is NOT repaired automatically. 0006 declares an ADD COLUMN '
        'only -- it owns no ALTER COLUMN and no DROP COLUMN -- and rewriting a '
        'column an operator has not inspected is precisely the destructive '
        'behaviour this migration exists to avoid.\n'
        '  Inspect the live column first, then either correct it to %s by hand, '
        'or write a separate forward migration for the corrected schema. Do '
        'not delete the ledger row to force this migration to re-run: it has '
        'not run.'
        % (_TABLE, _COLUMN, _describe(live), _expected_description(),
           '; '.join(problems), _expected_description()))


def _quoted_table(dialect):
    """``_TABLE`` as the target dialect requires it written.

    ``user`` is a RESERVED word in PostgreSQL, so the unquoted form this used to
    emit (``ALTER TABLE user ADD COLUMN ...``) is a syntax error there — the
    migration could not run on the production engine at all. The bug only
    surfaced in the PostgreSQL suite; the SQLite suite accepted the bare
    identifier, which is why the offline gate stayed green.

    Hand-quoting would be the wrong fix: ``"user"`` happens to be valid on
    SQLite too, but hard-coding one dialect's quoting into a statement built
    for several engines invites the next mismatch in the other direction.
    SQLAlchemy's ``IdentifierPreparer`` already knows each dialect's reserved
    words, so asking it is both correct and self-maintaining — PostgreSQL gets
    ``"user"``, SQLite and MySQL get ``user``.

    Takes the dialect rather than the connection so the statement can be built,
    and asserted, for any engine without one being present.
    """
    return dialect.identifier_preparer.quote(_TABLE)


def _add_column_statement(dialect):
    """The exact ``ALTER TABLE ... ADD COLUMN`` this migration sends.

    Compiling ``CreateColumn`` keeps the rendered type and the server default
    dialect-correct instead of hand-writing ``INTEGER NOT NULL DEFAULT 0``,
    which is wrong for at least one engine this app might be pointed at. The
    table name is prepared the same way, for the reserved-word reason given in
    ``_quoted_table``.

    Split out from ``_add_column`` so the statement can be asserted for several
    dialects with no database present. That is the only way the offline suite
    could catch the reserved-word defect at all, since SQLite tolerates the bare
    identifier PostgreSQL rejects.
    """
    column = _declared_column()
    from sqlalchemy.schema import CreateColumn
    rendered = str(CreateColumn(column).compile(dialect=dialect))
    return 'ALTER TABLE %s ADD COLUMN %s' % (_quoted_table(dialect), rendered)


def _add_column(connection):
    """Execute the ADD COLUMN against ``connection``. Idempotent by contract."""
    connection.exec_driver_sql(_add_column_statement(connection.dialect))
    return 'added'


def run(connection):
    """Ensure ``user.session_version`` exists. Safe to re-run."""
    print('[0006] user.session_version (additive)')

    if not inspect(connection).has_table(_TABLE):
        # Refuse BEFORE any DDL. Bootstrapping here would be the defect F32
        # removed from the legacy scripts: one column's migration creating the
        # entire application schema.
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
    print('      no rows rewritten: existing users read version 0, which is '
          'what the code issues until they log in again. Cookies minted '
          'before this migration carry no version and are rejected, so every '
          'existing session is revoked once — intended, not a data loss.')
    return {'tables': list(CREATES_TABLES), 'columns': list(ADDS_COLUMNS)}


def verify(connection):
    """Post-apply assertions: the declared column is actually usable.

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
    if live.get('nullable'):
        problems.append('column %s.%s is NULLABLE after the migration, expected '
                        'NOT NULL' % (_TABLE, _COLUMN))
    if not _has_default(live):
        problems.append('column %s.%s has no DEFAULT after the migration, '
                        'expected DEFAULT 0' % (_TABLE, _COLUMN))
    return problems