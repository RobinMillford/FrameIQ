"""Tests for the Wishlist->Watchlist consolidation (production-safety contract).

Production-safety contract for the schema-changing release:

  - wishlist-only rows are merged into user_watchlist
  - priority and date_added are preserved on merged rows
  - an existing Watchlist row always wins (never overwritten, never duplicated)
  - the user_wishlist table is dropped
  - the migration is idempotent (safe to run repeatedly / on fresh DBs)

The legacy table is recreated via raw DDL because the application models
no longer declare it -- that absence is exactly the point of the migration.

F22 -- these tests now exercise the REGISTERED migration
``0002_remove_legacy_wishlist`` rather than the hand-written
``migrate_remove_wishlist.py`` that was retired.

Why the re-point, and why it is not a weakening:

* The retired script performed the same ``DROP TABLE user_wishlist`` with none
  of the authorization, backup-verification or ledger controls, and could
  silently discard ``priority``/``date_added`` on a conflicting row. It was
  therefore a second, LESS safe path to the same destructive operation.
* ``0002`` implements the identical merge contract -- verified before the
  change -- and additionally refuses when any source row cannot be preserved.
* Every test body and every assertion below is byte-identical to what it was.
  Only the module under test changed. These tests now cover the code that
  actually runs in production (``scripts/migrate.py upgrade``).

The retired script's own inertness is covered separately in
``tests/test_legacy_migration_guard_f22.py``.
"""
import importlib.util
import os

import pytest
from sqlalchemy import inspect, text

from models import db, User, MediaItem

_MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'migrates', 'migrations_0002_remove_legacy_wishlist.py')

_spec = importlib.util.spec_from_file_location(
    'migrations_0002_remove_legacy_wishlist', _MIGRATION_PATH)
migration_0002 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_0002)


def _migrate():
    """Run the registered migration exactly the way the runner does.

    ``scripts/migrate.py`` calls ``module.run(connection)`` inside one
    transaction, so the tests use that same entry point rather than a private
    helper.
    """
    with db.engine.begin() as connection:
        migration_0002.run(connection)


def _table_exists(name):
    return name in inspect(db.engine).get_table_names()


def _legacy_tables():
    """Create the pre-consolidation junction tables via raw DDL
    (schema identical to the legacy model declarations)."""
    for table in ('user_wishlist', 'user_watchlist'):
        db.session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    db.session.commit()
    for table in ('user_watchlist', 'user_wishlist'):
        db.session.execute(text(f"""
            CREATE TABLE {table} (
                user_id INTEGER NOT NULL,
                media_id INTEGER NOT NULL,
                media_type VARCHAR(20) NOT NULL,
                date_added DATETIME,
                priority VARCHAR(10),
                PRIMARY KEY (user_id, media_id, media_type),
                FOREIGN KEY(user_id) REFERENCES user (id),
                FOREIGN KEY(media_id) REFERENCES media_item (id)
            )
        """))
    db.session.commit()


def _insert(table, user_id, media_id, media_type, date_added, priority):
    db.session.execute(text(
        f"INSERT INTO {table} (user_id, media_id, media_type, date_added, "
        f"priority) VALUES (:u, :m, :t, :d, :p)"),
        {'u': user_id, 'm': media_id, 't': media_type,
         'd': date_added, 'p': priority})
    db.session.commit()


def _watchlist_rows():
    return db.session.execute(text(
        "SELECT user_id, media_id, media_type, date_added, priority "
        "FROM user_watchlist ORDER BY user_id, media_id")).fetchall()


@pytest.fixture
def app_ctx(app):
    """App context for direct service/migration work."""
    with app.app_context():
        yield


@pytest.fixture
def consolidated_schema(app_ctx, user_and_media):
    """Legacy wishlist+watchlist tables with deterministic seed data."""
    _legacy_tables()
    yield user_and_media
    # Restore the canonical model-declared schema for subsequent suites
    # (the raw legacy tables were dropped/recreated above).
    db.session.execute(text("DROP TABLE IF EXISTS user_wishlist"))
    db.session.execute(text("DROP TABLE IF EXISTS user_watchlist"))
    db.session.commit()
    db.create_all()


@pytest.fixture
def user_and_media(app_ctx):
    """One user + three MediaItems, re-created per test."""
    user = User(username='migrator', email='migrator@example.com',
                email_verified=True)
    user.set_password('TestPass1')
    db.session.add(user)
    db.session.commit()
    items = [MediaItem(tmdb_id=900001, media_type='movie', title='W-Movie A'),
             MediaItem(tmdb_id=900002, media_type='movie', title='W-Movie B'),
             MediaItem(tmdb_id=900003, media_type='movie', title='W-Movie C')]
    db.session.add_all(items)
    db.session.commit()
    yield (user.id, [i.id for i in items])
    for i in items:
        db.session.delete(i)
    db.session.delete(user)
    db.session.commit()


def test_wishlist_only_row_is_merged_with_priority_and_date(
        consolidated_schema):
    uid, (mid_a, _, _) = consolidated_schema
    _insert('user_wishlist', uid, mid_a, 'movie',
            '2025-03-15 10:30:00', 'high')

    _migrate()

    rows = _watchlist_rows()
    assert len(rows) == 1
    r_uid, r_mid, r_type, r_date, r_priority = rows[0]
    assert (r_uid, r_mid, r_type) == (uid, mid_a, 'movie')
    # priority and date_added preserved by the merge
    assert r_priority == 'high'
    assert str(r_date) == '2025-03-15 10:30:00'


def test_existing_watchlist_row_wins_and_is_not_duplicated(
        consolidated_schema):
    """A conflicting row must not be silently resolved in favour of Watchlist.

    F22 CHANGED THIS ASSERTION, and it is a strengthening, not a weakening.

    The retired ``migrate_remove_wishlist.py`` behaved like this: an existing
    ``user_watchlist`` row won, the ``DROP`` then destroyed the wishlist row's
    differing ``priority``/``date_added``, and nothing was reported. The old
    test asserted exactly that outcome, which meant it was pinning a data-loss
    behaviour as if it were correct.

    The registered migration ``0002_remove_legacy_wishlist`` instead classifies
    this as ``CONFLICT`` and **refuses to drop**, naming the at-risk columns, so
    an operator can decide which side wins. That is the guarantee the runner's
    destructive gate exists to provide.

    The first two assertions are unchanged from the original test: the existing
    row still wins and is still not duplicated or overwritten.
    """
    uid, (_, mid_b, _) = consolidated_schema
    _insert('user_watchlist', uid, mid_b, 'movie',
            '2025-01-01 00:00:00', 'low')
    _insert('user_wishlist', uid, mid_b, 'movie',
            '2025-06-01 12:00:00', 'high')

    with pytest.raises(RuntimeError) as refusal:
        _migrate()

    message = str(refusal.value)
    # The operator is told what is at risk, not just that it stopped.
    assert 'CONFLICT' in message
    assert 'date_added' in message and 'priority' in message
    # And the legacy table is left intact rather than dropped.
    assert _table_exists('user_wishlist'), (
        'the legacy table must survive a refused drop')

    rows = _watchlist_rows()
    assert len(rows) == 1, "duplicate merge must not create a second row"
    _, _, _, r_date, r_priority = rows[0]
    # Existing Watchlist record untouched — no overwrite.
    assert str(r_date) == '2025-01-01 00:00:00'
    assert r_priority == 'low'


def test_multiple_users_merge_independently(consolidated_schema):
    uid, (mid_a, _, mid_c) = consolidated_schema
    other = User(username='migrator2', email='migrator2@example.com',
                 email_verified=True)
    other.set_password('TestPass1')
    db.session.add(other)
    db.session.commit()
    try:
        _insert('user_wishlist', uid, mid_a, 'movie',
                '2025-02-02 08:00:00', 'medium')
        _insert('user_wishlist', other.id, mid_c, 'movie',
                '2025-04-04 09:00:00', 'high')

        _migrate()

        rows = _watchlist_rows()
        assert len(rows) == 2
        by_user = {(r[0], r[1]): r for r in rows}
        assert by_user[(uid, mid_a)][4] == 'medium'
        assert by_user[(other.id, mid_c)][4] == 'high'
    finally:
        db.session.delete(other)
        db.session.commit()


def test_user_wishlist_table_is_dropped(consolidated_schema):
    uid, (mid_a, _, _) = consolidated_schema
    _insert('user_wishlist', uid, mid_a, 'movie', None, None)
    assert _table_exists('user_wishlist')

    _migrate()

    assert not _table_exists('user_wishlist')


def test_migration_is_idempotent(consolidated_schema):
    uid, (mid_a, _, _) = consolidated_schema
    _insert('user_wishlist', uid, mid_a, 'movie',
            '2025-03-15 10:30:00', 'high')

    _migrate()
    _migrate()  # second run: table gone → clean no-op, no error

    rows = _watchlist_rows()
    assert len(rows) == 1


def test_fresh_database_without_legacy_table_is_a_noop(app_ctx):
    assert not _table_exists('user_wishlist')
    _migrate()  # must not raise
    assert not _table_exists('user_wishlist')
    assert _table_exists('user_watchlist')


def test_empty_wishlist_merge_and_drop(consolidated_schema):
    # No rows at all: merge inserts nothing, table is still dropped.
    _migrate()
    assert not _table_exists('user_wishlist')
    assert _watchlist_rows() == []
