"""Offline (SQLite) coverage for registered migration ``0006_user_session_version``.

Feature F55. The PostgreSQL half lives in ``tests/test_migration_postgres.py``;
this file covers what can be proved without a server, and in particular the one
thing SQLite alone could NOT have caught.

Why this file exists
--------------------
``user`` is a RESERVED word in PostgreSQL, and the migration originally emitted
``ALTER TABLE user ADD COLUMN ...`` unquoted — a syntax error on the production
engine. The offline suite stayed green because SQLite accepts the bare
identifier, so nothing in the SQLite run ever noticed. That is precisely the
class of defect an engine-differing migration can hide, so the statement's
rendering is asserted here for BOTH dialects: PostgreSQL must quote, SQLite must
not need to. No server is involved; the dialect objects are pure SQLAlchemy.
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql, sqlite

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'migrates'))

import migrations_0006_user_session_version as migration  # noqa: E402
from migrates import registry  # noqa: E402

VERSION = '0006_user_session_version'


# ── registry / shape (offline) ───────────────────────────────────────────────

def test_registry_declares_0006_and_stays_valid():
    spec = registry.by_version()[VERSION]
    assert spec.module == 'migrations_0006_user_session_version'
    assert spec.depends_on == ('0001_canonical_watched_reconcile',), (
        '0006 must depend on 0001 only, so the deferred destructive 0002 '
        'cannot block a purely additive schema change')
    assert registry.validate() == []


def test_0006_owns_exactly_one_column_and_creates_no_table():
    assert migration.ADDS_COLUMNS == ('user.session_version',)
    assert migration.CREATES_TABLES == ()
    assert migration.DESTRUCTIVE is False


def test_0006_exposes_run_and_verify():
    assert callable(migration.run)
    assert callable(migration.verify)


# ── identifier preparation: the defect SQLite alone would have hidden ─────────

def _rendered_add_column(dialect):
    """The statement the migration really sends, via its own builder.

    Deliberately NOT re-implemented here. An earlier version of this file
    rebuilt the statement from ``_declared_column()`` and the preparer, and both
    mutation checks against the real ``_add_column`` passed — the test was
    asserting a copy that could drift from the code under test. Calling the
    migration's own builder is what makes these tests capable of failing.
    """
    return migration._add_column_statement(dialect)


def test_postgres_quotes_the_reserved_table_name():
    """`user` is reserved on PostgreSQL; the bare form is a syntax error."""
    statement = _rendered_add_column(postgresql.dialect())
    assert 'ALTER TABLE "user" ADD COLUMN' in statement, (
        'the ADD COLUMN statement must quote the reserved table name on '
        'PostgreSQL, got: %s' % statement)
    assert 'ALTER TABLE user ' not in statement, (
        'the unquoted form was emitted on PostgreSQL: %s' % statement)


def test_sqlite_still_gets_the_bare_identifier():
    """Quoting is per-dialect, not hard-coded, and SQLite needs no quotes."""
    statement = _rendered_add_column(sqlite.dialect())
    assert 'ALTER TABLE user ADD COLUMN' in statement, (
        'SQLite does not reserve `user`; got: %s' % statement)


def test_rendered_column_keeps_type_default_and_not_null():
    """The ADD COLUMN must still be `INTEGER ... DEFAULT 0 ... NOT NULL`."""
    for label, dialect in (('postgresql', postgresql.dialect()),
                           ('sqlite', sqlite.dialect())):
        statement = _rendered_add_column(dialect)
        upper = statement.upper()
        assert 'INTEGER' in upper, '%s: %s' % (label, statement)
        assert 'DEFAULT' in upper, '%s: %s' % (label, statement)
        assert 'NOT NULL' in upper, '%s: %s' % (label, statement)
        assert 'SESSION_VERSION' in upper, '%s: %s' % (label, statement)


# ── run / verify against a real SQLite database ──────────────────────────────

def _user_table_sql():
    return (
        'CREATE TABLE "user" ('
        'id INTEGER PRIMARY KEY, '
        'username VARCHAR(80), '
        'email VARCHAR(120), '
        'password_hash VARCHAR(512), '
        'email_verified BOOLEAN NOT NULL)'
    )


def test_column_is_added_to_a_populated_table_and_backfilled(tmp_path):
    engine = create_engine('sqlite:///%s' % (tmp_path / 'f55offline.db'))
    with engine.begin() as connection:
        connection.exec_driver_sql(_user_table_sql())
        connection.exec_driver_sql(
            "INSERT INTO \"user\" (username, email, password_hash, "
            "email_verified) VALUES ('a', 'a@t.test', 'x', 1)")

    with engine.begin() as connection:
        assert migration.verify(connection) != []      # missing first
        migration.run(connection)
        assert migration.verify(connection) == []     # present after

    with engine.connect() as connection:
        value = connection.exec_driver_sql(
            'SELECT session_version FROM "user" WHERE username = ?',
            ('a',)).scalar()
        assert value == 0, (
            'the pre-existing row must be backfilled to 0, got %r' % (value,))
        # A later insert may omit the column; the server default supplies it.
        connection.exec_driver_sql(
            "INSERT INTO \"user\" (username, email, password_hash, "
            "email_verified) VALUES ('b', 'b@t.test', 'x', 1)")
        assert connection.exec_driver_sql(
            'SELECT session_version FROM "user" WHERE username = ?',
            ('b',)).scalar() == 0
    engine.dispose()


def test_run_is_idempotent(tmp_path):
    engine = create_engine('sqlite:///%s' % (tmp_path / 'f55idem.db'))
    with engine.begin() as connection:
        connection.exec_driver_sql(_user_table_sql())
        migration.run(connection)
        migration.run(connection)
        assert migration.verify(connection) == []
    engine.dispose()


def test_a_nullable_column_is_refused_rather_than_adopted(tmp_path):
    """A NULLABLE version would compare unequal to every cookie issued."""
    engine = create_engine('sqlite:///%s' % (tmp_path / 'f55null.db'))
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE "user" (id INTEGER PRIMARY KEY, '
            'session_version INTEGER)')
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            with connection.begin():
                migration.run(connection)
    assert 'NULLABLE' in str(excinfo.value)
    engine.dispose()


def test_a_missing_user_table_is_refused_before_any_ddl(tmp_path):
    """One column's migration must never bootstrap the schema."""
    engine = create_engine('sqlite:///%s' % (tmp_path / 'f55notable.db'))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            with connection.begin():
                migration.run(connection)
    message = str(excinfo.value)
    assert 'does not exist' in message
    assert 'bootstrap' in message.lower()
    engine.dispose()
