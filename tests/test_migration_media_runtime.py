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
import importlib.util
import os

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
