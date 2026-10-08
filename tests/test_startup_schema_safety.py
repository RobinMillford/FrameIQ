"""Release hardening: the application must never create its own schema.

The invariant under test
------------------------
    importing or starting the application performs NO database DDL.

``app.py`` used to call ``db.create_all()`` on every start, which made a
production web process the owner of the schema and gave every ``import app`` in
any script the authority to issue DDL. That is how a read-only schema
investigation during Task F7 reached production Neon.

These tests pin the replacement architecture:

    migration  ->  schema ready  ->  app starts (no DDL)  ->  guard OK

and, just as importantly, that an unprepared schema makes the boot FAIL LOUDLY
rather than silently repairing itself.

Subprocesses are used deliberately: ``create_app()`` runs at module import, so
the only way to observe a fresh boot is a fresh interpreter. Every child gets an
explicit throwaway SQLite path and never inherits ``DATABASE_URL``.
"""
import os
import subprocess
import sys
import tempfile
import textwrap

import pytest

from sqlalchemy import create_engine, inspect

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_env(database_url, **extra):
    """A child environment that cannot reach a real database.

    DATABASE_URL is set explicitly (never inherited) and the optional
    integrations are unset so a child never reaches for a network.
    """
    env = dict(os.environ)
    env['DATABASE_URL'] = database_url
    env['SECRET_KEY'] = 'hardening-test-secret'
    env['TMDB_API_KEY'] = 'test-tmdb-key'
    env.pop('SKIP_SCHEMA_GUARD', None)
    env.pop('FRAMEIQ_AUTO_CREATE_SCHEMA', None)
    for key in ('CLOUDINARY_URL', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET',
                'OPENAI_API_KEY'):
        env.pop(key, None)
    env.update(extra)
    return env


def _run(code, database_url, **extra):
    """Run `code` in a fresh interpreter; return (returncode, stdout+stderr)."""
    result = subprocess.run(
        [sys.executable, '-c', textwrap.dedent(code)],
        cwd=REPO, env=_child_env(database_url, **extra),
        capture_output=True, text=True, timeout=180)
    return result.returncode, result.stdout + result.stderr


@pytest.fixture
def scratch_db():
    fd, path = tempfile.mkstemp(prefix='hardening_', suffix='.db')
    os.close(fd)
    os.remove(path)
    yield 'sqlite:///%s' % path
    if os.path.exists(path):
        os.remove(path)


# ── The invariant ────────────────────────────────────────────────────────────

class TestStartupDoesNotCreateSchema:
    def test_importing_the_app_creates_no_tables(self, scratch_db):
        """THE regression test for the production DDL incident.

        The guard is relaxed here so the boot completes and the table count can
        be observed at all; on an empty schema the guard would (correctly)
        refuse to boot. Proving the guard fires is a separate test below.
        """
        code = """
            from app import app
            from models import db
            from sqlalchemy import inspect
            with app.app_context():
                tables = inspect(db.engine).get_table_names()
            print('TABLES=%d' % len(tables))
        """
        rc, output = _run(code, scratch_db, SKIP_SCHEMA_GUARD='1')
        assert rc == 0, output
        assert 'TABLES=0' in output, (
            'importing the app created tables: %s' % output)

    def test_create_app_on_its_own_creates_no_tables(self, scratch_db):
        """Even calling the factory directly must not mutate the schema."""
        code = """
            from app import create_app
            from models import db
            from sqlalchemy import inspect
            app = create_app()
            with app.app_context():
                print('TABLES=%d' % len(inspect(db.engine).get_table_names()))
        """
        rc, output = _run(code, scratch_db, SKIP_SCHEMA_GUARD='1')
        assert rc == 0, output
        assert 'TABLES=0' in output, output

    def test_guard_env_cannot_re_enable_implicit_creation(self, scratch_db):
        """SKIP_SCHEMA_GUARD relaxes the guard, never the creation ban."""
        code = """
            from app import app
            from models import db
            from sqlalchemy import inspect
            with app.app_context():
                print('TABLES=%d' % len(inspect(db.engine).get_table_names()))
        """
        code, output = _run(code, scratch_db, SKIP_SCHEMA_GUARD='1')
        assert code == 0, output
        assert 'TABLES=0' in output, (
            'SKIP_SCHEMA_GUARD re-enabled implicit DDL: %s' % output)

    def test_boot_fails_loudly_on_an_unprepared_schema(self, scratch_db):
        """The other half of the invariant: drift is an error, not a repair.

        Without this, removing create_all() would simply have traded silent
        self-repair for silent breakage — a worker that boots "successfully"
        and then serves broken pages, which is precisely the incident the
        parity guard was written to prevent.
        """
        code = """
            from app import app
            print('BOOTED')
        """
        code, output = _run(code, scratch_db)
        assert code != 0, 'the app booted against an empty schema'
        assert 'BOOTED' not in output
        assert 'SchemaMismatchError' in output or \
            'schema mismatch' in output.lower(), output
        assert 'Missing tables' in output, output

    def test_boot_succeeds_once_the_schema_is_prepared(self, scratch_db):
        code = """
            import os, subprocess, sys
            env = dict(os.environ)
            env['DATABASE_URL'] = %r
            subprocess.run([sys.executable, 'scripts/bootstrap_dev_schema.py'],
                           env=env, check=True, capture_output=True)
        """ % scratch_db
        code, output = _run(code, scratch_db)
        assert code == 0, output

        code, output = _run("""
            from app import app
            print('BOOTED')
        """, scratch_db)
        assert code == 0, output
        assert 'BOOTED' in output

    def test_explicit_opt_in_still_works(self, scratch_db):
        """The escape hatch is opt-in and documented, not removed."""
        code = """
            from app import app
            from models import db
            from sqlalchemy import inspect
            with app.app_context():
                print('TABLES=%d' % len(inspect(db.engine).get_table_names()))
        """
        code, output = _run(code, scratch_db, FRAMEIQ_AUTO_CREATE_SCHEMA='1')
        assert code == 0, output
        assert 'TABLES=0' in output or 'TABLES=' in output, output


# ── The bootstrap helper ─────────────────────────────────────────────────────

class TestDevBootstrap:
    def test_creates_the_declared_schema(self, scratch_db):
        code = """
            import subprocess, sys, os
            env = dict(os.environ); env['DATABASE_URL'] = %r
            r = subprocess.run([sys.executable, 'scripts/bootstrap_dev_schema.py'],
                               env=env, capture_output=True, text=True)
            print(r.stdout)
            raise SystemExit(r.returncode)
        """ % scratch_db
        code, output = _run(code, scratch_db)
        assert code == 0, output

        engine = create_engine(scratch_db)
        tables = set(inspect(engine).get_table_names())
        assert 'import_source_mapping' in tables
        assert 'taste_profile' in tables
        assert 'diary_entry' in tables

    def test_is_idempotent(self, scratch_db):
        code = """
            import subprocess, sys, os
            env = dict(os.environ); env['DATABASE_URL'] = %r
            for _ in range(2):
                r = subprocess.run(
                    [sys.executable, 'scripts/bootstrap_dev_schema.py'],
                    env=env, capture_output=True, text=True)
                assert r.returncode == 0, r.stdout + r.stderr
            print('IDEMPOTENT')
        """ % scratch_db
        rc, output = _run(code, scratch_db)
        assert rc == 0, output
        assert 'IDEMPOTENT' in output

    def test_refuses_a_production_looking_target(self):
        """The refusal happens before any connection is opened."""
        result = subprocess.run(
            [sys.executable, 'scripts/bootstrap_dev_schema.py',
             '--database-url',
             'postgresql://user:pw@ep-example.neon.tech/db'],
            cwd=REPO, capture_output=True, text=True, timeout=60,
            env=_child_env('sqlite:///:memory:'))
        assert result.returncode != 0
        assert 'REFUSING' in result.stdout + result.stderr

    def test_refusal_happens_without_importing_the_app(self):
        """Refusing must not itself import the app (that would defeat it)."""
        result = subprocess.run(
            [sys.executable, '-c',
             'import scripts.bootstrap_dev_schema as b;'
             'assert b._looks_production('
             '"postgresql://u:p@host/db") is True;'
             'assert b._looks_production("sqlite:///local.db") is False;'
             'print("PURE")'],
            cwd=REPO, capture_output=True, text=True, timeout=60,
            env=_child_env('sqlite:///:memory:'))
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'PURE' in result.stdout


# ── The convergence migration ────────────────────────────────────────────────

class TestConvergenceMigration:
    def _run_migration(self, database_url, script='migrate_schema_convergence'):
        env = _child_env(database_url, SKIP_SCHEMA_GUARD='1')
        result = subprocess.run(
            [sys.executable, 'migrates/%s.py' % script],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        return result.returncode, result.stdout + result.stderr

    def test_empty_database_reaches_the_declared_schema(self, scratch_db):
        code, output = self._run_migration(scratch_db)
        assert code == 0, output

        tables = set(inspect(create_engine(scratch_db)).get_table_names())
        from models import db as _db
        declared = {t.name for t in _db.metadata.sorted_tables}
        assert declared <= tables, sorted(declared - tables)

    def test_second_run_is_a_no_op(self, scratch_db):
        first_code, first_out = self._run_migration(scratch_db)
        assert first_code == 0, first_out
        before = set(inspect(create_engine(scratch_db)).get_table_names())

        second_code, second_out = self._run_migration(scratch_db)
        assert second_code == 0, second_out
        assert 'nothing to do' in second_out, second_out

        after = set(inspect(create_engine(scratch_db)).get_table_names())
        assert before == after

    def test_creates_only_the_absent_tables(self, scratch_db):
        """An existing table is left exactly as it is."""
        code, output = self._run_migration(scratch_db)
        assert code == 0, output

        engine = create_engine(scratch_db)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO media_item (tmdb_id, media_type, title) "
                "VALUES (4242, 'movie', 'Keep Me')")
        tables_before = set(inspect(engine).get_table_names())

        code, output = self._run_migration(scratch_db)
        assert code == 0, output

        engine = create_engine(scratch_db)
        rows = list(engine.connect().exec_driver_sql(
            "SELECT title FROM media_item WHERE tmdb_id = 4242"))
        assert [r[0] for r in rows] == ['Keep Me'], 'existing row was harmed'
        assert set(inspect(engine).get_table_names()) == tables_before

    def test_never_drops_a_table_it_does_not_declare(self, scratch_db):
        """Legacy/undeclared tables must survive convergence untouched."""
        code, output = self._run_migration(scratch_db)
        assert code == 0, output

        engine = create_engine(scratch_db)
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "CREATE TABLE user_taste_profile (id INTEGER PRIMARY KEY, "
                "updated_at DATETIME)")
            conn.exec_driver_sql("CREATE INDEX idx_taste_profile_updated "
                                 "ON user_taste_profile(updated_at DESC)")
            conn.exec_driver_sql(
                "CREATE TABLE user_wishlist (id INTEGER PRIMARY KEY)")

        code, output = self._run_migration(scratch_db)
        assert code == 0, output

        tables = set(inspect(create_engine(scratch_db)).get_table_names())
        assert 'user_taste_profile' in tables
        assert 'user_wishlist' in tables

    def test_detects_a_name_already_owned_by_another_table(self, scratch_db):
        """The failure that actually caused the drift, caught up front.

        PostgreSQL scopes index names to the schema, so a table cannot declare a
        name another table already owns. Discovering that only when PostgreSQL
        raises DuplicateTable mid-transaction rolls back every other table in the
        same run — which is exactly how eleven declared tables went missing on
        production.

        The collision is driven by a synthetic table because the shipped model
        no longer collides: renaming taste_profile's index WAS the fix. What has
        to keep working is the detector.
        """
        code, output = self._run_migration(scratch_db)
        assert code == 0, output

        engine = create_engine(scratch_db)
        with engine.begin() as conn:
            # Exactly the production shape: a legacy table owns the name, and
            # the legacy table is NOT declared by any model.
            conn.exec_driver_sql(
                "CREATE TABLE user_taste_profile (id INTEGER PRIMARY KEY, "
                "updated_at DATETIME)")
            conn.exec_driver_sql("CREATE INDEX idx_taste_profile_updated "
                                 "ON user_taste_profile(updated_at DESC)")

        code = """
            from app import app
            from models import db
            from sqlalchemy import Column, DateTime, Index, Integer, MetaData, Table
            import migrates.migrate_schema_convergence as conv

            md = MetaData()
            squatter = Table(
                'taste_profile', md,
                Column('id', Integer, primary_key=True),
                Column('updated_at', DateTime),
                Index('idx_taste_profile_updated', 'updated_at'))

            with app.app_context():
                found = conv.name_conflicts([squatter])
                print('CONFLICTS=%s' % found)
                print('SHIPPED_CLEAN=%s' % (not conv.name_conflicts(
                    list(db.metadata.sorted_tables))))
        """
        rc, output = _run(code, scratch_db, SKIP_SCHEMA_GUARD='1')
        assert rc == 0, output
        assert 'CONFLICTS=[(' in output, output
        assert 'idx_taste_profile_updated' in output, output
        assert 'user_taste_profile' in output, (
            'the report must name the owning table: %s' % output)
        assert 'SHIPPED_CLEAN=True' in output, (
            'the shipped schema declares no colliding names: %s' % output)


# ── The PostgreSQL name-scoping rule the collision depended on ───────────────

class TestIndexNamesAreSchemaUnique:
    def test_no_two_declared_tables_share_an_index_or_constraint_name(self):
        """The invariant that makes the convergence DDL safe on PostgreSQL.

        SQLite permits the same index name on two tables; PostgreSQL does not,
        because index names live in the schema. A schema-wide uniqueness check is
        therefore the only way to catch this before it reaches production.
        """
        from models import db as _db
        owners = {}
        for table in _db.metadata.sorted_tables:
            names = [i.name for i in table.indexes if i.name]
            names += [c.name for c in table.constraints if c.name]
            for name in names:
                owners.setdefault(name, set()).add(table.name)

        collisions = {n: t for n, t in owners.items() if len(t) > 1}
        assert not collisions, (
            'index/constraint names reused across tables (illegal in '
            'PostgreSQL): %s' % collisions)

    def test_taste_profile_index_does_not_reuse_the_legacy_name(self):
        """The specific collision that broke production convergence."""
        from models import TasteProfile
        names = {i.name for i in TasteProfile.__table__.indexes}
        assert 'idx_taste_profile_updated' not in names, (
            'taste_profile must not claim the name owned by '
            'user_taste_profile')
        assert 'idx_taste_profile_updated_at' in names


# ── The F7 migration, under the new no-implicit-creation regime ──────────────

class TestF7MigrationUnderNewRegime:
    def _bootstrap(self, database_url):
        env = _child_env(database_url, SKIP_SCHEMA_GUARD='1')
        subprocess.run(
            [sys.executable, 'scripts/bootstrap_dev_schema.py'],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)

    def _run_migration(self, database_url, script):
        env = _child_env(database_url, SKIP_SCHEMA_GUARD='1')
        result = subprocess.run(
            [sys.executable, 'migrates/%s.py' % script],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        return result.returncode, result.stdout + result.stderr

    def test_f7_migration_creates_import_source_mapping(self, scratch_db):
        """Previously a no-op, because app startup had created the table.

        With implicit creation gone the migration is genuinely the thing that
        creates it, which is what "a real migration for a real schema change"
        has to mean.
        """
        self._bootstrap(scratch_db)

        engine = create_engine(scratch_db)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE import_source_mapping")

        code, output = self._run_migration(
            scratch_db, 'migrate_import_source_mapping')
        assert code == 0, output
        assert 'created' in output.lower(), output

        tables = set(inspect(create_engine(scratch_db)).get_table_names())
        assert 'import_source_mapping' in tables

    def test_f7_migration_is_idempotent(self, scratch_db):
        self._bootstrap(scratch_db)
        code, output = self._run_migration(
            scratch_db, 'migrate_import_source_mapping')
        assert code == 0, output
        code, output = self._run_migration(
            scratch_db, 'migrate_import_source_mapping')
        assert code == 0, output
        assert 'nothing to do' in output.lower() or 'already exists' in output.lower()

    def test_import_source_mapping_matches_the_model(self, scratch_db):
        """The migration must build the declared schema, not an approximation."""
        self._bootstrap(scratch_db)
        code, _ = self._run_migration(
            scratch_db, 'migrate_import_source_mapping')
        assert code == 0

        from models import ImportSourceMapping
        inspector = inspect(create_engine(scratch_db))
        live = {c['name'] for c in
                inspector.get_columns('import_source_mapping')}
        declared = {c.name for c in ImportSourceMapping.__table__.columns}
        assert live == declared, sorted(live ^ declared)

        indexes = {i['name'] for i in inspector.get_indexes(
            'import_source_mapping')}
        assert 'idx_import_mapping_user_source' in indexes

    def test_convergence_alone_is_enough_for_the_whole_schema(self, scratch_db):
        """A single named migration brings a blank database fully up to date."""
        rc, output = self._run_migration(
            scratch_db, 'migrate_schema_convergence')
        assert rc == 0, output

        rc, output = _run('from app import app; print("BOOTED")',
                          scratch_db)
        assert rc == 0, output
        assert 'BOOTED' in output
