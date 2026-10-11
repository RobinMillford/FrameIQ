"""Forward migration 0004 — notification preferences (Feature F12).

Schema added
------------
    user_notification_preference  id, user_id -> user.id (UNIQUE),
                                  new_episode BOOLEAN NOT NULL DEFAULT 1,
                                  season_premiere BOOLEAN NOT NULL DEFAULT 0,
                                  created_at, updated_at

One row per user, enforced by UNIQUE(user_id), so concurrent first-saves
cannot fork the record into two.

Why no backfill of existing users
---------------------------------
``api/notification_preferences.get_preferences`` resolves a MISSING row to the
documented DEFAULTS instead of creating one, so reading preferences never
writes. Existing users therefore keep their pre-F12 behaviour (new-episode
notifications on) with no data migration and no startup DDL — which is why this
migration creates the table and populates nothing.

Relationship to 0001 and 0002
-----------------------------
This migration depends on ``0001_canonical_watched_reconcile`` ONLY. It must
NOT depend on ``0002_remove_legacy_wishlist``: 0002 is DESTRUCTIVE and stays
deferred until an operator records a verified restorable snapshot. Depending
on it would hold a purely additive preference table hostage to an unrelated
blocked DROP — exactly the mistake 0003 was written to avoid.

Why it is additive and therefore ordinary
-----------------------------------------
It creates one table, adds no column to an existing table, drops nothing,
rewrites no user data, and touches no canonical history table (``diary_entry``,
``user_viewed``). So it declares ``DESTRUCTIVE = False`` and takes the ordinary
execution path.

Contract
--------
``run(connection)`` executes inside a transaction owned by the runner. It MUST
NOT commit and MUST NOT open its own transaction.
"""
from sqlalchemy import inspect

# Declared to the runner. Additive only: no DROP, no data rewrite, no touch to
# canonical watch history.
DESTRUCTIVE = False
DESTRUCTIVE_REASON = ''

#: The table this migration brings into existence. Read by
#: utils/schema_guard.py (via migrate_schema_convergence) so the
#: migration-aware pre-upgrade check tolerates exactly this drift and nothing
#: else between deploying the code and running the migration.
CREATES_TABLES = ('user_notification_preference',)

#: Columns added to pre-existing tables. None for this migration.
ADDS_COLUMNS = ()


def _models():
    """Import the declared models lazily.

    Lazy so merely importing this module (the runner does, to read
    ``DESTRUCTIVE``) never pulls in the whole model registry.
    """
    import models
    from models.base import db
    return models, db


def _table(name):
    """The single declared table object for ``name``."""
    _package, db = _models()
    try:
        return db.metadata.tables[name]
    except KeyError as exc:  # pragma: no cover - a model/registry bug
        raise RuntimeError(
            'no model declares table %r; the migration and the models have '
            'drifted apart' % name) from exc


def _declared_names(table):
    """Named index/constraint objects the table declares, as (kind, name)."""
    names = []
    for index in table.indexes:
        if index.name:
            names.append(('index', index.name))
    for constraint in table.constraints:
        if constraint.name:
            names.append(('constraint', constraint.name))
    return names


def live_name_owners(inspector):
    """``{index_or_constraint_name: owning_table}`` from the LIVE catalog."""
    owners = {}
    for table_name in inspector.get_table_names():
        try:
            indexes = inspector.get_indexes(table_name)
        except Exception:  # noqa: BLE001 - a view or other odd relation
            indexes = []
        for index in indexes:
            if index.get('name'):
                owners.setdefault(index['name'], table_name)
        try:
            uniques = inspector.get_unique_constraints(table_name)
        except Exception:  # noqa: BLE001
            uniques = []
        for constraint in uniques:
            if constraint.get('name'):
                owners.setdefault(constraint['name'], table_name)
    return owners


def name_conflicts(connection):
    """``[(kind, name, wanted_table, owner_table)]`` for names already taken.

    A conflict is a name this migration declares that a DIFFERENT table already
    owns. The table existing already is not a conflict — ``checkfirst=True``
    below makes creation idempotent, which is what lets the migration converge
    on a database bootstrapped from models by
    scripts/bootstrap_dev_schema.py.
    """
    inspector = inspect(connection)
    owners = live_name_owners(inspector)

    conflicts = []
    for name in CREATES_TABLES:
        table = _table(name)
        for kind, declared in _declared_names(table):
            owner = owners.get(declared)
            if owner and owner != table.name:
                conflicts.append((kind, declared, table.name, owner))
    return conflicts


def _assert_no_name_conflicts(connection):
    conflicts = name_conflicts(connection)
    if conflicts:
        raise RuntimeError(
            '0004 refuses to run: %s' % '; '.join(
                '%s %r wanted by %s is already owned by %s' % c
                for c in sorted(conflicts, key=lambda x: (x[0], x[1]))))


def _apply_server_defaults(table):
    """Mirror the model's column defaults as server defaults.

    The model default is what ORM inserts use; a server default means a row
    written by any other client (a raw migration, a console) still lands on the
    documented behaviour rather than on NULL.
    """
    for column in table.columns:
        if column.server_default is not None or column.default is None:
            continue
        value = getattr(column.default, 'arg', None)
        if isinstance(value, bool):
            column.server_default = '1' if value else '0'


def run(connection):
    """Create the preference table if absent. Safe to re-run."""
    print('[0004] notification preferences (additive)')

    # Fail BEFORE any DDL.
    _assert_no_name_conflicts(connection)

    for name in CREATES_TABLES:
        table = _table(name)
        _apply_server_defaults(table)
        existed = inspect(connection).has_table(name)
        table.create(bind=connection, checkfirst=True)
        print('      table %-32s %s' % (name, 'present' if existed else 'created'))

    print('      no rows written: preferences resolve to documented defaults '
          'for users who never saved any, so no backfill is needed.')
    return {'tables': list(CREATES_TABLES), 'columns': list(ADDS_COLUMNS)}


def verify(connection):
    """Post-apply assertions: the declared schema is actually present.

    Returns a list of problems (empty = converged). Separate from the ledger
    row on purpose — the runner only records the migration as applied if this
    returns no problems, so a partial apply can never be recorded as complete.
    """
    problems = []
    inspector = inspect(connection)

    for name in CREATES_TABLES:
        if not inspector.has_table(name):
            problems.append('table %s is missing after the migration' % name)
            continue
        actual = {c['name'] for c in inspector.get_columns(name)}
        wanted = {c.name for c in _table(name).columns}
        missing = wanted - actual
        if missing:
            problems.append('%s is missing column(s): %s'
                            % (name, ', '.join(sorted(missing))))
    return problems