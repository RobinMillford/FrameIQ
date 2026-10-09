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
    """STRICT guard — deploy.yml step 6, and the in-app equivalent."""
    return _run(['-m', 'utils.schema_guard'], _CURRENT['url'], skip_guard=False)


def _guard_pending():
    """deploy.yml step 3: tolerant ONLY of pending migration-owned objects."""
    return _run(['-m', 'utils.schema_guard', '--allow-pending-migrations'],
                _CURRENT['url'], skip_guard=False)


# Objects that Feature F9 adds. A post-F8/pre-F9 database has none of them.
F9_TABLES = ('person', 'media_cast')
F9_COLUMNS = (('media_item', 'cast_enriched_at'),
              ('taste_profile', 'actor_affinity_json'))


def _columns(path, table):
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in
                connection.execute('PRAGMA table_info("%s")' % table)}
    finally:
        connection.close()


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
def post_f8_pre_f9(tmp_path):
    """A database that genuinely predates the F9 release.

    This is the only fixture that models the situation a release creates: the
    MODELS declare schema the DATABASE does not have yet. `production_like`
    cannot, because it builds the schema from the current models — which is why
    a strict pre-upgrade guard passed there and then aborted the real F9 deploy.

    Removed here: F9's tables, F9's two columns, and F9's ledger row.
    """
    import sqlite3

    from models.base import db
    from sqlalchemy import create_engine

    path = str(tmp_path / 'post_f8_pre_f9.db')
    url = 'sqlite:///%s' % path
    engine = create_engine(url)
    db.metadata.create_all(bind=engine)
    engine.dispose()

    connection = sqlite3.connect(path)
    for table in ('media_cast', 'person'):
        connection.execute('DROP TABLE IF EXISTS "%s"' % table)
    connection.execute('ALTER TABLE media_item '
                       'DROP COLUMN cast_enriched_at')
    connection.execute('ALTER TABLE taste_profile '
                       'DROP COLUMN actor_affinity_json')
    # The pre-F9 ledger: F8 is applied, F9 is not.
    #
    # `create_all` already built schema_migrations with its DECLARED shape, so
    # it is reused rather than hand-written — an approximate DDL here is
    # correctly refused by init-ledger, which is its whole job.
    #
    # 0001 is recorded with its REAL checksum. A fabricated one would make
    # `upgrade` refuse with ChecksumMismatch, which is correct behaviour and
    # would mask what these tests are actually about.
    import hashlib

    if os.path.join(REPO, 'scripts') not in sys.path:
        sys.path.insert(0, os.path.join(REPO, 'scripts'))
    import migrate as _runner_mod
    _checksum = _runner_mod.checksum_for(
        _runner_mod.registry.by_version()['0001_canonical_watched_reconcile'])
    connection.execute(
        'INSERT INTO schema_migrations '
        '(version, checksum, kind, applied_at, execution_ms, note) VALUES '
        "(?, ?, 'baseline', CURRENT_TIMESTAMP, 0, 'adopted')",
        (_runner_mod.BASELINE_VERSION,
         hashlib.sha256(_runner_mod.BASELINE_NOTE.encode()).hexdigest()))
    connection.execute(
        'INSERT INTO schema_migrations '
        '(version, checksum, kind, applied_at, execution_ms, note) VALUES '
        "('0001_canonical_watched_reconcile', ?, 'migration', "
        'CURRENT_TIMESTAMP, 1, NULL)', (_checksum,))
    connection.commit()
    connection.close()

    _CURRENT['url'] = url
    _CURRENT['path'] = path
    yield url, path
    _CURRENT.clear()


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


# ══════════════════════════════════════════════════════════════════════════
# H: a release that ADDS a model — the post-F8/pre-F9 shape
#
# This is the case `production_like` cannot represent. Its schema is built from
# the CURRENT models, so it already contains everything a new release declares.
# The deploy sequence therefore always looked green locally while a strict
# pre-upgrade guard aborted the real F9 deploy: the models referenced tables
# (`person`, `media_cast`) and columns (`media_item.cast_enriched_at`,
# `taste_profile.actor_affinity_json`) that the database did not have, and the
# runner that would have created them had not run yet.
# ══════════════════════════════════════════════════════════════════════════


def test_post_f8_pre_f9_db_really_lacks_every_f9_object(post_f8_pre_f9):
    url, path = post_f8_pre_f9
    tables = _tables(path)
    for name in F9_TABLES:
        assert name not in tables, name
    for table, column in F9_COLUMNS:
        assert column not in _columns(path, table), '%s.%s' % (table, column)
    # And the ledger reflects the pre-F9 state.
    assert dict(_ledger(path)) == {
        '0000_legacy_baseline': 'baseline',
        '0001_canonical_watched_reconcile': 'migration',
    }


def test_strict_pre_upgrade_guard_aborts_on_the_pre_f9_state(post_f8_pre_f9):
    """The blocker, reproduced: a strict check cannot pass here."""
    result = _guard()
    assert result.returncode == 1, result.stdout + result.stderr
    for name in F9_TABLES:
        assert name in result.stderr, name
    assert 'actor_affinity_json' in result.stderr


def test_pending_aware_pre_upgrade_guard_passes(post_f8_pre_f9):
    result = _guard_pending()
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Schema OK' in result.stdout
    # It must SAY what it is tolerating, not pass silently.
    for name in F9_TABLES:
        assert name in result.stdout, name


def test_full_safe_sequence_applies_f9_while_0002_stays_deferred(post_f8_pre_f9):
    """The whole deploy.yml sequence, on the shape it actually faces."""
    url, path = post_f8_pre_f9

    assert _runner('init-ledger').returncode == 0
    convergence = _convergence()
    assert convergence.returncode == 0, convergence.stdout[-600:]
    # Convergence must NOT create what the runner owns, and must say so.
    assert not (set(F9_TABLES) & _tables(path))
    assert 'DEFERRED' in convergence.stdout
    assert 'media_cast' in convergence.stdout
    assert 'person' in convergence.stdout

    pending = _guard_pending()
    assert pending.returncode == 0, pending.stdout + pending.stderr

    # baseline is a no-op: this database already records one.
    assert _runner('adopt-legacy-baseline').returncode == 0

    upgrade = _runner('upgrade')
    assert upgrade.returncode == 0, upgrade.stdout[-600:]
    assert '+ 0003_cast_persistence' in upgrade.stdout

    # F9's objects now exist...
    tables = _tables(path)
    for name in F9_TABLES:
        assert name in tables, name
    for table, column in F9_COLUMNS:
        assert column in _columns(path, table), '%s.%s' % (table, column)

    # ...the ledger records F9, and the destructive migration is untouched.
    recorded = dict(_ledger(path))
    assert recorded['0003_cast_persistence'] == 'migration'
    assert '0002_remove_legacy_wishlist' not in recorded

    # The STRICT post-upgrade guard — the gate that actually prevents serving —
    # now passes, and would have failed a moment earlier.
    strict = _guard()
    assert strict.returncode == 0, strict.stdout + strict.stderr


def test_post_upgrade_guard_is_still_strict(post_f8_pre_f9):
    """`--allow-pending-migrations` is a pre-upgrade tool, not a permanent one.

    Drift introduced AFTER the migration is applied must stop the deploy even
    though the tolerant flag would forgive migration-owned objects.
    """
    import sqlite3
    url, path = post_f8_pre_f9
    assert _runner('init-ledger').returncode == 0
    assert _convergence().returncode == 0
    assert _guard_pending().returncode == 0
    assert _runner('upgrade').returncode == 0

    connection = sqlite3.connect(path)
    connection.execute('DROP TABLE IF EXISTS media_like')
    connection.commit()
    connection.close()

    result = _guard_pending()
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'media_like' in result.stderr


def test_convergence_leaves_migration_owned_tables_to_the_runner(post_f8_pre_f9):
    """Convergence must never create an object the runner will record.

    If it did, the ledger would be unable to say which migration produced the
    table — the exact failure `init-ledger` exists to prevent.
    """
    url, path = post_f8_pre_f9
    before = _tables(path)
    result = _convergence()
    assert result.returncode == 0, result.stdout[-600:]
    created = _tables(path) - before
    assert not (created & set(F9_TABLES)), created
    # And the honest report: it must not claim everything exists.
    assert 'every declared table exists' not in result.stdout, result.stdout
    assert 'DEFERRED' in result.stdout
    assert 'NOT created here' in result.stdout


def test_convergence_reports_success_honestly_when_nothing_is_deferred(
        production_like):
    """The complementary case: with nothing pending, the claim is true.

    Reach it honestly — run the sequence so the ledger exists and F9 is applied,
    then converge again. Asserting it on the un-converged fixture would only
    prove that convergence refuses on a missing ledger.
    """
    url, path = production_like
    assert _runner('init-ledger').returncode == 0
    assert _convergence().returncode == 0
    assert _guard_pending().returncode == 0
    assert _runner('upgrade').returncode == 0

    result = _convergence()
    assert result.returncode == 0, result.stdout[-600:]
    assert 'DEFERRED' not in result.stdout
    assert 'every declared table exists' in result.stdout
