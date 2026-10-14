"""Registered migration ``0005_media_runtime`` (Feature F32).

Why this suite exists
---------------------
``models/media.py`` declares ``runtime = db.Column(db.Integer)``, but tables
created before that column existed do not have it, and ``create_all()`` never
alters an existing table — so detail queries fail with ``UndefinedColumn``.

The column used to be repaired only by ``migrates/migrate_media_runtime.py``,
a one-off legacy script that is not registered: it writes no ledger row, and
``utils/schema_guard`` had no declared owner for the column it repairs. A
deployed database missing the column therefore had no ledgered remedy.

0005 closes that gap. What must not regress:

1. it adds exactly one nullable integer column, and nothing else;
2. it is safe to re-run and never duplicates the column;
3. existing rows survive, with ``runtime`` left NULL;
4. a missing ``media_item`` is refused, not bootstrapped;
5. an existing but INCOMPATIBLE ``runtime`` column is refused, not accepted;
6. the ledger records 0005 only when it genuinely converged;
7. a failure produces no successful-looking ledger record;
8. the schema guard's ownership rules still apply to it.

All tests are hermetic: throwaway SQLite files under ``tmp_path``, no
production target, no network.
"""
import os
import sys

import pytest
from sqlalchemy import create_engine, inspect, text


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'migrates'))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import migrate as runner  # noqa: E402
import migrations_0005_media_runtime as migration  # noqa: E402
from migrates import registry  # noqa: E402


VERSION = '0005_media_runtime'

# A media_item shaped the way it was before Feature 05 added ``runtime``:
# enough columns to be realistic, and none of them named ``runtime``.
_LEGACY_MEDIA_ITEM = """
    CREATE TABLE media_item (
        id INTEGER PRIMARY KEY,
        tmdb_id INTEGER NOT NULL,
        media_type TEXT NOT NULL,
        title TEXT NOT NULL
    )
"""


# ── helpers ───────────────────────────────────────────────────────────────────


def _bare_engine(tmp_path, name, *statements):
    """An empty SQLite file with ``statements`` applied, and nothing else."""
    engine = create_engine('sqlite:///%s' % (tmp_path / name))
    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
    return engine


def _legacy_engine(tmp_path, name='legacy.db', runtime_ddl=None):
    """A database whose ``media_item`` predates ``runtime``."""
    statements = [_LEGACY_MEDIA_ITEM]
    if runtime_ddl:
        statements.append('ALTER TABLE media_item ADD COLUMN %s' % runtime_ddl)
    return _bare_engine(tmp_path, name, *statements)


def _full_schema_engine(tmp_path, name='full.db'):
    """The declared schema, then ``runtime`` dropped to simulate pre-0005.

    Built from model metadata so the rest of the schema is real — the runner
    applies 0001/0003/0004 first, and those need their own tables present.
    ``ALTER TABLE ... DROP COLUMN`` is used rather than a hand-written table so
    the only difference from a live database is the missing column.
    """
    from models.base import db
    engine = create_engine('sqlite:///%s' % (tmp_path / name))
    db.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE media_item DROP COLUMN runtime')
        connection.exec_driver_sql(
            "INSERT INTO media_item (tmdb_id, media_type, title) "
            "VALUES (17287, 'tv', 'Legacy Show')")
    return engine


def _runtime_column(engine):
    for column in inspect(engine).get_columns('media_item'):
        if column['name'] == 'runtime':
            return column
    return None


def _ledger_rows(engine):
    with engine.connect() as connection:
        return {row[0]: (row[1], row[2]) for row in connection.execute(
            text('SELECT version, checksum, kind FROM schema_migrations'))}


def _mark_applied(engine, version):
    """Record a migration without running it.

    0005 depends on 0001, and the runner refuses a pending migration whose
    dependencies are unmet. Recording 0001 lets these tests exercise 0005 in
    isolation on a database that deliberately holds only ``media_item`` —
    rather than dragging 0001's data reconciliation in, which needs tables
    these fixtures do not create.
    """
    with engine.begin() as connection:
        runner.ensure_ledger_table(connection)
        connection.execute(
            text('INSERT INTO schema_migrations '
                 '(version, checksum, kind, applied_at, execution_ms) VALUES '
                 "(:v, :c, 'migration', CURRENT_TIMESTAMP, 0)"),
            {'v': version,
             'c': runner.checksum_for(registry.by_version()[version])})


def _apply_0005_only(engine):
    """Apply 0005 and nothing else."""
    _mark_applied(engine, '0001_canonical_watched_reconcile')
    return runner.apply_pending(engine, only={VERSION})


# ── declared contract ─────────────────────────────────────────────────────────


def test_registry_declares_0005_and_stays_valid():
    assert registry.validate() == []
    versions = [spec.version for spec in registry.ordered_migrations()]
    assert VERSION in versions


def test_0005_depends_on_0001_only_so_deferred_0002_cannot_block_it():
    """Same reasoning as 0003/0004.

    0002 is destructive and stays deferred until an operator records a verified
    restorable snapshot. A purely additive column migration must not be held
    hostage to that unrelated blocked DROP.
    """
    spec = registry.by_version()[VERSION]
    assert spec.depends_on == ('0001_canonical_watched_reconcile',)


def test_0005_owns_exactly_one_column_and_creates_no_table():
    """A migration that creates a table would be able to bootstrap."""
    assert migration.ADDS_COLUMNS == ('media_item.runtime',)
    assert migration.CREATES_TABLES == ()


def test_0005_is_additive_and_transactable():
    """Non-destructive keeps it off the approval-gated path.

    ``NON_TRANSACTIONAL`` is absent/false because the runner owns the
    transaction; declaring it would make the runner refuse outright.
    """
    assert migration.DESTRUCTIVE is False
    assert getattr(migration, 'NON_TRANSACTIONAL', False) is False


def test_0005_exposes_run_and_verify():
    assert callable(migration.run)
    assert callable(migration.verify)


# ── 1. missing column ─────────────────────────────────────────────────────────


def test_missing_runtime_column_is_added_as_nullable_integer(tmp_path):
    engine = _legacy_engine(tmp_path)

    with engine.connect() as connection:
        migration.run(connection)
        assert migration.verify(connection) == []

    column = _runtime_column(engine)
    assert column is not None, 'runtime was not added'
    assert migration._is_integer_family(column['type'])
    assert column['nullable'] is True, (
        'runtime must be nullable so existing rows need no value')


# ── 2. already-correct column ─────────────────────────────────────────────────


def test_existing_correct_column_is_a_safe_no_op(tmp_path):
    engine = _legacy_engine(tmp_path)
    with engine.connect() as connection:
        migration.run(connection)

    with engine.connect() as connection:
        result = migration.run(connection)
        assert migration.verify(connection) == []

    names = [c['name'] for c in inspect(engine).get_columns('media_item')]
    assert names.count('runtime') == 1, (
        'the column must not be duplicated; got %r' % names)
    assert result['columns'] == ['media_item.runtime']


def test_three_consecutive_runs_still_yield_one_column(tmp_path):
    engine = _legacy_engine(tmp_path)
    for _ in range(3):
        with engine.connect() as connection:
            migration.run(connection)
    names = [c['name'] for c in inspect(engine).get_columns('media_item')]
    assert names.count('runtime') == 1


# ── 3. existing rows ──────────────────────────────────────────────────────────


def test_existing_rows_survive_with_runtime_left_null(tmp_path):
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO media_item (tmdb_id, media_type, title) "
            "VALUES (17287, 'tv', 'Legacy Show')")
        connection.exec_driver_sql(
            "INSERT INTO media_item (tmdb_id, media_type, title) "
            "VALUES (680, 'movie', 'Pulp Fiction')")

    with engine.connect() as connection:
        migration.run(connection)

    with engine.connect() as connection:
        rows = connection.execute(
            text('SELECT tmdb_id, title, runtime FROM media_item '
                 'ORDER BY tmdb_id')).fetchall()

    assert [(r.tmdb_id, r.title) for r in rows] == [
        (680, 'Pulp Fiction'), (17287, 'Legacy Show')]
    assert all(r.runtime is None for r in rows), (
        'runtime must stay NULL; a backfilled value would be fabricated data '
        'that downstream statistics would treat as real')


# ── 4. missing table ──────────────────────────────────────────────────────────


def test_missing_media_item_refuses_and_creates_nothing(tmp_path):
    """The bootstrap case 0005 must NOT handle.

    Creating the application schema from a one-column migration is the exact
    defect F32 removed from the legacy script. A fresh database belongs to
    ``scripts/bootstrap_dev_schema.py``.
    """
    engine = _bare_engine(tmp_path, 'empty.db')

    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            migration.run(connection)
        assert 'does not exist' in str(excinfo.value)

    assert inspect(engine).get_table_names() == [], (
        'the refusal must create nothing at all')


def test_missing_table_refusal_names_the_established_remedies(tmp_path):
    engine = _bare_engine(tmp_path, 'empty2.db')
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            migration.run(connection)

    message = str(excinfo.value)
    assert 'scripts/bootstrap_dev_schema.py' in message
    assert 'migration-inventory.md' in message


def test_0005_source_contains_no_unrestricted_schema_creation():
    """AST, not text: the module docstring legitimately says ``create_all()``.

    Only an executable call is a defect, and only the parse tree can tell a
    docstring mention from a call.
    """
    import ast
    with open(migration.__file__, encoding='utf-8') as handle:
        tree = ast.parse(handle.read(), filename=migration.__file__)

    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == 'create_all':
                calls.append(getattr(node.func.value, 'id', '<expr>'))
    assert not calls, (
        '0005 must not call create_all() on %r; it adds exactly one column.'
        % (calls,))


# ── 5. incompatible existing column ───────────────────────────────────────────


@pytest.mark.parametrize('runtime_ddl,expected_fragment', [
    ('runtime VARCHAR(20)', 'VARCHAR'),
    ('runtime TEXT', 'TEXT'),
    ('runtime TIMESTAMP', 'TIMESTAMP'),
    ('runtime BOOLEAN', 'BOOLEAN'),
    ('runtime NUMERIC', 'NUMERIC'),
    ('runtime FLOAT', 'FLOAT'),
])
def test_incompatible_column_type_is_refused(
        tmp_path, runtime_ddl, expected_fragment):
    engine = _legacy_engine(tmp_path, runtime_ddl=runtime_ddl)
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            migration.run(connection)

    message = str(excinfo.value)
    assert expected_fragment in message.upper()
    # The refusal must be actionable, not merely a complaint.
    assert 'INTEGER' in message
    assert '0005' in message


def test_not_null_column_is_refused(tmp_path):
    """NOT NULL is a different schema: existing rows would have no value."""
    engine = _legacy_engine(
        tmp_path, runtime_ddl='runtime INTEGER NOT NULL DEFAULT 0')
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            migration.run(connection)

    message = str(excinfo.value)
    assert 'NOT NULL' in message

    column = _runtime_column(engine)
    assert column['nullable'] is False, (
        'the refusal must leave the incompatible column exactly as it was, not '
        'relax it')


def test_incompatible_refusal_explains_it_cannot_self_repair(tmp_path):
    engine = _legacy_engine(tmp_path, runtime_ddl='runtime VARCHAR(20)')
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            migration.run(connection)

    message = str(excinfo.value)
    assert 'NOT repaired automatically' in message
    assert 'ADD COLUMN' in message


def test_incompatible_refusal_changes_nothing(tmp_path):
    engine = _legacy_engine(tmp_path, runtime_ddl='runtime VARCHAR(20)')
    with engine.connect() as connection:
        before = connection.execute(text(
            'SELECT COUNT(*) FROM media_item')).scalar()
        with pytest.raises(RuntimeError):
            migration.run(connection)

    with engine.connect() as connection:
        after = connection.execute(text(
            'SELECT COUNT(*) FROM media_item')).scalar()
    assert after == before
    column = _runtime_column(engine)
    assert 'VARCHAR' in str(column['type']).upper(), (
        'the refusal must not have rewritten the column')


def test_verify_also_rejects_an_incompatible_column(tmp_path):
    """verify() is the last gate before the ledger row is written."""
    engine = _legacy_engine(tmp_path, runtime_ddl='runtime VARCHAR(20)')
    with engine.connect() as connection:
        problems = migration.verify(connection)
    assert problems, (
        'verify() must report an incompatible column, not return clean')


# ── 6 & 7. ledger and repeated invocation ─────────────────────────────────────


def test_upgrade_records_0005_with_its_file_checksum(tmp_path):
    engine = _full_schema_engine(tmp_path)

    applied = runner.apply_pending(engine)

    assert VERSION in applied
    rows = _ledger_rows(engine)
    assert VERSION in rows
    assert rows[VERSION][1] == 'migration'
    assert rows[VERSION][0] == runner.checksum_for(
        registry.by_version()[VERSION])


def test_upgrade_adds_the_column_and_preserves_the_row(tmp_path):
    engine = _full_schema_engine(tmp_path)
    assert _runtime_column(engine) is None, (
        'precondition: the simulated database must lack runtime')

    runner.apply_pending(engine)

    column = _runtime_column(engine)
    assert column is not None
    assert column['nullable'] is True
    with engine.connect() as connection:
        row = connection.execute(text(
            'SELECT title, runtime FROM media_item WHERE tmdb_id = 17287')
        ).fetchone()
    assert row.title == 'Legacy Show'
    assert row.runtime is None


def test_repeated_upgrade_does_not_reapply_or_duplicate(tmp_path):
    engine = _full_schema_engine(tmp_path)
    runner.apply_pending(engine)

    assert runner.apply_pending(engine) == [], (
        'a second upgrade must find nothing pending')

    rows = _ledger_rows(engine)
    assert sum(1 for v in rows if v == VERSION) == 1
    names = [c['name'] for c in inspect(engine).get_columns('media_item')]
    assert names.count('runtime') == 1


def test_0005_never_reaches_the_destructive_approval_gate(tmp_path):
    """The ordinary stage must apply 0005 without any backup evidence."""
    engine = _full_schema_engine(tmp_path)
    applied = runner.apply_pending(engine)
    assert VERSION in applied

    # 0002 stayed deferred and unrecorded: the additive column did not drag the
    # destructive DROP into the ordinary path.
    rows = _ledger_rows(engine)
    assert '0002_remove_legacy_wishlist' not in rows


# ── 10. failure must not record success ───────────────────────────────────────


def test_failing_verify_records_no_ledger_row(tmp_path):
    """A partial apply must never look complete in the ledger.

    ``run`` succeeds here (the table and column are fine) and ``verify`` is
    forced to report a problem, isolating the post-run gate from the DDL.
    """
    engine = _legacy_engine(tmp_path)
    with engine.connect() as connection:
        runner.ensure_ledger_table(connection)
        connection.commit()

    spec = registry.by_version()[VERSION]
    original = migration.verify
    migration.verify = lambda connection: ['forced: simulating partial apply']
    try:
        with pytest.raises(runner.MigrationError):
            runner.apply_one(engine, spec, runner.checksum_for(spec))
    finally:
        migration.verify = original

    rows = _ledger_rows(engine)
    assert VERSION not in rows, (
        'a migration whose verify() reports problems must not be recorded')


def test_failing_run_records_no_ledger_row(tmp_path):
    engine = _bare_engine(tmp_path, 'nofile.db')
    with engine.connect() as connection:
        runner.ensure_ledger_table(connection)
        connection.commit()

    spec = registry.by_version()[VERSION]
    with pytest.raises(runner.MigrationError):
        runner.apply_one(engine, spec, runner.checksum_for(spec))

    assert VERSION not in _ledger_rows(engine)
    assert inspect(engine).get_table_names() == ['schema_migrations'], (
        'the refusal must leave the database exactly as it found it')


# ── 8. guards still apply ─────────────────────────────────────────────────────


def test_pending_guard_tolerates_runtime_only_while_0005_is_pending(tmp_path):
    """Ownership is narrow by construction.

    While 0005 is registered, not applied and non-destructive, a missing
    ``runtime`` is expected drift the pre-upgrade guard may tolerate. Once
    0005 is recorded, the same missing column is real drift and must be fatal.
    """
    from utils.schema_guard import pending_migration_objects

    engine = _legacy_engine(tmp_path)
    owned = pending_migration_objects(engine)['columns']
    assert ('media_item', 'runtime') in owned

    _apply_0005_only(engine)
    after = pending_migration_objects(engine)['columns']
    assert ('media_item', 'runtime') not in after


def test_runtime_is_reported_as_declared_column_drift_once_applied(tmp_path):
    """Recorded + still missing == genuine drift, not tolerated pending work."""
    from utils.schema_guard import pending_migration_objects

    engine = _legacy_engine(tmp_path)
    _apply_0005_only(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql('ALTER TABLE media_item DROP COLUMN runtime')

    owned = pending_migration_objects(engine)['columns']
    assert ('media_item', 'runtime') not in owned, (
        'an APPLIED migration must not excuse its own missing object')


def test_declared_column_matches_the_model(tmp_path):
    """The DDL is rendered from the model, not hard-coded.

    If the model changes, the migration follows it and the drift shows up in
    review rather than as a type error against a live database.
    """
    declared = migration._declared_column()
    assert declared.name == 'runtime'
    assert declared.nullable is True
    assert migration._is_integer_family(declared.type)


def test_the_runner_refuses_to_edit_an_applied_migration(tmp_path):
    """Checksum immutability is what makes the ledger mean anything.

    0005 is additive and has therefore never been applied to a database whose
    recorded checksum could drift — but the guarantee must hold for it too, or
    the ledger is only meaningful for the older migrations.
    """
    engine = _legacy_engine(tmp_path)
    _apply_0005_only(engine)

    rows = _ledger_rows(engine)
    recorded = rows[VERSION][0]

    report = runner.plan(rows)
    assert report['mismatched'] == []

    tampered = dict(rows)
    tampered[VERSION] = ('deadbeef' * 8, 'migration')
    assert runner.plan(tampered)['mismatched'], (
        'a recorded checksum that differs from the file must be reported')
    assert recorded != 'deadbeef' * 8


# ── isolation ─────────────────────────────────────────────────────────────────


def test_these_tests_cannot_reach_production():
    url = os.environ.get('DATABASE_URL', '')
    assert url.startswith('sqlite:///'), (
        'the suite must run against a local SQLite file; got %r' % url)
    assert '/tmp' in url or url.startswith('sqlite:///:memory:')
