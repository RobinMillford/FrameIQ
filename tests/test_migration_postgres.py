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
    assert sorted(versions) == ['0001_canonical_watched_reconcile',
                                DESTRUCTIVE_VERSION]
    succeeded = [v for v in outcomes.values() if v[0] == 'ok']
    assert succeeded, 'no runner succeeded at all: %r' % outcomes
    total_applied = sum(len(v[1]) for v in succeeded)
    assert total_applied == 2, 'migrations were applied more than once: %r' % outcomes


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
    assert set(_ledger(pg_engine)) == {'0001_canonical_watched_reconcile',
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
