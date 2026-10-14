"""Regression tests for migrates/migrate_media_runtime.py.

Production incident: models/media.py declares `MediaItem.runtime`, but
db.create_all() cannot alter existing PostgreSQL tables, so production
media_item lacked the column and detail queries failed with
UndefinedColumn. These tests lock in, hermetically (SQLite, no prod
credentials):

1. the ORM model still declares the runtime column (model ↔ migration
   consistency), and
2. the migration's column helper is idempotent against a schema that
   predates the column: first call adds it, second call is a no-op,
   and existing row data is never touched.
"""
import ast
import importlib.util
import os
import re

import pytest
from sqlalchemy import (
    Column, Date, Float, Integer, MetaData, String, Table, Text,
    create_engine, text)

_MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'migrates', 'migrate_media_runtime.py')


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        'migrate_media_runtime', _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migration():
    return _load_migration_module()


def test_model_declares_runtime_column():
    """The model the migration must repair still expects media_item.runtime."""
    from models import MediaItem
    runtime_col = MediaItem.__table__.columns.get('runtime')
    assert runtime_col is not None, (
        'models/media.py no longer declares runtime — the migration '
        '(and this test) must be updated together with the model.')
    assert isinstance(runtime_col.type, Integer), (
        'runtime must remain INTEGER (minutes)')
    assert runtime_col.nullable, 'runtime is nullable (minutes, optional)'


def _legacy_media_item_table(metadata):
    """A media_item table as it existed before Feature 05 added runtime."""
    return Table(
        'media_item', metadata,
        Column('id', Integer, primary_key=True),
        Column('tmdb_id', Integer, nullable=False, unique=True),
        Column('media_type', String(20), nullable=False),
        Column('title', String(200), nullable=False),
        Column('release_date', Date),
        Column('poster_path', String(200)),
        Column('genres', String(200)),
        Column('overview', Text),
        Column('rating', Float),
    )


def test_migration_adds_runtime_once_and_preserves_data(migration):
    """First run adds the column; second run is a no-op; data is intact."""
    engine = create_engine('sqlite://')
    metadata = MetaData()
    table = _legacy_media_item_table(metadata)
    metadata.create_all(engine)

    with engine.begin() as conn:
        conn.execute(table.insert().values(
            tmdb_id=17287, media_type='tv', title='Legacy Show'))

    assert not migration._column_exists(engine, 'media_item', 'runtime')

    # FIRST RUN: column is created.
    assert migration._ensure_runtime_column(engine) is True
    assert migration._column_exists(engine, 'media_item', 'runtime')

    # SECOND RUN: idempotent no-op.
    assert migration._ensure_runtime_column(engine) is False

    # Existing data untouched; new column is NULL for legacy rows.
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT tmdb_id, title, runtime FROM media_item "
            "WHERE tmdb_id = 17287")).fetchone()
    assert row.tmdb_id == 17287
    assert row.title == 'Legacy Show'
    assert row.runtime is None


def test_column_exists_safe_for_missing_table(migration):
    """Fresh-database path: no media_item table must not raise."""
    engine = create_engine('sqlite://')
    assert migration._column_exists(engine, 'media_item', 'runtime') is False


def test_migration_reported_noop_on_current_schema(migration, app):
    """Running migrate() against the current test schema is a safe no-op."""
    from models import db
    with app.app_context():
        db.create_all()  # ensure schema exists in the hermetic test DB
    assert migration.migrate() == 0


# ═══════════════════════════════════════════════════════════════════════════
# F31 — the script must never bootstrap a schema
# ═══════════════════════════════════════════════════════════════════════════
#
# F31 replaced an unscoped `db.create_all()` fallback. That call created EVERY
# table the models declare — the entire application schema — for a migration
# whose only subject is one column on one existing table. Target authorization
# (authorize_legacy_migration) is permission to perform this alteration, not
# permission to bootstrap a database from it.
#
# These tests are behavioural wherever possible: they run the real migrate()
# against a hermetic temp-file SQLite and inspect the schema afterwards.


def _module_ast():
    """Parsed AST of the script.

    AST rather than a text search, because the docstring and the F31 comment
    legitimately mention create_all() as history. Only an executable Call node
    is a defect, and only the AST can tell those apart.
    """
    with open(_MIGRATION_PATH, encoding='utf-8') as handle:
        return ast.parse(handle.read(), filename=_MIGRATION_PATH)


def _create_all_calls(tree):
    """Every executable ``*.create_all(...)`` call in the module."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'create_all':
            found.append(getattr(func.value, 'id', '<expr>'))
    return found


def test_legacy_script_cannot_invoke_unscoped_create_all():
    """The core F31 regression: no executable create_all remains."""
    calls = _create_all_calls(_module_ast())
    assert not calls, (
        "migrate_media_runtime.py calls create_all() on %r. Schema creation "
        "must go through the approved migration mechanism "
        "(migrates/registry.py + scripts/migrate.py), or be an explicit, "
        "scoped DDL statement." % (calls,))


def test_script_emits_no_create_table_or_index_itself():
    """Broader than create_all: no CREATE TABLE emitted by the script."""
    emitted = []
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.upper()
            if 'CREATE TABLE' in text or 'CREATE INDEX' in text:
                emitted.append(node.value.strip()[:60])
    assert not emitted, (
        "the script must not emit CREATE TABLE/INDEX itself; the only DDL it "
        "owns is the one-column ALTER: %r" % (emitted,))


def test_only_ddl_owned_is_the_single_column_alter():
    """Exactly one statement may create schema, and it is one column."""
    source = open(_MIGRATION_PATH, encoding='utf-8').read()
    alters = re.findall(
        r'ALTER\s+TABLE\s+\S+\s+ADD\s+COLUMN\s+\S+', source, re.IGNORECASE)
    assert len(alters) == 1, (
        "expected exactly one ALTER TABLE ... ADD COLUMN in the script, "
        "found %r" % (alters,))
    assert 'media_item' in alters[0] and 'runtime' in alters[0], (
        "the one DDL statement must be the media_item.runtime addition, "
        "found %r" % (alters[0],))


def _drop_all_tables(db):
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    for table in list(inspector.get_table_names()):
        db.session.execute(db.text('DROP TABLE IF EXISTS "%s"' % table))
    db.session.commit()


def test_migrate_refuses_when_media_item_is_absent(migration, app):
    """A database with no media_item must FAIL, not be bootstrapped.

    Before F31 this branch returned 0 having created every table in the
    application schema. It must now report failure and change nothing.
    """
    from models import db
    from sqlalchemy import inspect

    with app.app_context():
        _drop_all_tables(db)
        before = set(inspect(db.engine).get_table_names())
        assert 'media_item' not in before

        result = migration.migrate()

        after = set(inspect(db.engine).get_table_names())
    assert result == 1, (
        "migrate() must return non-zero when it cannot perform its one "
        "documented change; returned %r" % (result,))
    assert after == before, (
        "the refusal must not create anything; it created %r"
        % sorted(after - before))


def test_refusal_does_not_claim_success(migration, app, capsys):
    """Failure output must not contain the success marker.

    A [FAIL] path that also prints [OK] would tell an operator the migration
    landed when it did not.
    """
    from models import db

    with app.app_context():
        _drop_all_tables(db)
        migration.migrate()

    combined = capsys.readouterr()
    text = combined.out + combined.err
    assert '[FAIL]' in text
    assert '[OK]' not in text, (
        "the refusal path printed a success marker: %r" % text)


def test_refusal_names_the_remedy_for_each_database_kind(
        migration, app, capsys):
    """The operator must be told which of the remedies applies."""
    from models import db

    with app.app_context():
        _drop_all_tables(db)
        migration.migrate()

    out = capsys.readouterr().out
    assert 'scripts/bootstrap_dev_schema.py' in out, (
        "must point a fresh-database operator at the designated bootstrap")
    assert 'scripts/migrate.py' in out, (
        "must point a deployed-database operator at the approved runner")
    assert 'does not bootstrap the application schema' in out


def test_target_authorization_still_runs_before_any_app_import():
    """F31 must not have reordered or removed the F22 guard.

    ``import app`` builds an engine and runs the schema guard, so a check
    placed after it would be too late. This asserts statement ORDER in the AST,
    not that the string merely appears somewhere in the file.
    """
    names = []
    for node in _module_ast().body:
        if isinstance(node, ast.ImportFrom) and node.module == 'app':
            names.append('<import app>')
        elif isinstance(node, ast.Import) and any(
                a.name == 'app' for a in node.names):
            names.append('<import app>')
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, 'id', None):
                names.append(sub.func.id)
    assert 'authorize_legacy_migration' in names, (
        "the F22 target-authorization guard has been removed")
    assert '<import app>' in names
    assert names.index('authorize_legacy_migration') < names.index('<import app>'), (
        "authorization must precede the app import; got %r" % (names,))


def test_repeated_execution_is_idempotent(migration, app):
    """Three runs on a prepared schema all succeed; the column appears once."""
    from models import db

    with app.app_context():
        db.create_all()  # hermetic temp-file SQLite from conftest
        assert migration.migrate() == 0
        assert migration.migrate() == 0
        assert migration.migrate() == 0
        assert migration._column_exists(db.engine, 'media_item', 'runtime')


def test_column_is_applied_when_media_item_exists_without_it(migration):
    """The intended change still happens: one column, existing rows intact."""
    engine = create_engine('sqlite://')
    metadata = MetaData()
    table = _legacy_media_item_table(metadata)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(table.insert().values(
            tmdb_id=17287, media_type='tv', title='Legacy Show'))

    assert migration._ensure_runtime_column(engine) is True
    assert migration._column_exists(engine, 'media_item', 'runtime')
    with engine.connect() as conn:
        row = conn.execute(text(
            'SELECT tmdb_id, title, runtime FROM media_item '
            'WHERE tmdb_id = 17287')).fetchone()
    assert row.title == 'Legacy Show'
    assert row.runtime is None


def test_alter_failure_propagates_and_is_not_swallowed(migration):
    """A duplicate-column race is tolerated; a genuine failure must surface.

    ``_ensure_runtime_column`` re-inspects after a SQLAlchemyError and re-raises
    if the column is still absent. That is the failure path that must not leave
    a misleading "applied" state.
    """
    engine = create_engine('sqlite://')
    _legacy_media_item_table(MetaData()).create(engine)
    assert migration._ensure_runtime_column(engine) is True

    # Simulate the non-race failure: the table disappears underneath us.
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE media_item'))
    with pytest.raises(Exception):
        migration._ensure_runtime_column(engine)


def test_tests_are_hermetic_and_cannot_reach_production():
    """The suite runs on a throwaway SQLite file forced by conftest."""
    import os
    url = os.environ.get('DATABASE_URL', '')
    assert url.startswith('sqlite:///'), (
        'tests must run against a local SQLite file; DATABASE_URL is %r' % url)
    assert '/tmp' in url or url.startswith('sqlite:///:memory:'), (
        'the test database must be a temporary file, not a real target: %r'
        % url)
