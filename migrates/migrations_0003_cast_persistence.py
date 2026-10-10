"""Forward migration 0003 — cast persistence (Feature F9).

Adds the durable local cast evidence that ``api/statistics.py`` and
``api/taste_profile.py`` read instead of fabricating a request-time TMDb call.

Schema added
------------
    person        id, tmdb_person_id (UNIQUE), name, profile_url, source,
                  created_at, updated_at
    media_cast    id, media_item_id -> media_item.id (CASCADE),
                  person_id -> person.id (CASCADE), character,
                  credit_order, created_at, updated_at
                  UNIQUE (media_item_id, person_id)
    media_item    + cast_enriched_at TIMESTAMP
    taste_profile + actor_affinity_json TEXT

``person`` and ``media_cast`` mirror ``director``/``media_director`` exactly
(see models/cast.py for the full rationale, including why uniqueness excludes
``character``). ``cast_enriched_at`` is a SEPARATE marker from
``directors_enriched_at`` because cast is captured for movies AND tv while
directors are movie-only: one shared marker could not express "this show has
cast evidence and will never have director evidence".

``taste_profile.actor_affinity_json`` is part of THIS migration, not an
afterthought. api/taste_profile.py writes that column on every
``compute_profile()``, so a database that received only the two tables would
pass this migration and then fail the startup schema guard on the very next
boot. Additive schema and the code that depends on it ship together, or the
guard is right to refuse.

Relationship to 0001 and 0002
-----------------------------
This migration depends on ``0001_canonical_watched_reconcile`` ONLY. It must
NOT depend on ``0002_remove_legacy_wishlist``, because 0002 is DESTRUCTIVE and
stays deferred until an operator records a verified restorable snapshot. A
dependency on 0002 would mean the entire F9 schema was unreachable while that
one table drop was blocked — additive work would be held hostage to an
unrelated destructive operation. The runner validates dependencies against the
versions applied so far in the same run, so 0001 being present is sufficient.

Why it is additive and therefore ordinary
-----------------------------------------
It creates two tables and adds one nullable column. It drops nothing, rewrites
no user data, and touches no canonical history table (``diary_entry``,
``user_viewed``). So it declares ``DESTRUCTIVE = False`` and takes the ordinary
execution path — no backup approval is needed or accepted for it.

Declared ownership of the new names
-----------------------------------
``CREATES_TABLES`` / ``ADDS_COLUMNS`` are read by
``migrate_schema_convergence.py``. They exist so bounded convergence can tell
"a table is missing and nothing will create it" (refuse — that is unexpected
drift) apart from "a table is missing and a REGISTERED NON-DESTRUCTIVE
migration will create it" (leave it to the runner's ``upgrade`` step, which
records it in the ledger). Convergence deliberately does NOT create these
tables itself: that is how ``schema_migrations`` came to exist outside the
ledger in F8, and re-introducing an unledgered creation path is the exact
failure this architecture was built to prevent.

Index/constraint name collisions
--------------------------------
PostgreSQL index and constraint names are SCHEMA-scoped, not table-scoped, and
a collision fails mid-transaction, rolling back every other statement in the
transaction with it. That is how the documented ``idx_taste_profile_updated``
incident destroyed ``taste_profile`` itself (docs/conventions.md). SQLite
permits duplicate index names, so this cannot be caught by simply running the
migration in the test suite.

So ``_assert_no_name_conflicts`` reads the LIVE catalog before any DDL and
aborts with the exact conflicting names if any name these tables declare is
already owned by a different table. It asks the database, not model metadata:
the historical colliding owner (``user_taste_profile``) is declared by no model
at all, so metadata cannot answer "who owns this name?".

Contract
--------
``run(connection)`` executes inside a transaction owned by the runner. It MUST
NOT commit and MUST NOT open its own transaction — the ledger row is written in
the same transaction, so on PostgreSQL (where DDL is transactional) a failure
here leaves neither schema change nor ledger row behind. See
docs/migration-inventory.md for the SQLite caveat.
"""
from sqlalchemy import inspect

# Declared to the runner. Additive only: no DROP, no data rewrite, no touch to
# canonical watch history. Keeps the ordinary execution path.
DESTRUCTIVE = False
DESTRUCTIVE_REASON = ''

# The tables this migration brings into existence, and the column it adds.
# Read by migrate_schema_convergence.py — see the module docstring.
CREATES_TABLES = ('person', 'media_cast')
ADDS_COLUMNS = ('media_item.cast_enriched_at',
                'taste_profile.actor_affinity_json')


def _models():
    """Import the declared models lazily.

    Lazy so that merely importing this module (the runner does, to read
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
    except KeyError as exc:  # pragma: no cover - would be a model/registry bug
        raise RuntimeError(
            'no model declares table %r; the migration and the models have '
            'drifted apart' % name) from exc


def _declared_names(table):
    """Named index/constraint objects the table declares, as (kind, name)."""
    for index in table.indexes:
        if index.name:
            yield 'index', index.name
    for constraint in table.constraints:
        if constraint.name:
            yield 'constraint', constraint.name


def live_name_owners(inspector):
    """``{index_or_constraint_name: owning_table}`` from the LIVE catalog.

    Read from the database rather than model metadata on purpose: a legacy
    table that already owns a name is declared by no model, so asking the
    models returns "nobody" and the collision only surfaces as a failing
    CREATE INDEX inside a transaction that then rolls back unrelated work.
    """
    owners = {}
    for table_name in inspector.get_table_names():
        try:
            indexes = inspector.get_indexes(table_name)
        except Exception:  # noqa: BLE001 - a view or otherwise odd relation
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

    Computed BEFORE any DDL, because a collision is discovered by a failing
    CREATE INDEX — and that failure aborts the transaction, taking every other
    statement in the migration down with it. Detecting it up front turns an
    unexplained all-or-nothing rollback into a clear message.
    """
    inspector = inspect(connection)
    owners = live_name_owners(inspector)

    conflicts = []
    for name in CREATES_TABLES:
        try:
            table = _table(name)
        except RuntimeError:
            continue
        for kind, declared in _declared_names(table):
            owner = owners.get(declared)
            if owner and owner != table.name:
                conflicts.append((kind, declared, table.name, owner))
    return conflicts


def _assert_no_name_conflicts(connection):
    conflicts = name_conflicts(connection)
    if not conflicts:
        return
    raise RuntimeError(
        'Refusing to create cast tables: %d index/constraint name(s) are '
        'already owned by a DIFFERENT table. PostgreSQL scopes these names to '
        'the schema, so creating them would raise DuplicateTable and roll back '
        'every other statement in this migration. Rename the NEW declaration; '
        'do not edit the legacy table, which existing migrations depend on '
        '(docs/conventions.md).\n  %s'
        % (len(conflicts),
           '\n  '.join('%s %r wanted by %s is already owned by %s'
                       % (kind, name, wanted, owner)
                       for kind, name, wanted, owner in conflicts)))


def _columns(connection, table_name):
    return {c['name'] for c in inspect(connection).get_columns(table_name)}


def _ensure_column(connection, table_name, column_name):
    """ADD COLUMN only when absent, rendered through SQLAlchemy's DDL compiler.

    Compiling ``CreateColumn`` keeps the rendered type dialect-correct instead
    of hard-coding ``TIMESTAMP`` (which happens to be right on PostgreSQL and
    SQLite but is a guess about any future backend).

    Returns 'present' or 'added'.
    """
    if not inspect(connection).has_table(table_name):
        raise RuntimeError(
            'cannot add %s.%s: the table does not exist. This migration adds '
            'cast tables; it does not bootstrap the application schema.'
            % (table_name, column_name))
    if column_name in _columns(connection, table_name):
        return 'present'

    table = _table(table_name)
    column = table.c[column_name]
    from sqlalchemy.schema import CreateColumn
    rendered = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.exec_driver_sql(
        'ALTER TABLE %s ADD COLUMN %s' % (table_name, rendered))
    return 'added'


def run(connection):
    """Create the cast tables and the media-level enrichment marker."""
    print('[0003] cast persistence (additive)')

    # Fail BEFORE any DDL: a name collision discovered mid-transaction would
    # roll back unrelated statements and report an unrelated cause.
    _assert_no_name_conflicts(connection)

    for name in CREATES_TABLES:
        table = _table(name)
        # checkfirst makes the whole migration idempotent at the DDL level, so
        # a partially-applied attempt (or a database bootstrapped from models
        # by scripts/bootstrap_dev_schema.py) converges rather than failing.
        existed = inspect(connection).has_table(name)
        table.create(bind=connection, checkfirst=True)
        print('      table %-12s %s' % (name, 'present' if existed else 'created'))

    for dotted in ADDS_COLUMNS:
        table_name, column_name = dotted.split('.', 1)
        outcome = _ensure_column(connection, table_name, column_name)
        print('      column %-24s %s' % (dotted, outcome))

    print('      no rows written: cast is populated by the offline batch '
          'scripts/enrich_cast.py, never by a migration and never at startup.')
    return {'tables': list(CREATES_TABLES), 'columns': list(ADDS_COLUMNS)}


def verify(connection):
    """Post-apply assertions: the declared schema is actually present.

    Separate from the ledger row on purpose — the runner only records the
    migration as applied if this returns no problems, so a partial apply can
    never be recorded as complete.
    """
    problems = []
    inspector = inspect(connection)

    for name in CREATES_TABLES:
        if not inspector.has_table(name):
            problems.append('table %s is missing after the migration' % name)

    for dotted in ADDS_COLUMNS:
        table_name, column_name = dotted.split('.', 1)
        if not inspector.has_table(table_name):
            problems.append('table %s is missing after the migration'
                            % table_name)
            continue
        if column_name not in _columns(connection, table_name):
            problems.append('column %s is missing after the migration' % dotted)

    # The people must be reachable, not merely present: an association table
    # whose foreign keys were never created would satisfy a has_table() check
    # while silently failing every join that reads it.
    for name in CREATES_TABLES:
        if not inspector.has_table(name):
            continue
        actual = {c['name'] for c in inspector.get_columns(name)}
        wanted = {c.name for c in _table(name).columns}
        missing = wanted - actual
        if missing:
            problems.append('%s is missing column(s): %s'
                            % (name, ', '.join(sorted(missing))))
    return problems