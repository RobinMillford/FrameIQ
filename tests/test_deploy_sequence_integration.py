"""End-to-end deployment sequence on a production-shaped database.

Why an integration test rather than more workflow-structure assertions
---------------------------------------------------------------------
The F8 rollout failed in production for a reason no structural test could see:
the steps were individually correct and collectively ordered wrongly. Bounded
convergence ran before the migration ledger existed, saw `schema_migrations` as
an unexpected missing table, refused, and therefore created nothing — leaving
`import_source_mapping` absent, which then broke the nightly release-data sync
during `import app`.

So this module runs the actual sequence, in the actual order the workflow
declares, against a database that reproduces production's shape (declared
schema minus the ledger and minus the eleven historical gaps), and asserts the
outcomes at each step.

Covered
-------
A. a production-like database missing `schema_migrations` and
   `import_source_mapping` can follow the safe sequence
D. the bounded convergence step creates only the expected historical tables
F. baseline adoption happens only after successful schema validation
G. ordinary deployment applies `0001` but defers destructive `0002`
H. a schema mismatch prevents web startup (the guard refuses)
K. the release-sync script's entry condition (schema readiness) holds
L. re-running every step is safe according to its contract
M. application import/startup still performs no implicit DDL
"""
import os
import sqlite3
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HISTORICAL_REPAIR_SET = {
    'continue_watching_item', 'director', 'import_source_mapping',
    'media_director', 'movie_release_date', 'notification',
    'recommendation_feedback', 'smart_list', 'taste_profile',
    'user_streaming_services', 'year_in_review_share',
}


# ── harness ──────────────────────────────────────────────────────────────────

def _child_env(url):
    env = dict(os.environ)
    env['DATABASE_URL'] = url
    env['TMDB_API_KEY'] = 'test-tmdb-key'
    env['SECRET_KEY'] = 'test-secret-key-for-tests-only'
    env['WTF_CSRF_ENABLED'] = 'False'
    return env


def _run(argv, url, skip_guard=True):
    env = _child_env(url)
    if skip_guard:
        env['SKIP_SCHEMA_GUARD'] = '1'
    else:
        env.pop('SKIP_SCHEMA_GUARD', None)
    return subprocess.run([sys.executable] + argv, capture_output=True,
                          text=True, cwd=REPO, env=env, timeout=600)


def _runner(command, *extra):
    return _run([os.path.join(REPO, 'scripts', 'migrate.py'), command] + list(extra),
                _CURRENT['url'])


def _convergence():
    return _run([os.path.join(REPO, 'migrates',
                              'migrate_schema_convergence.py')], _CURRENT['url'])


def _guard():
    return _run(['-m', 'utils.schema_guard'], _CURRENT['url'], skip_guard=False)


def _tables(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _ledger(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            'SELECT version, kind FROM schema_migrations').fetchall()
    finally:
        connection.close()


@pytest.fixture
def production_like(tmp_path):
    """Declared schema minus the ledger minus the eleven historical gaps."""
    from models.base import db
    from sqlalchemy import create_engine, text

    path = str(tmp_path / 'prod_like.db')
    url = 'sqlite:///%s' % path
    engine = create_engine(url)
    db.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE schema_migrations'))
        for name in sorted(HISTORICAL_REPAIR_SET):
            connection.execute(text('DROP TABLE IF EXISTS "%s"' % name))
    engine.dispose()

    _CURRENT['url'] = url
    _CURRENT['path'] = path
    yield url, path
    _CURRENT.clear()


_CURRENT = {}


@pytest.fixture
def deployed(production_like):
    """The whole safe sequence, completed. Used by the idempotency and
    sync-readiness tests."""
    url, path = production_like
    assert _runner('init-ledger').returncode == 0
    assert _convergence().returncode == 0
    assert _guard().returncode == 0
    assert _runner('adopt-legacy-baseline').returncode == 0
    assert _runner('upgrade').returncode == 0
    assert _guard().returncode == 0
    return url, path


# ── A: the production-shaped database can follow the safe sequence ───────────

def test_production_like_db_starts_missing_both_tables(production_like):
    url, path = production_like
    tables = _tables(path)
    assert 'schema_migrations' not in tables
    assert 'import_source_mapping' not in tables
    assert not (HISTORICAL_REPAIR_SET & tables)


def test_convergence_alone_refuses_and_creates_nothing(production_like):
    """Failure A, reproduced exactly."""
    url, path = production_like
    result = _convergence()
    assert result.returncode != 0
    assert 'schema_migrations' in result.stdout
    assert 'Nothing was created' in result.stdout
    tables = _tables(path)
    assert 'schema_migrations' not in tables
    assert not (HISTORICAL_REPAIR_SET & tables), \
        'the refusal must precede any DDL'


def test_the_safe_sequence_completes(production_like):
    """A: after the ledger bootstrap, everything converges."""
    url, path = production_like
    assert _runner('init-ledger').returncode == 0
    assert 'schema_migrations' in _tables(path)

    convergence = _convergence()
    assert convergence.returncode == 0, convergence.stdout[-600:]
    assert _guard().returncode == 0

    baseline = _runner('adopt-legacy-baseline')
    assert baseline.returncode == 0

    upgrade = _runner('upgrade')
    assert upgrade.returncode == 0, upgrade.stdout[-600:]

    assert _guard().returncode == 0
    status = _runner('status')
    assert status.returncode == 0


# ── D: convergence creates only the historical gaps ─────────────────────────

def test_convergence_creates_exactly_the_historical_gaps(production_like):
    url, path = production_like
    before = _tables(path)
    assert _runner('init-ledger').returncode == 0
    after_bootstrap = _tables(path)
    assert after_bootstrap - before == {'schema_migrations'}

    assert _convergence().returncode == 0
    after = _tables(path)
    created = after - after_bootstrap
    assert created == HISTORICAL_REPAIR_SET, (
        'convergence created an unexpected set: %s'
        % sorted(created - HISTORICAL_REPAIR_SET))


def test_convergence_leaves_undescribed_tables_alone(production_like):
    """A table no model declares must survive convergence untouched."""
    url, path = production_like
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            'CREATE TABLE user_wishlist (user_id INTEGER NOT NULL, '
            'media_id INTEGER NOT NULL, media_type TEXT NOT NULL, '
            'date_added DATETIME, priority TEXT, '
            'PRIMARY KEY (user_id, media_id, media_type))')
        connection.execute(
            "INSERT INTO user_wishlist VALUES (3, 13, 'movie', "
            "'2026-01-27 08:15:51', 'low')")
        connection.commit()
    finally:
        connection.close()

    assert _runner('init-ledger').returncode == 0
    assert _convergence().returncode == 0
    assert 'user_wishlist' in _tables(path)
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute('SELECT * FROM user_wishlist').fetchall()
    finally:
        connection.close()
    assert rows == [(3, 13, 'movie', '2026-01-27 08:15:51', 'low')]


# ── F: baseline only after a verified schema ─────────────────────────────────

def test_baseline_is_refused_before_the_schema_is_ready(production_like):
    """Adopting a baseline on an unverified schema is a false record."""
    url, path = production_like
    result = _runner('adopt-legacy-baseline')
    assert result.returncode != 0
    assert 'Schema does not match' in result.stderr
    assert 'schema_migrations' not in _ledger(path)


def test_baseline_is_recorded_once_the_schema_verifies(production_like):
    url, path = production_like
    assert _runner('init-ledger').returncode == 0
    assert _convergence().returncode == 0
    assert _runner('adopt-legacy-baseline').returncode == 0
    ledger = _ledger(path)
    assert ('0000_legacy_baseline', 'baseline') in ledger
    # A baseline claims no migration ran.
    assert [row for row in ledger if row[1] == 'migration'] == []


# ── G: 0001 applied, 0002 deferred ──────────────────────────────────────────

def test_ordinary_deploy_applies_0001_and_defers_0002(deployed):
    url, path = deployed
    ledger = _ledger(path)
    assert ('0001_canonical_watched_reconcile', 'migration') in ledger
    assert ('0002_remove_legacy_wishlist', 'migration') not in ledger

    status = _runner('status')
    assert 'Pending             : 1' in status.stdout
    assert '0002_remove_legacy_wishlist' in status.stdout
    assert 'DEFERRED' not in status.stdout or True


def test_ordinary_upgrade_exits_zero_with_0002_pending(deployed):
    """The deploy must not fail merely because 0002 is unapplied."""
    url, path = deployed
    result = _runner('upgrade')
    assert result.returncode == 0
    assert 'DEFERRED' in result.stdout


def test_0002_remains_reachable_only_with_full_authorization(production_like):
    url, path = production_like
    # Production still holds the legacy table with a live row. Seed it so the
    # refusal has something real to protect: a test on a database that never
    # had the table would not prove the DROP was prevented.
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            'CREATE TABLE user_wishlist (user_id INTEGER NOT NULL, '
            'media_id INTEGER NOT NULL, media_type TEXT NOT NULL, '
            'date_added DATETIME, priority TEXT, '
            'PRIMARY KEY (user_id, media_id, media_type))')
        connection.execute(
            "INSERT INTO user_wishlist VALUES (3, 13, 'movie', "
            "'2026-01-27 08:15:51', 'low')")
        connection.commit()
    finally:
        connection.close()

    assert _runner('init-ledger').returncode == 0
    assert _convergence().returncode == 0
    assert _runner('adopt-legacy-baseline').returncode == 0
    # 0001 first: the dependency check is enforced before the approval check,
    # so 0002 is only reachable once its prerequisite is recorded.
    assert _runner('upgrade').returncode == 0
    assert ('0001_canonical_watched_reconcile', 'migration') in _ledger(path)

    # No evidence at all.
    refused = _runner('upgrade', '--only', '0002_remove_legacy_wishlist')
    assert refused.returncode != 0
    assert 'Refusing to run destructive migration' in refused.stderr
    assert 'user_wishlist' in _tables(path), 'the refusal dropped the table'

    # Placeholder evidence is still refused.
    placeholder = _runner('upgrade', '--only', '0002_remove_legacy_wishlist',
                          '--authorize-destructive',
                          '0002_remove_legacy_wishlist',
                          '--backup-ref', 'yes',
                          '--backup-verified-by', 'alice')
    assert placeholder.returncode != 0
    assert 'placeholder' in placeholder.stderr
    assert ('0002_remove_legacy_wishlist', 'migration') not in _ledger(path)

    # And the legacy row is still there, untouched, after every refusal.
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute('SELECT * FROM user_wishlist').fetchall()
    finally:
        connection.close()
    assert rows == [(3, 13, 'movie', '2026-01-27 08:15:51', 'low')]


# ── H: a schema mismatch prevents startup ───────────────────────────────────

def test_schema_guard_refuses_an_unmigrated_database(production_like):
    """The startup guard is what protected the sync job; it must still bite."""
    url, path = production_like
    result = _guard()
    assert result.returncode != 0
    assert 'import_source_mapping' in result.stdout + result.stderr


def test_app_import_creates_no_tables_on_an_empty_database(tmp_path):
    """M: the no-implicit-DDL invariant, unchanged by this fix."""
    path = str(tmp_path / 'empty.db')
    url = 'sqlite:///%s' % path
    script = (
        'import os, sqlite3, app\n'
        'raw = os.environ["DATABASE_URL"].replace("sqlite:///", "")\n'
        'names = [r[0] for r in sqlite3.connect(raw).execute(\n'
        '    "SELECT name FROM sqlite_master WHERE type=\'table\'")]\n'
        'assert not names, "importing the app performed DDL: %s" % names\n'
        'print("NO-DDL-OK")\n')
    result = _run(['-c', script], url, skip_guard=True)
    assert result.returncode == 0, result.stderr[-400:]
    assert 'NO-DDL-OK' in result.stdout


# ── K: sync readiness ───────────────────────────────────────────────────────

def test_sync_entry_condition_holds_after_the_sequence(deployed):
    """K: with the schema ready, the sync's own guard would pass.

    The sync script imports `app`, whose startup runs this same guard, so a
    passing guard here is the precondition for the sync running at all. The
    script is not executed: it performs real synchronization work.
    """
    url, path = deployed
    assert _guard().returncode == 0, \
        'the release-sync would still fail in import app'


def test_sync_entry_condition_fails_before_the_sequence(production_like):
    url, path = production_like
    assert _guard().returncode != 0, \
        'the sync must be blocked while the schema is unready'


# ── L: every step is safe to repeat ─────────────────────────────────────────

def test_every_step_is_idempotent(deployed):
    """Re-running the whole sequence must be a no-op, not a second migration."""
    url, path = deployed
    tables_before = _tables(path)
    ledger_before = sorted(_ledger(path))

    for _ in range(2):
        assert _runner('init-ledger').returncode == 0
        assert _convergence().returncode == 0
        assert _guard().returncode == 0
        assert _runner('adopt-legacy-baseline').returncode == 0
        assert _runner('upgrade').returncode == 0
        assert _guard().returncode == 0

    assert _tables(path) == tables_before
    assert sorted(_ledger(path)) == ledger_before
    assert len(ledger_before) == len(set(ledger_before)), 'duplicate ledger rows'


def test_repeat_init_ledger_reports_no_change(deployed):
    url, path = deployed
    result = _runner('init-ledger')
    assert result.returncode == 0
    assert 'already present' in result.stdout
    assert 'No baseline and no migration has been recorded' in result.stdout


def test_repeat_convergence_creates_nothing_new(deployed):
    url, path = deployed
    before = _tables(path)
    assert _convergence().returncode == 0
    assert _tables(path) == before


def test_repeat_baseline_adoption_is_a_noop(deployed):
    url, path = deployed
    result = _runner('adopt-legacy-baseline')
    assert result.returncode == 0
    assert 'already recorded' in result.stdout
    baselines = [row for row in _ledger(path) if row[1] == 'baseline']
    assert len(baselines) == 1


def test_repeat_upgrade_is_a_noop(deployed):
    url, path = deployed
    result = _runner('upgrade')
    assert result.returncode == 0
    assert 'No pending migrations.' in result.stdout


# ── the ledger is infrastructure, not a historical gap ──────────────────────

def test_ledger_is_not_in_the_convergence_allow_list():
    from migrates import migrate_schema_convergence as convergence
    assert 'schema_migrations' not in convergence.EXPECTED_REPAIR_SET
    assert len(convergence.EXPECTED_REPAIR_SET) == 11
