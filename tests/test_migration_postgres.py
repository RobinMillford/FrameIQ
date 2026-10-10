"""PostgreSQL-only migration behaviour. Skipped unless a real server is given.

Why this module exists separately from test_migration_runner.py
--------------------------------------------------------------
The ordinary suite runs on SQLite, and SQLite cannot prove the two guarantees
that matter most for a destructive migration on production:

1. **Transactional DDL.** pysqlite commits around DDL, so a failed migration can
   leave its DDL behind on SQLite but not on PostgreSQL. A test that passes on
   SQLite proves nothing about rollback.
2. **Advisory locks.** ``pg_advisory_lock`` does not exist on SQLite. The runner
   prints "lock: UNAVAILABLE" and continues, so no SQLite test can show that
   concurrent runners are actually excluded.

Both were previously marked "not exercised". They are now exercised, against a
real PostgreSQL 17 server — the same major version as production.

Running them
------------
Point ``FRAMEIQ_TEST_POSTGRES_URL`` at a THROWAWAY database and run::

    FRAMEIQ_TEST_POSTGRES_URL=postgresql://user:pw@localhost:5432/scratch \\
        pytest tests/test_migration_postgres.py -v

CI runs them in a dedicated job with a `postgres:17` service
(``pytest -m postgres``). Nothing here may ever point at production: the
connection string is checked to be local before any test runs, and the tests
drop and recreate only their own throwaway tables.

PostgreSQL version parity
-------------------------
Production reports 17.11. The CI service is pinned to postgres:17 for that
reason; the two features relied on here (session advisory locks, transactional
DDL) are long-stable, but the pin is deliberate.
"""
import os
import sys
import threading
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.postgres

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'migrates'))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import migrate as runner  # noqa: E402

DESTRUCTIVE_VERSION = '0002_remove_legacy_wishlist'


def _pg_url():
    url = os.environ.get('FRAMEIQ_TEST_POSTGRES_URL', '').strip()
    if not url:
        return None
    # Refuse anything that could be a real database. This suite creates and
    # drops tables; pointing it at production would be catastrophic, so the
    # check is a hard precondition for the whole module, not per-test advice.
    from utils.db_target import classify
    target = classify(url)
    if not target.is_local:
        pytest.skip('FRAMEIQ_TEST_POSTGRES_URL is not a local host; refusing '
                    'to run destructive tests against %r' % target.safe_identity())
    return url


PG_URL = _pg_url()
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(PG_URL is None,
                       reason='needs FRAMEIQ_TEST_POSTGRES_URL pointing at a '
                              'throwaway PostgreSQL server'),
]


@pytest.fixture
def pg_engine():
    """A throwaway PostgreSQL database holding the declared schema."""
    from models.base import db
    engine = create_engine(PG_URL, future=True)
    # Prove it really is PostgreSQL before doing anything else.
    assert engine.dialect.name == 'postgresql'
    with engine.begin() as connection:
        connection.execute(text('DROP SCHEMA public CASCADE'))
        connection.execute(text('CREATE SCHEMA public'))
    db.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def _seed_identity(connection, users, media):
    """Create the parent rows PostgreSQL's foreign keys demand.

    SQLite does not enforce FKs by default, so the SQLite suite never needed
    this. On PostgreSQL the seeds must satisfy the real constraints, otherwise
    these tests would be measuring the FK instead of the migration.
    """
    import datetime

    for user_id in users:
        connection.execute(text(
            "INSERT INTO \"user\" (id, username, email, password_hash, "
            "date_joined, is_active, email_verified) "
            "VALUES (:i, :u, :e, 'x', :d, TRUE, FALSE)"), {
                'i': user_id, 'u': 'pguser%d' % user_id,
                'e': 'pguser%d@example.test' % user_id,
                'd': datetime.datetime.utcnow()})
    for media_id in media:
        connection.execute(text(
            'INSERT INTO media_item (id, tmdb_id, media_type, title) '
            'VALUES (:i, :t, \'movie\', :n)'), {
                'i': media_id, 't': media_id, 'n': 'M%d' % media_id})


def _seed_wishlist(connection, rows=((1, 100, 'movie', '2026-01-27 08:15:51',
                                      'low'),)):
    """Create the legacy table exactly as production has it, plus seed rows."""
    connection.execute(text("""
        CREATE TABLE user_wishlist (
            user_id INTEGER NOT NULL,
            media_id INTEGER NOT NULL,
            media_type VARCHAR(20) NOT NULL,
            date_added TIMESTAMP,
            priority VARCHAR(10),
            PRIMARY KEY (user_id, media_id, media_type))"""))
    users = {r[0] for r in rows}
    media = {r[1] for r in rows}
    _seed_identity(connection, users, media)
    for user_id, media_id, media_type, added, priority in rows:
        connection.execute(text(
            'INSERT INTO user_wishlist (user_id, media_id, media_type, '
            'date_added, priority) VALUES (:u, :m, :t, :a, :p)'),
            {'u': user_id, 'm': media_id, 't': media_type,
             'a': added, 'p': priority})


HISTORICAL_REPAIR_SET = [
    'continue_watching_item', 'director', 'import_source_mapping',
    'media_director', 'movie_release_date', 'notification',
    'recommendation_feedback', 'smart_list', 'taste_profile',
    'user_streaming_services', 'year_in_review_share',
]


def _drop_historical(connection, names=None):
    """Drop the historical-gap tables to reproduce production's gap.

    CASCADE is required and correct here: these tables have foreign keys
    between them (``media_director`` -> ``director``), and PostgreSQL refuses
    to drop a referenced table otherwise. These are throwaway container tables.
    """
    for name in (names or HISTORICAL_REPAIR_SET):
        connection.execute(text('DROP TABLE IF EXISTS "%s" CASCADE' % name))


def _approval(version=DESTRUCTIVE_VERSION):
    """Valid, obviously-synthetic approval so the gate can be exercised."""
    return runner.DestructiveApproval(
        authorized_version=version,
        backup_ref='ci-scratch-snapshot-%s' % uuid.uuid4().hex[:12],
        verified_by='ci-postgres-job')


def _apply_safe(engine):
    """Stage 1 — the ordinary deploy path. Never touches the destructive step."""
    return runner.apply_pending(engine)


def _apply_destructive(engine, approval=None):
    """Stage 2 — the approval-gated step, exactly as the workflow runs it.

    Requires BOTH naming the migration with ``only`` and supplying the three
    pieces of backup evidence, so the tests exercise the real two-key path.
    """
    return runner.apply_pending(engine, only={DESTRUCTIVE_VERSION},
                                approval=approval or _approval())


def _apply_all(engine, approval=None):
    """Both stages, as the two-job deployment performs them."""
    applied = _apply_safe(engine)
    try:
        applied += _apply_destructive(engine, approval)
    except runner.MigrationError:
        # A test asserting the refusal keeps the stage-1 result visible; re-raise
        # so the failure is still a failure.
        raise
    return applied


def _ledger(engine):
    with engine.connect() as connection:
        return {row[0]: (row[1], row[2]) for row in connection.execute(text(
            'SELECT version, checksum, kind FROM schema_migrations'))}


# ── advisory lock ────────────────────────────────────────────────────────────

def test_advisory_lock_is_acquired_and_released(pg_engine):
    with pg_engine.connect() as connection:
        with runner.advisory_lock(connection) as lock:
            assert lock.available is True, 'pg_advisory_lock did not take'
            held = connection.execute(text("""
                SELECT count(*) FROM pg_locks
                 WHERE locktype = 'advisory'
                   AND ((classid::bigint << 32) | objid::bigint)
                       = CAST(:k AS bigint)
            """), {'k': runner.LOCK_KEY}).scalar()
            assert held == 1, 'the advisory lock is not visible in pg_locks'
        released = connection.execute(text("""
            SELECT count(*) FROM pg_locks
             WHERE locktype = 'advisory'
               AND ((classid::bigint << 32) | objid::bigint)
                   = CAST(:k AS bigint)
        """), {'k': runner.LOCK_KEY}).scalar()
    assert released == 0, 'the advisory lock was not released'


def test_advisory_lock_uses_the_session_variant(pg_engine):
    """Session-scoped, so it survives the COMMIT between reading the ledger and
    applying each migration. A transaction-scoped lock would already be gone."""
    with pg_engine.connect() as connection:
        with runner.advisory_lock(connection) as lock:
            assert lock.available is True
            connection.commit()  # a real commit, not a rollback
            still = connection.execute(text("""
                SELECT count(*) FROM pg_locks
                 WHERE locktype = 'advisory'
                   AND ((classid::bigint << 32) | objid::bigint)
                       = CAST(:k AS bigint)
            """), {'k': runner.LOCK_KEY}).scalar()
        assert still == 1, 'lock vanished at COMMIT — it was transaction-scoped'


def test_a_second_connection_cannot_take_the_lock(pg_engine):
    """The actual exclusion property, observed from a genuinely other session.

    PostgreSQL advisory locks are re-entrant WITHIN a session, so testing from
    the lock-holding connection always succeeds and would prove nothing. This
    opens a separate connection, which is what a competing runner would be.
    """
    holder = pg_engine.connect()
    try:
        with runner.advisory_lock(holder):
            other = pg_engine.connect()
            try:
                assert other.execute(
                    text('SELECT pg_try_advisory_lock(:k)'),
                    {'k': runner.LOCK_KEY}).scalar() is False, \
                    'a second session acquired the held lock'
                # A DIFFERENT key is still free, proving we blocked only this key
                # and did not wedge the database for everyone.
                assert other.execute(
                    text('SELECT pg_try_advisory_lock(:k)'),
                    {'k': runner.LOCK_KEY + 1}).scalar() is True
                other.execute(text('SELECT pg_advisory_unlock(:k)'),
                              {'k': runner.LOCK_KEY + 1})
            finally:
                other.close()
    finally:
        holder.close()


def test_the_lock_is_released_so_a_later_runner_proceeds(pg_engine):
    """No stale lock left behind: after the first runner finishes, a second
    runner must be able to take the lock immediately."""
    first = pg_engine.connect()
    with runner.advisory_lock(first):
        pass
    first.close()

    second = pg_engine.connect()
    try:
        with runner.advisory_lock(second) as lock:
            assert lock.available is True
    finally:
        second.close()


def test_concurrent_runners_cannot_apply_the_same_migration(pg_engine, capsys):
    """Two real runners, real contention, one winner.

    The loser must not apply anything: the exclusion is what stops two deploys
    from both deciding a migration is pending and both performing it.
    """
    barrier = threading.Barrier(2, timeout=30)
    outcomes = {}

    def run_as(name):
        engine = create_engine(PG_URL, future=True)
        try:
            barrier.wait()
            try:
                outcomes[name] = ('ok', _apply_all(engine))
            except Exception as exc:  # noqa: BLE001
                outcomes[name] = ('error', str(exc)[:120])
            finally:
                engine.dispose()
        finally:
            barrier.wait(timeout=30)

    threads = [threading.Thread(target=run_as, args=(n,))
               for n in ('runner-a', 'runner-b')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    with pg_engine.connect() as connection:
        versions = [r[0] for r in connection.execute(text(
            'SELECT version FROM schema_migrations'))]

    assert len(versions) == len(set(versions)), 'a version was recorded twice'
    # Either the second runner found nothing pending, or it could not proceed.
    # What must never happen is both applying the same migration.
    # F9 adds a third migration (0003_cast_persistence, additive), so the
    # ledger now holds three rows: 0001, F9's 0003, and the gated 0002.
    assert sorted(versions) == ['0001_canonical_watched_reconcile',
                                DESTRUCTIVE_VERSION,
                                '0003_cast_persistence']
    succeeded = [v for v in outcomes.values() if v[0] == 'ok']
    assert succeeded, 'no runner succeeded at all: %r' % outcomes
    total_applied = sum(len(v[1]) for v in succeeded)
    assert total_applied == 3, 'migrations were applied more than once: %r' % outcomes


# ── transactional DDL ────────────────────────────────────────────────────────

def test_failed_migration_leaves_no_partial_ddl(pg_engine, monkeypatch):
    """The claim SQLite cannot make.

    A migration that runs DDL and then fails must leave NO trace on
    PostgreSQL, because DDL is transactional here.
    """
    import importlib

    module = importlib.import_module('migrations_0001_canonical_watched')

    def half_failing(connection):
        connection.execute(text('CREATE TABLE pg_partial_ddl (x INTEGER)'))
        connection.execute(text('INSERT INTO pg_partial_ddl VALUES (1)'))
        raise RuntimeError('deliberate failure after DDL')

    monkeypatch.setattr(module, 'run', half_failing)

    with pytest.raises(runner.MigrationError) as excinfo:
        runner.apply_pending(pg_engine)
    assert 'deliberate failure' in str(excinfo.value)

    with pg_engine.connect() as connection:
        tables = inspect(connection).get_table_names()
        recorded = connection.execute(text(
            'SELECT COUNT(*) FROM schema_migrations')).scalar()
    assert 'pg_partial_ddl' not in tables, 'DDL from a failed migration survived'
    assert recorded == 0, 'a failed migration was recorded as applied'


def test_failed_migration_is_not_recorded_and_retry_succeeds(pg_engine,
                                                             monkeypatch):
    """Retry after a failure must work and must not double-apply."""
    import importlib

    module = importlib.import_module('migrations_0001_canonical_watched')
    original = module.run
    calls = []

    def flaky(connection):
        calls.append(1)
        if len(calls) == 1:
            connection.execute(text('CREATE TABLE pg_flaky (x INTEGER)'))
            raise RuntimeError('transient failure')
        return original(connection)

    monkeypatch.setattr(module, 'run', flaky)

    with pytest.raises(runner.MigrationError):
        runner.apply_pending(pg_engine)
    with pg_engine.connect() as connection:
        assert 'pg_flaky' not in inspect(connection).get_table_names()
        assert connection.execute(text(
            'SELECT COUNT(*) FROM schema_migrations')).scalar() == 0

    monkeypatch.setattr(module, 'run', original)
    applied = _apply_all(pg_engine)
    assert '0001_canonical_watched_reconcile' in applied
    # F9's additive 0003 rides the ordinary stage, so it is recorded here too.
    assert '0003_cast_persistence' in applied
    assert set(_ledger(pg_engine)) == {'0001_canonical_watched_reconcile',
                                       '0003_cast_persistence',
                                       DESTRUCTIVE_VERSION}


def test_wishlist_merge_and_drop_roll_back_together(pg_engine, monkeypatch):
    """The decisive rollback test: the INSERT and the DROP share one fate.

    Deliberately made to fail AFTER the merge, then checks that the legacy
    table still exists AND that the merged row was rolled back. A DROP that
    survived its own merge would be unrecoverable data loss.
    """
    import importlib

    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')
    with pg_engine.begin() as connection:
        _seed_wishlist(connection)

    def failing_verify(connection):
        raise RuntimeError('deliberate post-merge failure')

    monkeypatch.setattr(module, 'verify', failing_verify)

    _apply_safe(pg_engine)  # 0001 first, so 0002's dependency is satisfied
    with pytest.raises(runner.MigrationError) as excinfo:
        _apply_destructive(pg_engine)
    assert 'deliberate post-merge failure' in str(excinfo.value)

    with pg_engine.connect() as connection:
        assert 'user_wishlist' in inspect(connection).get_table_names(), \
            'the DROP was committed despite the failure'
        legacy_rows = connection.execute(text(
            'SELECT COUNT(*) FROM user_wishlist')).scalar()
        merged_rows = connection.execute(text(
            'SELECT COUNT(*) FROM user_watchlist WHERE media_id = 100'
        )).scalar()
        recorded = connection.execute(text(
            'SELECT COUNT(*) FROM schema_migrations WHERE version = :v'),
            {'v': DESTRUCTIVE_VERSION}).scalar()
    assert legacy_rows == 1, 'the legacy row was lost'
    assert merged_rows == 0, 'the merge was not rolled back with the DROP'
    assert recorded == 0, 'the destructive migration was recorded as applied'

    # Stronger than re-checking verify(): prove the database is completely
    # usable again. A rollback that left the schema or the data half-changed
    # would make a retry fail here, which is exactly what an operator would hit.
    monkeypatch.setattr(module, 'verify', lambda connection: [])
    retried = _apply_destructive(pg_engine)
    assert DESTRUCTIVE_VERSION in retried
    with pg_engine.connect() as connection:
        assert 'user_wishlist' not in inspect(connection).get_table_names()
        assert connection.execute(text(
            'SELECT priority FROM user_watchlist WHERE media_id = 100'
        )).scalar() == 'low'


def test_wishlist_merge_and_drop_commit_together_on_success(pg_engine):
    """The other half: when it succeeds, both the merge and the DROP land."""
    with pg_engine.begin() as connection:
        _seed_wishlist(connection,
                       rows=((3, 13, 'movie', '2026-01-27 08:15:51', 'low'),))

    applied = _apply_all(pg_engine)
    assert DESTRUCTIVE_VERSION in applied

    with pg_engine.connect() as connection:
        assert 'user_wishlist' not in inspect(connection).get_table_names()
        merged = connection.execute(text(
            'SELECT user_id, media_id, media_type, priority FROM '
            'user_watchlist WHERE media_id = 13')).fetchone()
        date_added = connection.execute(text(
            'SELECT date_added FROM user_watchlist WHERE media_id = 13'
        )).scalar()
    assert merged == (3, 13, 'movie', 'low')
    assert date_added is not None and date_added.year == 2026


def test_schema_guard_passes_after_the_successful_migration_path(pg_engine):
    _apply_all(pg_engine)
    from utils.schema_guard import check_schema
    verdict = check_schema(pg_engine)
    assert verdict['ok'], verdict


def test_rerunning_applied_migrations_is_safe(pg_engine):
    _apply_all(pg_engine)
    before = _ledger(pg_engine)
    assert _apply_safe(pg_engine) == []
    assert _apply_destructive(pg_engine) == []
    assert _ledger(pg_engine) == before


def test_destructive_migration_refuses_without_approval_on_postgres(pg_engine):
    """The gate must hold on the real target database too, not just SQLite."""
    with pg_engine.begin() as connection:
        _seed_wishlist(connection)

    # Deliberate attempt at the destructive step, no approval at all.
    _apply_safe(pg_engine)
    with pytest.raises(runner.DestructiveApprovalError):
        runner.apply_pending(pg_engine, only={DESTRUCTIVE_VERSION})

    with pg_engine.connect() as connection:
        assert 'user_wishlist' in inspect(connection).get_table_names()
        assert connection.execute(text(
            'SELECT COUNT(*) FROM user_watchlist WHERE media_id = 100'
        )).scalar() == 0, 'a partial merge happened before the refusal'


def test_placeholder_evidence_is_refused_on_postgres(pg_engine):
    for placeholder in ('yes', 'true', 'TODO', 'none'):
        approval = runner.DestructiveApproval(
            authorized_version=DESTRUCTIVE_VERSION,
            backup_ref=placeholder, verified_by='ci')
        assert approval.problems_for(DESTRUCTIVE_VERSION), placeholder


def test_audit_note_is_recorded_in_the_ledger(pg_engine):
    approval = _approval()
    _apply_all(pg_engine, approval=approval)
    with pg_engine.connect() as connection:
        note = connection.execute(text(
            'SELECT note FROM schema_migrations WHERE version = :v'),
            {'v': DESTRUCTIVE_VERSION}).scalar()
    assert note is not None
    assert DESTRUCTIVE_VERSION in note
    assert approval.backup_ref in note
    assert approval.verified_by in note


def test_conflict_stops_before_drop_on_postgres(pg_engine):
    """Fail-closed on a conflicting target row, with the legacy table intact."""
    with pg_engine.begin() as connection:
        _seed_wishlist(connection,
                       rows=((1, 100, 'movie', '2026-01-01', 'low'),))
        connection.execute(text(
            'INSERT INTO user_watchlist (user_id, media_id, media_type, '
            "date_added, priority) VALUES (1, 100, 'movie', '2026-05-05', 'high')"))

    _apply_safe(pg_engine)
    with pytest.raises(runner.MigrationError):
        _apply_destructive(pg_engine)

    with pg_engine.connect() as connection:
        assert 'user_wishlist' in inspect(connection).get_table_names()
        priority = connection.execute(text(
            'SELECT priority FROM user_watchlist WHERE media_id = 100')).scalar()
    assert priority == 'high', 'the existing target row was overwritten'


# ── init-ledger on real PostgreSQL ──────────────────────────────────────────
#
# The bootstrap that unblocked the production deploy. DDL is transactional on
# PostgreSQL, so the ledger creation is atomic here in a way it is not on
# SQLite (pysqlite commits around DDL).

def test_init_ledger_creates_only_the_ledger_on_postgres(pg_engine):
    """Production's exact gap: no ledger, and the historical tables absent."""
    with pg_engine.begin() as connection:
        connection.execute(text('DROP TABLE IF EXISTS schema_migrations'))
        _drop_historical(connection)

    result = runner.init_ledger(pg_engine)
    assert result['ledger'] == 'created'

    with pg_engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
        assert 'schema_migrations' in names
        for name in HISTORICAL_REPAIR_SET:
            assert name not in names, (
                'init-ledger created a historical table: %s' % name)
        rows = connection.execute(text(
            'SELECT version, kind FROM schema_migrations')).fetchall()
    assert rows == [], 'init-ledger wrote rows'


def test_init_ledger_is_idempotent_on_postgres(pg_engine):
    assert runner.init_ledger(pg_engine)['ledger'] == 'present'
    assert runner.init_ledger(pg_engine)['ledger'] == 'present'
    with pg_engine.connect() as connection:
        assert connection.execute(text(
            'SELECT COUNT(*) FROM schema_migrations')).scalar() == 0


def test_init_ledger_fails_closed_on_a_drifted_ledger_on_postgres(pg_engine):
    with pg_engine.begin() as connection:
        connection.execute(text('DROP TABLE schema_migrations'))
        connection.execute(text(
            'CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY)'))
    with pytest.raises(runner.MigrationError) as excinfo:
        runner.init_ledger(pg_engine)
    assert 'does not match the declared ledger schema' in str(excinfo.value)
    # The drifted table is left exactly as found — never repaired.
    with pg_engine.connect() as connection:
        columns = [c['name'] for c in
                   inspect(connection).get_columns('schema_migrations')]
    assert columns == ['id']


def test_ledger_bootstrap_rolls_back_as_one_transaction(pg_engine):
    """A failure after the ledger DDL must not leave the table behind.

    Only observable on PostgreSQL: SQLite commits around DDL, so it cannot
    demonstrate this at all.

    The table is dropped first -- the fixture's create_all would otherwise
    leave it present, in which case there is no DDL to roll back and the test
    would pass for the wrong reason.
    """
    from sqlalchemy.exc import ProgrammingError

    with pg_engine.begin() as connection:
        connection.execute(text('DROP TABLE IF EXISTS schema_migrations'))

    real = runner._ledger_table

    def explode(connection):
        real(connection)
        raise ProgrammingError('SELECT 1', {}, Exception('deliberate'))

    runner._ledger_table = explode
    try:
        with pytest.raises(Exception):
            runner.init_ledger(pg_engine)
    finally:
        runner._ledger_table = real

    with pg_engine.connect() as connection:
        assert not inspect(connection).has_table('schema_migrations'), \
            'the ledger DDL was committed despite the failure'


def test_convergence_then_runs_after_the_bootstrap_on_postgres(pg_engine):
    """The production recovery path, on the real target engine.

    Convergence is a standalone script, so it is invoked exactly as the deploy
    does. Run via the runner's own imports to avoid touching production: this
    URL is a throwaway container.
    """
    import os
    import subprocess
    import sys

    with pg_engine.begin() as connection:
        connection.execute(text('DROP TABLE IF EXISTS schema_migrations'))
        _drop_historical(connection)

    assert runner.init_ledger(pg_engine)['ledger'] == 'created'

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    # sslmode is set explicitly because the convergence script imports `app`,
    # which appends sslmode=require on a Render/Cloud Run host (`.env` here sets
    # RENDER=true). A throwaway local container has no TLS, and app.py trusts an
    # sslmode that is already present rather than overriding it.
    env['DATABASE_URL'] = PG_URL + ('&' if '?' in PG_URL else '?') \
        + 'sslmode=disable'
    env['SKIP_SCHEMA_GUARD'] = '1'
    result = subprocess.run(
        [sys.executable,
         os.path.join(repo, 'migrates', 'migrate_schema_convergence.py')],
        capture_output=True, text=True, env=env, cwd=repo, timeout=600)
    assert result.returncode == 0, result.stdout[-500:] + result.stderr[-300:]

    with pg_engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
    for name in HISTORICAL_REPAIR_SET:
        assert name in names, 'convergence did not create %s' % name

    from utils.schema_guard import check_schema
    assert check_schema(pg_engine)['ok']

    # The ledger itself is untouched by convergence.
    with pg_engine.connect() as connection:
        assert connection.execute(text(
            'SELECT COUNT(*) FROM schema_migrations')).scalar() == 0


# ════════════════════════════════════════════════════════════════════════════
# Feature F9 — cast persistence, on real PostgreSQL
#
# Three guarantees here are UNPROVABLE on SQLite and are the reason this
# module exists (see its docstring):
#   * transactional DDL — a failed additive migration must leave nothing behind;
#   * schema-scoped index names — SQLite permits duplicates, so only here can
#     the pre-flight name-collision guard be shown to fire for the right reason;
#   * real FOREIGN KEY enforcement — SQLite does not enforce FKs by default,
#     so CASCADE behaviour is otherwise untested.
# ════════════════════════════════════════════════════════════════════════════


def _strip_f9_objects(connection):
    """Return the database to its pre-F9 shape."""
    for name in ('media_cast', 'person'):
        connection.execute(text('DROP TABLE IF EXISTS "%s" CASCADE' % name))
    connection.execute(text('ALTER TABLE media_item '
                            'DROP COLUMN IF EXISTS cast_enriched_at'))
    connection.execute(text('ALTER TABLE taste_profile '
                            'DROP COLUMN IF EXISTS actor_affinity_json'))


def _cast_module():
    import importlib
    return importlib.import_module('migrations_0003_cast_persistence')


def test_f9_objects_absent_before_the_migration_on_postgres(pg_engine):
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
    with pg_engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
        assert 'person' not in names
        assert 'media_cast' not in names


def test_f9_migration_applies_and_converges_on_postgres(pg_engine):
    module = _cast_module()
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
    with pg_engine.connect() as connection:
        with connection.begin():
            assert module.verify(connection) != []   # really missing first
            module.run(connection)
            assert module.verify(connection) == []
        names = set(inspect(connection).get_table_names())
        assert {'person', 'media_cast'} <= names
        media_columns = {c['name'] for c in
                         inspect(connection).get_columns('media_item')}
        assert 'cast_enriched_at' in media_columns
        profile_columns = {c['name'] for c in
                           inspect(connection).get_columns('taste_profile')}
        assert 'actor_affinity_json' in profile_columns


def test_f9_migration_is_idempotent_on_postgres(pg_engine):
    module = _cast_module()
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
    for _ in range(2):
        with pg_engine.connect() as connection:
            with connection.begin():
                module.run(connection)
                assert module.verify(connection) == []


def test_f9_writes_no_rows_on_postgres(pg_engine):
    module = _cast_module()
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
    with pg_engine.connect() as connection:
        with connection.begin():
            module.run(connection)
    with pg_engine.connect() as connection:
        assert connection.execute(
            text('SELECT COUNT(*) FROM person')).scalar() == 0
        assert connection.execute(
            text('SELECT COUNT(*) FROM media_cast')).scalar() == 0


def test_f9_ddl_is_transactional_on_postgres(pg_engine, monkeypatch):
    """The guarantee SQLite cannot give: a failure leaves NOTHING behind.

    ``person`` is created first, so making the SECOND table fail must roll the
    first one back too — and the ledger must not claim success.
    """
    module = _cast_module()
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)

    original = module._table

    def _explode(name):
        if name == 'media_cast':
            raise RuntimeError('simulated failure after person was created')
        return original(name)

    monkeypatch.setattr(module, '_table', _explode)

    with pg_engine.connect() as connection:
        with pytest.raises(RuntimeError, match='simulated failure'):
            with connection.begin():
                module.run(connection)

    with pg_engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
    assert 'person' not in names, 'transactional DDL did not roll back'
    assert 'media_cast' not in names


def test_f9_index_name_collision_is_refused_on_postgres(pg_engine):
    """The documented idx_taste_profile_updated hazard, proven for real.

    SQLite tolerates duplicate index names, so only PostgreSQL can show that
    the pre-flight refuses for the RIGHT reason — before any DDL — instead of
    letting a CREATE INDEX fail mid-transaction.
    """
    module = _cast_module()
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
        connection.execute(text('DROP TABLE IF EXISTS legacy_owner'))
        connection.execute(text('CREATE TABLE legacy_owner ('
                                'id INTEGER PRIMARY KEY)'))
        connection.execute(text('CREATE UNIQUE INDEX uq_media_cast_pair '
                                'ON legacy_owner(id)'))

    with pg_engine.connect() as connection:
        with connection.begin():
            conflicts = module.name_conflicts(connection)
        assert any(name == 'uq_media_cast_pair' and owner == 'legacy_owner'
                   for _kind, name, _wanted, owner in conflicts), conflicts
        with pytest.raises(RuntimeError, match='already owned by'):
            with connection.begin():
                module.run(connection)

    with pg_engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
    assert 'person' not in names
    assert 'media_cast' not in names


def test_f9_cascade_is_enforced_by_postgres(pg_engine):
    """SQLite does not enforce FKs by default; PostgreSQL does."""
    with pg_engine.begin() as connection:
        connection.execute(text('DELETE FROM media_cast'))
        connection.execute(text('DELETE FROM person'))
        connection.execute(text(
            "INSERT INTO media_item (tmdb_id, media_type, title) "
            "VALUES (900001, 'movie', 'Cascade Film')"))
        # `source` has an ORM-side default only, so a raw INSERT must supply it.
        connection.execute(text(
            "INSERT INTO person (tmdb_person_id, name, source, created_at, "
            "updated_at) VALUES (900001, 'Cascade Actor', 'tmdb', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        connection.execute(text(
            'INSERT INTO media_cast (media_item_id, person_id, credit_order, '
            'created_at, updated_at) '
            'SELECT id, id, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP '
            'FROM media_item WHERE tmdb_id = 900001'))
        assert connection.execute(
            text('SELECT COUNT(*) FROM media_cast')).scalar() == 1

        connection.execute(text('DELETE FROM media_item WHERE tmdb_id = 900001'))
        assert connection.execute(
            text('SELECT COUNT(*) FROM media_cast')).scalar() == 0
        assert connection.execute(
            text('SELECT COUNT(*) FROM person')).scalar() == 1


def test_f9_unique_pair_is_enforced_by_postgres(pg_engine):
    from sqlalchemy.exc import IntegrityError
    with pg_engine.begin() as connection:
        connection.execute(text('DELETE FROM media_cast'))
        connection.execute(text('DELETE FROM person'))
        connection.execute(text(
            "INSERT INTO media_item (tmdb_id, media_type, title) "
            "VALUES (900002, 'movie', 'Unique Film')"))
        connection.execute(text(
            "INSERT INTO person (tmdb_person_id, name, source, created_at, "
            "updated_at) VALUES (900002, 'Unique Actor', 'tmdb', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
    stmt = text(
        'INSERT INTO media_cast (media_item_id, person_id, credit_order, '
        'created_at, updated_at) '
        'SELECT id, id, :o, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP '
        'FROM media_item WHERE tmdb_id = 900002')
    with pg_engine.connect() as connection:
        with connection.begin():
            connection.execute(stmt, {'o': 0})
        with pytest.raises(IntegrityError):
            with connection.begin():
                connection.execute(stmt, {'o': 5})


def test_f9_applies_by_ordinary_upgrade_while_0002_stays_deferred(pg_engine):
    """The ordering guarantee, proven on the production engine."""
    with pg_engine.begin() as connection:
        connection.execute(text('DROP TABLE IF EXISTS schema_migrations'))
        _strip_f9_objects(connection)

    applied = runner.apply_pending(pg_engine)

    assert '0003_cast_persistence' in applied, applied
    assert '0002_remove_legacy_wishlist' not in applied, applied

    with pg_engine.connect() as connection:
        recorded = {row[0] for row in connection.execute(
            text('SELECT version FROM schema_migrations'))}
        names = set(inspect(connection).get_table_names())
    assert {'0001_canonical_watched_reconcile',
            '0003_cast_persistence'} <= recorded
    assert '0002_remove_legacy_wishlist' not in recorded
    assert {'person', 'media_cast'} <= names


def test_schema_guard_passes_after_f9_on_postgres(pg_engine):
    from utils.schema_guard import check_schema
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
        connection.execute(text('DROP TABLE IF EXISTS schema_migrations'))
    runner.init_ledger(pg_engine)
    verdict = check_schema(pg_engine)
    assert verdict['ok'] is False
    assert 'person' in verdict['missing_tables']

    runner.apply_pending(pg_engine)
    assert check_schema(pg_engine)['ok'] is True


def _seed_taste_profile(connection, count=2):
    """Populate taste_profile the way production is populated.

    Necessary, not decoration: `actor_affinity_json` is added by ALTER TABLE to
    an already-populated table, and PostgreSQL refuses `ADD COLUMN ... NOT NULL`
    with no DEFAULT on such a table. A test on an EMPTY taste_profile passes
    while production fails, which is precisely the bug this seeds against.
    """
    # PostgreSQL enforces the FK, so the parent rows must exist first.
    _seed_identity(connection, [200000 + i for i in range(count)], [])
    for index in range(count):
        connection.execute(text(
            'INSERT INTO taste_profile (user_id, genre_weights_json, '
            'decade_weights_json, director_affinity_json, runtime_pref_json, '
            'media_type_pref_json, confidence, signal_count, '
            'distinct_title_count, profile_version, created_at, updated_at) '
            'VALUES (:u, :g, :d, :dir, :r, :m, 0.4, 7, 6, 1, '
            'CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)'), {
                'u': 200000 + index,
                'g': '{"Drama": 0.5}', 'd': '{"2010s": 0.5}',
                'dir': '{"Old Director": 1.0}',
                'r': '{"p25": 90, "p75": 120, "sample_count": 3}',
                'm': '{"movie": 0.8, "tv": 0.2}'})


def test_f9_adds_not_null_column_to_populated_profile_on_postgres(pg_engine):
    """Blocker 1 on the production engine: the ADD COLUMN must backfill.

    This is the only place the PostgreSQL-specific refusal can be observed
    directly. SQLite rejects the same statement, so the SQLite suite also
    covers it — but production is PostgreSQL, and the two error differently.
    """
    module = _cast_module()
    with pg_engine.begin() as connection:
        _strip_f9_objects(connection)
        _seed_taste_profile(connection, count=3)

    before = pg_engine.connect().execute(
        text('SELECT COUNT(*) FROM taste_profile')).scalar()
    assert before == 3, before

    with pg_engine.connect() as connection:
        with connection.begin():
            module.run(connection)
            assert module.verify(connection) == []

    with pg_engine.connect() as connection:
        rows = connection.execute(text(
            'SELECT user_id, genre_weights_json, director_affinity_json, '
            'signal_count, profile_version, actor_affinity_json '
            'FROM taste_profile ORDER BY user_id')).fetchall()
        # Existing rows survive untouched, with the new column initialised.
        assert len(rows) == 3
        for user_id, genre, director, signals, version, actor in rows:
            assert user_id == 200000 + (user_id - 200000)
            assert genre == '{"Drama": 0.5}'
            assert director == '{"Old Director": 1.0}'
            assert (signals, version) == (7, 1)
            assert actor == '{}', 'existing rows must be backfilled: %r' % (actor,)

        # And the column carries the NOT NULL + DEFAULT contract.
        defaults = connection.execute(text(
            "SELECT column_name, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_name = "
            "'taste_profile' AND column_name = 'actor_affinity_json'")).fetchall()
        assert defaults, 'actor_affinity_json is missing'
        _name, nullable, default = defaults[0]
        assert nullable == 'NO', 'the NOT NULL invariant must survive'
        assert default is not None and '{}' in str(default), (
            'a database-side default is what lets ADD COLUMN backfill existing '
            'rows on PostgreSQL; got %r' % (default,))
