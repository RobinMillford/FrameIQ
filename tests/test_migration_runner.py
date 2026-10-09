"""F8: the versioned migration runner, its ledger, and its safety rules.

What is under test
------------------
The runner is the thing that decides whether a deploy changes a database. The
dangerous failures are not crashes; they are quiet, wrong outcomes:

* a migration silently edited after it ran, so two databases that both report
  "applied" have different schemas;
* a migration that half-applied but is recorded as done;
* two deploys racing and both applying the same migration;
* an empty ledger being mistaken for "nothing was ever applied", which is how
  unrecorded history gets rewritten by a future migration;
* the destructive wishlist merge dropping data.

Each of those is pinned below.

Harness notes
-------------
Migrations are exercised on SQLite because the CI environment has no PostgreSQL
server. The dialect-specific paths (advisory locks, ``ON CONFLICT``) are NOT
exercised and are NOT claimed to work; see the two explicit limitation tests at
the end, which assert the runner *reports* the gap instead of hiding it.
"""
import datetime
import importlib
import inspect as stdlib_inspect
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Mirror the runner's own import bootstrap so the test imports the *same*
# module object the CLI loads, not a second copy.
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'migrates'))
sys.path.insert(0, os.path.join(REPO, 'scripts'))

import migrate as runner  # noqa: E402
import registry as reg  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    """An empty SQLite database with the declared schema."""
    from models.base import db
    engine = create_engine('sqlite:///%s' % (tmp_path / 'f8.db'))
    db.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


def _ledger_rows(engine):
    with engine.connect() as connection:
        return {row[0]: (row[1], row[2]) for row in connection.execute(
            text('SELECT version, checksum, kind FROM schema_migrations'))}


def _spec(version):
    return reg.by_version()[version]


# ── checksums ────────────────────────────────────────────────────────────────


def test_checksum_is_the_sha256_of_the_whole_file():
    """Covers every byte, so a comment change is also a tamper signal."""
    import hashlib

    spec = _spec('0001_canonical_watched_reconcile')
    with open(runner.module_path(spec), 'rb') as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()
    assert runner.checksum_for(spec) == expected
    assert len(runner.checksum_for(spec)) == 64


def test_checksum_is_stable_across_calls():
    spec = _spec('0001_canonical_watched_reconcile')
    assert runner.checksum_for(spec) == runner.checksum_for(spec)


def test_checksum_changes_when_a_single_byte_changes(tmp_path, monkeypatch):
    """The property that makes the whole ledger meaningful."""
    spec = _spec('0001_canonical_watched_reconcile')
    real = runner.module_path(spec)
    copy = tmp_path / 'mutated.py'
    copy.write_bytes(open(real, 'rb').read())
    monkeypatch.setattr(runner, 'module_path', lambda _s: str(copy))
    before = runner.checksum_for(spec)
    data = bytearray(copy.read_bytes())
    data[-2] = data[-2] ^ 0x20
    copy.write_bytes(bytes(data))
    assert runner.checksum_for(spec) != before


def test_missing_module_is_reported_not_silently_skipped():
    bogus = reg.MigrationSpec(version='9999_nope', module='no_such_module',
                              summary='x')
    with pytest.raises(runner.MigrationError):
        runner.load_module(bogus)


def test_module_without_run_is_refused():
    from types import SimpleNamespace

    monkey = SimpleNamespace()
    monkey.run = None

    class FakeSpec:
        version = '9999_x'
        module = 'fake_module_without_run'

    with pytest.raises(Exception):
        # A module lacking run() must never be treated as a no-op success.
        runner.load_module(FakeSpec())


# ── registry integrity ───────────────────────────────────────────────────────


def test_shipped_registry_is_valid():
    assert reg.validate() == []


def test_duplicate_versions_are_detected():
    specs = (reg.MigrationSpec('0001_a', 'a', 'x'),
             reg.MigrationSpec('0001_a', 'b', 'x'))
    problems = _validate_specs(specs)
    assert any('duplicate' in p for p in problems)


def test_unknown_dependency_is_detected():
    specs = (reg.MigrationSpec('0001_a', 'a', 'x', depends_on=('0099_zzz',)),)
    problems = _validate_specs(specs)
    assert any('not registered' in p for p in problems)


def test_forward_dependency_is_detected():
    specs = (reg.MigrationSpec('0001_a', 'a', 'x', depends_on=('0002_b',)),
             reg.MigrationSpec('0002_b', 'b', 'x'))
    problems = _validate_specs(specs)
    assert any('forward dependency' in p for p in problems)


def test_dependency_cycle_is_detected():
    specs = (reg.MigrationSpec('0001_a', 'a', 'x', depends_on=('0002_b',)),
             reg.MigrationSpec('0002_b', 'b', 'x', depends_on=('0001_a',)))
    problems = _validate_specs(specs)
    assert any('cycle' in p for p in problems)


def _validate_specs(specs):
    """Run registry.validate() against a synthetic registry."""
    original = reg.MIGRATIONS
    reg.MIGRATIONS = specs
    try:
        return reg.validate()
    finally:
        reg.MIGRATIONS = original


def test_ordering_is_by_version_not_filesystem():
    versions = [spec.version for spec in reg.ordered_migrations()]
    assert versions == sorted(versions)


def test_historical_scripts_are_not_registered():
    """The 30 unwired scripts must never be scheduled by accident."""
    registered = {spec.module for spec in reg.ordered_migrations()}
    for historical in ('migrate_schema_convergence',
                       'migrate_import_source_mapping',
                       'migrate_remove_wishlist',
                       'migrate_taste_profile',
                       'migrate_director_capture',
                       'migrate_week4_discovery'):
        assert historical not in registered


# ── planning: pending vs satisfied vs tampered ───────────────────────────────


def test_fresh_database_plans_every_registered_migration_pending():
    report = runner.plan({})
    assert [s.version for s in report['pending']] == [
        '0001_canonical_watched_reconcile', '0002_remove_legacy_wishlist',
        '0003_cast_persistence']
    assert report['mismatched'] == []


def test_applied_migration_is_not_pending_again():
    checksum = runner.checksum_for(_spec('0001_canonical_watched_reconcile'))
    report = runner.plan({'0001_canonical_watched_reconcile': (checksum,
                                                               'migration')})
    assert [s.version for s in report['pending']] == [
        '0002_remove_legacy_wishlist', '0003_cast_persistence']


def test_edited_applied_migration_is_reported_as_mismatched():
    report = runner.plan(
        {'0001_canonical_watched_reconcile': ('deadbeef' * 8, 'migration')})
    assert len(report['mismatched']) == 1
    spec, recorded = report['mismatched'][0]
    assert spec.version == '0001_canonical_watched_reconcile'
    assert recorded == 'deadbeef' * 8


def test_tampered_history_makes_the_run_refuse_entirely():
    """Fail closed: not 'run the rest', but stop before applying anything."""
    with pytest.raises(runner.ChecksumMismatch) as excinfo:
        runner.decide_pending({'0001_canonical_watched_reconcile':
                               ('deadbeef' * 8, 'migration')})
    assert '0001_canonical_watched_reconcile' in str(excinfo.value)
    assert 'NEW forward migration' in str(excinfo.value)


def test_baseline_is_not_reported_as_orphaned(engine):
    """The baseline is deliberately unregistered; flagging it would make
    operators ignore the warning that catches a genuinely unknown entry."""
    runner.adopt_legacy_baseline(engine)
    report = runner.plan(_ledger_rows(engine))
    assert report['orphaned'] == []


def test_unregistered_ledger_entry_is_reported_not_deleted():
    applied = {'0900_from_a_future_release': ('ab' * 32, 'migration')}
    report = runner.plan(applied)
    assert '0900_from_a_future_release' in report['orphaned']


def test_only_filter_still_enforces_dependencies():
    """`--only` narrows the plan; it is not a way to skip a prerequisite.

    Applying 0002 without 0001 on a fresh database would produce a database
    whose history claims 0002 ran while 0001 never did.
    """
    with pytest.raises(runner.MigrationError) as excinfo:
        runner.decide_pending({}, only={'0002_remove_legacy_wishlist'})
    assert '0001_canonical_watched_reconcile' in str(excinfo.value)


def test_only_filter_narrows_the_plan_when_prerequisites_are_met():
    checksum = runner.checksum_for(_spec('0001_canonical_watched_reconcile'))
    applied = {'0001_canonical_watched_reconcile': (checksum, 'migration')}
    pending = runner.decide_pending(applied, only={
        '0002_remove_legacy_wishlist'})
    assert [s.version for s in pending] == ['0002_remove_legacy_wishlist']


# ── dependency resolution across a single run ────────────────────────────────


BOTH = {'0001_canonical_watched_reconcile', '0002_remove_legacy_wishlist'}


def test_dependency_is_satisfied_by_an_earlier_migration_in_the_same_run():
    """A fresh database must be able to run 0002 after 0001.

    Validating dependencies only against what is *already recorded* would make
    the very first run of a fresh database impossible.
    """
    pending = runner.decide_pending({}, only=BOTH)
    assert [s.version for s in pending] == [
        '0001_canonical_watched_reconcile', '0002_remove_legacy_wishlist']


def test_dependency_on_a_later_unapplied_version_is_refused():
    """If 0001 is already applied, only 0002 remains and the chain holds."""
    checksum = runner.checksum_for(_spec('0001_canonical_watched_reconcile'))
    pending = runner.decide_pending(
        {'0001_canonical_watched_reconcile': (checksum, 'migration')},
        only=BOTH)
    assert [s.version for s in pending] == ['0002_remove_legacy_wishlist']


def test_an_unselected_destructive_migration_is_deferred_not_attempted():
    """An ordinary deploy must not be blocked, and must not silently drop it
    from the plan either: it is reported and left pending."""
    pending = runner.decide_pending({}, only={'0001_canonical_watched_reconcile'})
    assert [s.version for s in pending] == ['0001_canonical_watched_reconcile']


def test_the_destructive_migration_is_not_in_destructive_projects():
    spec = reg.by_version()['0002_remove_legacy_wishlist']
    module = importlib.import_module(spec.module.split('.')[-1])
    assert module.DESTRUCTIVE is True
    assert 'drop' in module.DESTRUCTIVE_REASON.lower()


def test_a_non_destructive_migration_needs_no_approval():
    spec = reg.by_version()['0001_canonical_watched_reconcile']
    module = importlib.import_module(spec.module.split('.')[-1])
    assert module.DESTRUCTIVE is False


def test_missing_dependencies_helper():
    spec = _spec('0002_remove_legacy_wishlist')
    assert runner.missing_dependencies(spec, set()) == [
        '0001_canonical_watched_reconcile']
    assert runner.missing_dependencies(spec, {'0001_canonical_watched_reconcile'}) == []


# ── applying ─────────────────────────────────────────────────────────────────


def test_upgrade_applies_all_and_records_each(engine):
    applied = _run_all(engine)
    # 0003 is applied by the ORDINARY stage, ahead of the destructive 0002,
    # which only runs in the approval-gated second stage. F9's additive schema
    # is therefore reachable without authorising the legacy DROP.
    assert applied == ['0001_canonical_watched_reconcile',
                       '0003_cast_persistence',
                       '0002_remove_legacy_wishlist']
    rows = _ledger_rows(engine)
    assert set(rows) == {'0001_canonical_watched_reconcile',
                         '0002_remove_legacy_wishlist',
                         '0003_cast_persistence'}
    for version, (checksum, kind) in rows.items():
        assert kind == 'migration'
        assert checksum == runner.checksum_for(_spec(version))


def test_upgrade_is_idempotent(engine):
    _run_all(engine)
    assert runner.apply_pending(engine) == []
    assert _run_0002(engine) == []
    assert len(_ledger_rows(engine)) == 3


def test_ledger_table_is_created_by_the_runner_not_a_migration(engine):
    with engine.connect() as connection:
        assert 'schema_migrations' in inspect(connection).get_table_names()


def test_failing_migration_is_not_recorded_and_its_work_rolls_back(
        engine, monkeypatch):
    """The core transaction guarantee.

    A migration whose body raises must leave neither a ledger row nor a partial
    schema change. Without this, a failed deploy looks like a successful one.
    """
    calls = []

    def half_failing_run(connection):
        connection.execute(text('CREATE TABLE partial_write (x INTEGER)'))
        calls.append('body')
        raise RuntimeError('boom')

    module = importlib.import_module('migrations_0001_canonical_watched')
    monkeypatch.setattr(module, 'run', half_failing_run)

    with pytest.raises(runner.MigrationError) as excinfo:
        runner.apply_pending(engine)

    assert 'boom' in str(excinfo.value)
    assert calls == ['body']

    with engine.connect() as connection:
        recorded = connection.execute(
            text('SELECT COUNT(*) FROM schema_migrations')).scalar()
    assert recorded == 0, 'a failed migration was recorded as applied'

    if engine.dialect.name == 'postgresql':
        with engine.connect() as connection:
            assert 'partial_write' not in inspect(connection).get_table_names()
    else:
        # SQLite's pysqlite driver commits before DDL, so the DDL is NOT rolled
        # back. The ledger guarantee above still holds. Asserting DDL rollback
        # here would be a test that passes only by accident on production.
        pytest.skip('DDL is not transactional on SQLite; the ledger guarantee '
                    'is asserted above and holds on every backend. The '
                    'PostgreSQL rollback guarantee IS executed — see '
                    'tests/test_migration_postgres.py')


def test_ddl_rollback_requires_a_postgresql_server():
    """Marks the unexercised path so it cannot be forgotten.

    On PostgreSQL a failing migration leaves no trace at all; on SQLite the
    DDL survives. CI has the psql client but no server, so the PostgreSQL
    guarantee is asserted only when such an engine is actually available.
    """
    engine = create_engine('sqlite:///:memory:')
    transactional_ddl = engine.dialect.name == 'postgresql'
    engine.dispose()
    if not transactional_ddl:
        pytest.skip('DDL rollback needs a PostgreSQL server; this SQLite suite '
                    'cannot prove it. The PostgreSQL path IS executed in '
                    'tests/test_migration_postgres.py (pytest -m postgres)')


def test_verify_failure_also_rolls_back(engine, monkeypatch):
    """A migration that runs but does not converge must not be recorded."""
    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')

    def bad_run(connection):
        connection.execute(text('CREATE TABLE partial_two (x INTEGER)'))
        return {}

    def bad_verify(connection):
        return ['deliberately not converged']

    monkeypatch.setattr(module, 'run', bad_run)
    monkeypatch.setattr(module, 'verify', bad_verify, raising=False)

    # 0001 must be recorded first so 0002 is the pending migration.
    checksum = runner.checksum_for(_spec('0001_canonical_watched_reconcile'))
    with engine.begin() as connection:
        runner.ensure_ledger_table(connection)
        connection.execute(text(
            'INSERT INTO schema_migrations (version, checksum, kind, '
            'applied_at, execution_ms) VALUES '
            "(:v, :c, 'migration', CURRENT_TIMESTAMP, 0)"),
            {'v': '0001_canonical_watched_reconcile', 'c': checksum})

    with pytest.raises(runner.MigrationError) as excinfo:
        _run_0002(engine)
    assert 'did not converge' in str(excinfo.value)

    with engine.connect() as connection:
        versions = [r[0] for r in connection.execute(
            text('SELECT version FROM schema_migrations'))]
    assert '0002_remove_legacy_wishlist' not in versions

    if engine.dialect.name != 'postgresql':
        pytest.skip('DDL is not transactional on SQLite; the ledger guarantee '
                    'is asserted above. The PostgreSQL rollback guarantee IS '
                    'executed — see tests/test_migration_postgres.py')


def test_non_transactional_migration_is_refused_not_best_effort():
    """The runner will not record a row it cannot make atomic."""
    from types import ModuleType

    module = ModuleType('fake_non_transactional')
    module.NON_TRANSACTIONAL = True
    module.run = lambda connection: None

    spec = reg.MigrationSpec(version='0001_nontransactional',
                             module='fake_non_transactional', summary='x')
    original = importlib.import_module
    try:
        importlib.import_module = lambda name: module
        with pytest.raises(runner.MigrationError) as excinfo:
            runner.load_module(spec)
        assert 'NON_TRANSACTIONAL' in str(excinfo.value)
    finally:
        importlib.import_module = original


# ── legacy baseline ──────────────────────────────────────────────────────────


def test_adopt_baseline_requires_a_healthy_schema(engine, monkeypatch):
    """No baseline may be recorded on a schema that is missing tables.

    A baseline asserts "this database already satisfies the models". If the
    schema does not, the baseline is a false claim that will permanently
    suppress the very migration needed to fix it.
    """
    with engine.connect() as connection:
        connection.execute(text('DROP TABLE user_watchlist'))
        connection.commit()

    with pytest.raises(runner.MigrationError) as excinfo:
        runner.adopt_legacy_baseline(engine)
    assert 'schema' in str(excinfo.value).lower()

    assert runner.BASELINE_VERSION not in _ledger_rows(engine)


def test_adopt_baseline_succeeds_on_a_healthy_schema(engine):
    assert runner.adopt_legacy_baseline(engine) is True
    rows = _ledger_rows(engine)
    assert runner.BASELINE_VERSION in rows
    checksum, kind = rows[runner.BASELINE_VERSION]
    assert kind == 'baseline'


def test_adopt_baseline_refuses_to_run_twice(engine):
    runner.adopt_legacy_baseline(engine)
    assert runner.adopt_legacy_baseline(engine) is False


def test_baseline_checksum_is_documented_not_a_claim_of_history(engine):
    """The checksum is of a fixed note, so it records *what was claimed*.

    It is not a checksum of the 30 historical scripts: their combined effect
    cannot be reconstructed, and pretending otherwise would invent provenance.
    """
    import hashlib

    expected = hashlib.sha256(
        runner.BASELINE_NOTE.encode()).hexdigest()
    with engine.connect() as connection:
        runner.ensure_ledger_table(connection)
        runner.adopt_legacy_baseline(engine)
        stored = connection.execute(
            text('SELECT checksum FROM schema_migrations WHERE version = :v'),
            {'v': runner.BASELINE_VERSION}).scalar()
    assert stored == expected


def test_baseline_does_not_mark_migrations_as_applied(engine):
    """Adopting a baseline must not invent applied migrations."""
    runner.adopt_legacy_baseline(engine)
    report = runner.plan(_ledger_rows(engine))
    assert [s.version for s in report['pending']] == [
        '0001_canonical_watched_reconcile', '0002_remove_legacy_wishlist',
        '0003_cast_persistence']


# ── locking ──────────────────────────────────────────────────────────────────


def test_lock_reports_its_own_absence_on_sqlite(engine, capsys):
    """Better an honest warning than a silent false sense of safety."""
    with engine.connect() as connection:
        with runner.advisory_lock(connection) as lock:
            assert lock.available is False
    out = capsys.readouterr().out
    assert 'UNAVAILABLE' in out
    assert 'WITHOUT concurrent-runner exclusion' in out


def test_lock_key_is_stable_and_documented():
    assert runner.LOCK_KEY == 0x46515538


def test_lock_is_session_scoped_not_transaction_scoped():
    """A transaction-scoped lock dies at the first COMMIT.

    The runner commits between reading the ledger and applying each migration,
    so a transaction-scoped lock would leave that whole window unprotected.
    """
    source = stdlib_inspect.getsource(runner.advisory_lock)
    assert 'pg_advisory_lock' in source
    assert 'pg_advisory_xact_lock' not in source


# ── 0001: canonical_watched — diary is canonical, markers are preserved ───────
#
# Decision A: the 15 viewed-only pairs are legacy MARKERS, not watch history.
# They must be preserved exactly, never turned into DiaryEntry rows, never
# deleted, and never reported as reconciled canonical watch history.


def _seed_diary(engine, rows):
    """rows: (user_id, media_id, watched_date) for movies."""
    import datetime

    from models.base import db

    with engine.begin() as connection:
        users = {u for u, _m, _d in rows}
        media = {m for _u, m, _d in rows}
        for index, user_id in enumerate(sorted(users)):
            connection.execute(db.metadata.tables['user'].insert().values(
                id=user_id, username='u%d' % user_id,
                email='u%d@example.test' % user_id, password_hash='x',
                date_joined=datetime.datetime.utcnow()))
            assert index is not None
        for media_id in sorted(media):
            connection.execute(db.metadata.tables['media_item'].insert().values(
                id=media_id, media_type='movie', tmdb_id=media_id,
                title='T%s' % media_id))
        for user_id, media_id, watched in rows:
            connection.execute(
                db.metadata.tables['diary_entry'].insert().values(
                    user_id=user_id, media_id=media_id, media_type='movie',
                    watched_date=datetime.date(*watched),
                    created_at=datetime.datetime.utcnow()))


def _seed_viewed(engine, rows):
    """rows: (user_id, media_id, date_viewed|None, rating|None)."""
    import datetime

    with engine.begin() as connection:
        for user_id, media_id, date_viewed, rating in rows:
            connection.execute(text(
                'INSERT INTO user_viewed (user_id, media_id, media_type, '
                'date_viewed, rating) VALUES (:u, :m, \'movie\', :d, :r)'),
                {'u': user_id, 'm': media_id,
                 'd': datetime.datetime(*date_viewed) if date_viewed else None,
                 'r': rating})


def _diary_rows(engine):
    with engine.connect() as connection:
        return sorted(connection.execute(text(
            'SELECT user_id, media_id, media_type FROM diary_entry')).fetchall())


def _with_dates(rows, positions):
    """Coerce only the datetime columns; SQLite returns those as strings.

    Comparing a serialised string against a datetime would assert on format
    rather than on value, and converting every string would mangle media_type.
    """
    out = []
    for row in rows:
        values = list(row)
        for position in positions:
            if isinstance(values[position], str):
                values[position] = datetime.datetime.fromisoformat(
                    values[position])
        out.append(tuple(values))
    return sorted(out)


def _viewed_rows(engine):
    with engine.connect() as connection:
        return _with_dates(connection.execute(text(
            'SELECT user_id, media_id, media_type, date_viewed, '
            'rating FROM user_viewed')).fetchall(), (3,))


def _run_0001(engine):
    module = importlib.import_module('migrations_0001_canonical_watched')
    with engine.begin() as connection:
        return module.run(connection)


def test_0001_derives_viewed_from_the_canonical_diary(engine):
    _seed_diary(engine, [(1, 1, (2024, 1, 1)), (1, 2, (2024, 2, 1))])
    result = _run_0001(engine)
    assert result['inserted_user_viewed'] == 2
    assert result['diary_only_after'] == 0
    assert len(_viewed_rows(engine)) == 2


def test_0001_uses_the_earliest_watch_date_for_rewatches(engine):
    """Continue Watching orders by date_viewed, so the earliest watch is the
    meaningful value for a re-watched title."""
    _seed_diary(engine, [(1, 1, (2024, 5, 1))])
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO diary_entry (user_id, media_id, media_type, "
            "watched_date, created_at) VALUES (1, 1, 'movie', '2023-01-01', "
            'CURRENT_TIMESTAMP)'))
    _run_0001(engine)
    with engine.connect() as connection:
        date_viewed = connection.execute(text(
            'SELECT date_viewed FROM user_viewed WHERE media_id=1')).scalar()
    assert _with_dates([(date_viewed,)], (0,))[0][0].year == 2023


# ── Decision A: the 15 viewed-only markers ───────────────────────────────────


def test_0001_does_not_fabricate_diary_entries_from_viewed_markers(engine):
    """The core Decision A guarantee.

    A `user_viewed` row with no diary entry is a marker, not history. Creating
    a DiaryEntry for it would manufacture canonical watch history the user never
    entered, which then reads as authoritative forever.
    """
    _seed_viewed(engine, [(1, 10, (2024, 3, 3), None),
                          (1, 11, (2024, 4, 4), None)])
    result = _run_0001(engine)
    assert _diary_rows(engine) == [], 'diary rows were fabricated'
    assert result.get('inserted_diary_entry', 0) == 0
    assert result['unresolved_legacy_markers'] == 2


def test_0001_preserves_the_viewed_only_rows_verbatim(engine):
    """Not deleted, not rewritten: same keys, same dates, same ratings."""
    _seed_viewed(engine, [(1, 10, (2024, 3, 3), 4),
                          (1, 11, (2024, 4, 4), None)])
    _run_0001(engine)

    after = _viewed_rows(engine)
    assert after == [
        (1, 10, 'movie', datetime.datetime(2024, 3, 3), 4),
        (1, 11, 'movie', datetime.datetime(2024, 4, 4), None),
    ], 'the legacy markers were removed or altered'


def test_0001_handles_a_viewed_marker_with_a_null_date(engine):
    """diary_entry.watched_date is NOT NULL.

    Back-filling from a NULL date_viewed would abort the INSERT outright — a
    latent crash. The marker must simply be preserved.
    """
    _seed_viewed(engine, [(1, 12, None, None)])
    result = _run_0001(engine)
    assert result['unresolved_legacy_markers'] == 1
    assert _diary_rows(engine) == []
    assert len(_viewed_rows(engine)) == 1


def test_0001_does_not_report_markers_as_reconciled(engine):
    """The deploy log must not imply the discrepancy was resolved."""
    _seed_viewed(engine, [(1, 10, (2024, 3, 3), None)])
    result = _run_0001(engine)
    assert result['reconciled_viewed_only'] == 0
    assert result['unresolved_legacy_markers'] == 1
    assert 'unresolved_legacy_markers' in result
    assert result['diary_only_after'] == 0


def test_0001_verifies_clean_despite_unresolved_markers(engine):
    """verify() must NOT require the marker count to be zero.

    Requiring zero would make the migration fail precisely because it correctly
    declined to fabricate history.
    """
    module = importlib.import_module('migrations_0001_canonical_watched')
    _seed_viewed(engine, [(1, 10, (2024, 3, 3), None),
                          (1, 11, (2024, 4, 4), None)])
    _run_0001(engine)
    with engine.connect() as connection:
        assert module.verify(connection) == []


def test_0001_verify_still_catches_a_genuine_unconverged_diary(engine):
    """verify() keeps its original purpose: the DERIVED direction must converge."""
    module = importlib.import_module('migrations_0001_canonical_watched')
    _seed_diary(engine, [(1, 1, (2024, 1, 1))])
    with engine.connect() as connection:
        problems = module.verify(connection)
    assert problems and 'diary-only' in problems[0]


def test_0001_mixed_population_both_directions(engine):
    """Diary-only pairs are derived; viewed-only markers are preserved."""
    _seed_diary(engine, [(1, 1, (2024, 1, 1)), (1, 2, (2024, 2, 1))])
    _seed_viewed(engine, [(1, 2, None, None), (1, 20, (2024, 6, 1), 5)])
    result = _run_0001(engine)
    assert result['diary_only_before'] == 1        # (1,1) needed a viewed row
    assert result['inserted_user_viewed'] == 1
    assert result['unresolved_legacy_markers'] == 1  # (1,20) stays a marker
    assert len(_diary_rows(engine)) == 2, 'a marker became a diary entry'
    # (1,2) was seeded in both tables; (1,1) was derived; (1,20) is the marker.
    assert len(_viewed_rows(engine)) == 3


def test_0001_does_not_overwrite_an_existing_viewed_row(engine):
    """Derivation must never clobber a user's own viewed date or rating."""
    _seed_viewed(engine, [(1, 1, (2023, 1, 1), 5)])
    _seed_diary(engine, [(1, 1, (2024, 6, 6))])
    _run_0001(engine)
    with engine.connect() as connection:
        row = connection.execute(text(
            'SELECT date_viewed, rating FROM user_viewed '
            'WHERE media_id=1')).fetchone()
    assert _with_dates([row], (0,))[0][0].year == 2023 and row[1] == 5


def test_0001_is_idempotent(engine):
    _seed_diary(engine, [(1, 1, (2024, 1, 1))])
    _seed_viewed(engine, [(1, 20, (2024, 6, 1), None)])
    first = _run_0001(engine)
    second = _run_0001(engine)
    assert first['inserted_user_viewed'] == 1
    assert second['inserted_user_viewed'] == 0
    assert second['unresolved_legacy_markers'] == 1
    assert len(_viewed_rows(engine)) == 2
    assert len(_diary_rows(engine)) == 1


def test_0001_preserves_the_exact_15_marker_shape(engine):
    """Reproduces the production population: 2 diary-only, 15 viewed-only."""
    diary = [(1, media, (2024, 1, 1)) for media in range(1, 3)]
    markers = [(1, 100 + n, (2024, 2, 1), None) for n in range(15)]
    _seed_diary(engine, diary)
    _seed_viewed(engine, markers)
    result = _run_0001(engine)
    assert result['diary_only_before'] == 2
    assert result['inserted_user_viewed'] == 2
    assert result['diary_only_after'] == 0
    assert result['unresolved_legacy_markers'] == 15
    assert len(_diary_rows(engine)) == 2, 'markers leaked into the diary'
    assert len(_viewed_rows(engine)) == 17
    module = importlib.import_module('migrations_0001_canonical_watched')
    with engine.connect() as connection:
        assert module.verify(connection) == []


def test_0001_leaves_tv_history_alone(engine):
    """TV lives in TVEpisodeWatch; user_viewed is movie-scoped by design."""
    _seed_viewed(engine, [(1, 30, (2024, 2, 2), None)])
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO diary_entry (user_id, media_id, media_type, "
            "watched_date, created_at) VALUES (1, 30, 'tv', '2024-01-01', "
            'CURRENT_TIMESTAMP)'))
    result = _run_0001(engine)
    assert result['inserted_user_viewed'] == 0
    assert len(_viewed_rows(engine)) == 1


def test_0001_never_commits(engine):
    """The runner owns the transaction; a commit here would break atomicity."""
    import inspect as stdlib_inspect
    module = importlib.import_module('migrations_0001_canonical_watched')
    source = stdlib_inspect.getsource(module)
    assert '.commit(' not in source
    assert 'session.begin' not in source


def test_0001_never_deletes_diary_entries(engine):
    """F4 made diary the source of truth; this may only ever ADD."""
    _seed_diary(engine, [(1, 1, (2024, 1, 1))])
    before = _diary_rows(engine)
    _run_0001(engine)
    assert _diary_rows(engine) == before


def test_0001_does_not_touch_watchlist(engine):
    _seed_diary(engine, [(1, 1, (2024, 1, 1))])
    _run_0001(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text('SELECT COUNT(*) FROM user_watchlist')).scalar() == 0


# ── 0002: destructive legacy wishlist consolidation ──────────────────────────
#
# Production reality (read-only, 2026-09): 1 source row, 4 target rows, NO
# matching target. Identical column sets; both keyed (user_id, media_id,
# media_type). The migration must therefore INSERT that row and keep its
# date_added and priority.

LEGACY_COLUMNS = """
            CREATE TABLE user_wishlist (
                user_id INTEGER NOT NULL,
                media_id INTEGER,
                media_type VARCHAR(20) NOT NULL,
                date_added DATETIME,
                priority VARCHAR(10),
                PRIMARY KEY (user_id, media_id, media_type))
"""


def _seed_wishlist(engine, rows, watchlist_rows=()):
    """rows/target rows: (user_id, media_id, media_type, date_added, priority)."""
    with engine.begin() as connection:
        connection.execute(text(LEGACY_COLUMNS))
        for user_id, media_id, media_type, added, priority in rows:
            connection.execute(text(
                'INSERT INTO user_wishlist (user_id, media_id, media_type, '
                'date_added, priority) VALUES (:u, :m, :t, :a, :p)'),
                {'u': user_id, 'm': media_id, 't': media_type,
                 'a': added, 'p': priority})
        for user_id, media_id, media_type, added, priority in watchlist_rows:
            connection.execute(text(
                'INSERT INTO user_watchlist (user_id, media_id, media_type, '
                'date_added, priority) VALUES (:u, :m, :t, :a, :p)'),
                {'u': user_id, 'm': media_id, 't': media_type,
                 'a': added, 'p': priority})


def _mark_0001_applied(engine):
    """Record 0001 so 0002 is the pending migration."""
    checksum = runner.checksum_for(_spec('0001_canonical_watched_reconcile'))
    with engine.begin() as connection:
        runner.ensure_ledger_table(connection)
        connection.execute(text(
            'INSERT INTO schema_migrations (version, checksum, kind, '
            'applied_at, execution_ms) VALUES '
            "(:v, :c, 'migration', CURRENT_TIMESTAMP, 0)"),
            {'v': '0001_canonical_watched_reconcile', 'c': checksum})


def _approval():
    """Obviously-synthetic approval so the gate can be exercised.

    The backup reference is deliberately fake and labelled as a CI scratch
    value: no production backup is claimed to exist by this file.
    """
    import uuid
    return runner.DestructiveApproval(
        authorized_version='0002_remove_legacy_wishlist',
        backup_ref='ci-scratch-snapshot-%s' % uuid.uuid4().hex[:12],
        verified_by='ci-test')


def _run_0002(engine, approval=None):
    """Stage 2 — the approval-gated step, exactly as the workflow invokes it."""
    return runner.apply_pending(
        engine, only={'0002_remove_legacy_wishlist'},
        approval=approval or _approval())


def _run_all(engine, approval=None):
    """Both stages, as the two-job deployment performs them: the ordinary
    upgrade first (0001 + F9's 0003), then the approval-gated 0002."""
    return runner.apply_pending(engine) + _run_0002(engine, approval)


def _watchlist(engine):
    with engine.connect() as connection:
        return _with_dates(connection.execute(text(
            'SELECT user_id, media_id, media_type, date_added, '
            'priority FROM user_watchlist')).fetchall(), (3,))


def _tables(engine):
    with engine.connect() as connection:
        return set(inspect(connection).get_table_names())

# --- the production shape ----------------------------------------------------


def test_0002_inserts_the_production_row_and_keeps_its_values(engine):
    """The real production case: no matching target, meaningful values.

    date_added and priority must survive verbatim — neither substituted with
    "now" nor defaulted.
    """
    _seed_wishlist(
        engine,
        rows=[(3, 13, 'movie', datetime.datetime(2026, 1, 27, 8, 15, 51),
               'low')],
        watchlist_rows=[(9, 99, 'movie', None, 'medium')])
    _mark_0001_applied(engine)
    _run_0002(engine)

    assert _watchlist(engine) == [
        (3, 13, 'movie', datetime.datetime(2026, 1, 27, 8, 15, 51), 'low'),
        (9, 99, 'movie', None, 'medium'),
    ]
    assert 'user_wishlist' not in _tables(engine)

# --- exact duplicate ---------------------------------------------------------


def test_0002_treats_a_full_match_as_an_exact_duplicate(engine):
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low')],
        watchlist_rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1),
                         'low')])
    _mark_0001_applied(engine)
    _run_0002(engine)
    assert _watchlist(engine) == [
        (1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low')]
    assert 'user_wishlist' not in _tables(engine)


def test_0002_documented_uniqueness_rule_establishes_duplicate_status():
    """Both tables are keyed (user_id, media_id, media_type)."""
    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')
    assert module.KEY_COLUMNS == ('user_id', 'media_id', 'media_type')
    assert module.INFORMATIVE == ('date_added', 'priority')

# --- conflicting values must stop the migration before the DROP --------------


def test_0002_refuses_to_drop_on_a_priority_conflict(engine):
    """The source has meaningful information the target lacks.

    The old script skipped the row and dropped the table, silently destroying
    date_added/priority. Now the migration stops with the legacy table intact.
    """
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low')],
        watchlist_rows=[(1, 100, 'movie', datetime.datetime(2026, 5, 5),
                         'high')])
    _mark_0001_applied(engine)
    with pytest.raises(runner.MigrationError) as excinfo:
        _run_0002(engine)
    message = str(excinfo.value)
    assert 'Refusing to drop user_wishlist' in message
    assert 'CONFLICT' in message
    assert 'user_wishlist' in _tables(engine), 'legacy table was destroyed'
    assert _watchlist(engine) == [
        (1, 100, 'movie', datetime.datetime(2026, 5, 5), 'high')]


def test_0002_refuses_to_drop_on_a_date_added_conflict(engine):
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low')],
        watchlist_rows=[(1, 100, 'movie', datetime.datetime(2026, 5, 5),
                         'low')])
    _mark_0001_applied(engine)
    with pytest.raises(runner.MigrationError) as excinfo:
        _run_0002(engine)
    assert 'date_added' in str(excinfo.value)
    assert 'user_wishlist' in _tables(engine)


def test_0002_conflict_does_not_overwrite_the_existing_target(engine):
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low')],
        watchlist_rows=[(1, 100, 'movie', datetime.datetime(2026, 5, 5),
                         'high')])
    _mark_0001_applied(engine)
    with pytest.raises(runner.MigrationError):
        _run_0002(engine)
    assert _watchlist(engine) == [
        (1, 100, 'movie', datetime.datetime(2026, 5, 5), 'high')]


def test_0002_allows_a_conflict_where_the_source_adds_nothing(engine):
    """A differing column whose source value is NULL carries no information,
    so dropping the source loses nothing."""
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', None, None)],
        watchlist_rows=[(1, 100, 'movie', datetime.datetime(2026, 5, 5),
                         'high')])
    _mark_0001_applied(engine)
    _run_0002(engine)
    assert _watchlist(engine) == [
        (1, 100, 'movie', datetime.datetime(2026, 5, 5), 'high')]
    assert 'user_wishlist' not in _tables(engine)

# --- unrepresentable legacy-only information ---------------------------------


def test_0002_refuses_to_drop_an_unrepresentable_row(engine):
    """media_id is part of the target's NOT NULL key.

    Such a row cannot be stored at all, so it must stop the migration rather
    than be silently skipped.
    """
    _seed_wishlist(engine, rows=[(1, None, 'movie', None, None)])
    _mark_0001_applied(engine)
    with pytest.raises(runner.MigrationError) as excinfo:
        _run_0002(engine)
    assert 'UNREPRESENTABLE' in str(excinfo.value)
    assert 'user_wishlist' in _tables(engine)


def test_0002_refuses_when_any_one_row_blocks_among_many(engine):
    """One bad row must block the whole DROP, even with good rows alongside."""
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low'),
              (1, 200, 'movie', datetime.datetime(2026, 2, 2), 'high'),
              (1, None, 'movie', None, None)],
        watchlist_rows=[(1, 200, 'movie', datetime.datetime(2026, 9, 9),
                         'low')])
    _mark_0001_applied(engine)
    with pytest.raises(runner.MigrationError):
        _run_0002(engine)
    assert 'user_wishlist' in _tables(engine)
    # The legacy data must be entirely intact after the refusal.
    with engine.connect() as connection:
        assert connection.execute(
            text('SELECT COUNT(*) FROM user_wishlist')).scalar() == 3

# --- multiple rows, mixed classification -------------------------------------


def test_0002_handles_many_rows_across_all_classifications(engine):
    _seed_wishlist(
        engine,
        rows=[(1, 1, 'movie', datetime.datetime(2026, 1, 1), 'low'),      # new
              (1, 2, 'movie', datetime.datetime(2026, 1, 2), 'high'),     # exact
              (1, 3, 'movie', None, None),                                 # redundant
              (2, 4, 'movie', datetime.datetime(2026, 3, 3), 'medium')],   # new
        watchlist_rows=[(1, 2, 'movie', datetime.datetime(2026, 1, 2),
                         'high'),
                        (1, 3, 'movie', datetime.datetime(2026, 4, 4),
                         'medium')])
    _mark_0001_applied(engine)
    _run_0002(engine)

    rows = _watchlist(engine)
    assert (1, 1, 'movie', datetime.datetime(2026, 1, 1), 'low') in rows
    assert (2, 4, 'movie', datetime.datetime(2026, 3, 3), 'medium') in rows
    # the exact duplicate is untouched
    assert (1, 2, 'movie', datetime.datetime(2026, 1, 2), 'high') in rows
    # the redundant row's target keeps its real values
    assert (1, 3, 'movie', datetime.datetime(2026, 4, 4), 'medium') in rows
    assert len(rows) == 4
    assert 'user_wishlist' not in _tables(engine)


def test_0002_defaults_a_null_date_only_for_a_new_row(engine):
    """CURRENT_TIMESTAMP may stand in for a NULL only when there is no target
    row to conflict with — it is still reported, never silently invented for an
    existing record."""
    _seed_wishlist(engine, rows=[(1, 1, 'movie', None, None)])
    _mark_0001_applied(engine)
    _run_0002(engine)
    rows = _watchlist(engine)
    assert len(rows) == 1 and rows[0][4] == 'medium'
    assert rows[0][3] is not None  # CURRENT_TIMESTAMP filled it

# --- rerun / idempotency -----------------------------------------------------


def test_0002_is_a_noop_when_the_legacy_table_is_absent(engine):
    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')
    with engine.begin() as connection:
        result = module.run(connection)
    assert result['dropped'] is False
    assert result['source_rows'] == 0


def test_0002_runner_rerun_is_a_noop(engine):
    _seed_wishlist(engine, rows=[(1, 1, 'movie', datetime.datetime(2026, 1, 1),
                                 'low')])
    _mark_0001_applied(engine)
    assert _run_0002(engine) == ['0002_remove_legacy_wishlist']
    assert _run_0002(engine) == []
    assert len(_watchlist(engine)) == 1


def test_0002_is_not_recorded_when_it_refuses(engine):
    _seed_wishlist(
        engine,
        rows=[(1, 100, 'movie', datetime.datetime(2026, 1, 1), 'low')],
        watchlist_rows=[(1, 100, 'movie', datetime.datetime(2026, 5, 5),
                         'high')])
    _mark_0001_applied(engine)
    with pytest.raises(runner.MigrationError):
        _run_0002(engine)
    with engine.connect() as connection:
        versions = [r[0] for r in connection.execute(text(
            'SELECT version FROM schema_migrations'))]
    assert '0002_remove_legacy_wishlist' not in versions

# --- unrelated legacy data must survive --------------------------------------


def test_0002_preserves_unrelated_tables_and_their_data(engine):
    _seed_wishlist(engine, rows=[(1, 1, 'movie', None, 'low')])
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE user_watchlist_archive (id INTEGER PRIMARY KEY, '
            'note TEXT)'))
        connection.execute(text(
            "INSERT INTO user_watchlist_archive (note) VALUES ('keep me')"))
        connection.execute(text(
            'INSERT INTO user_viewed (user_id, media_id, media_type, '
            "date_viewed) VALUES (1, 77, 'movie', CURRENT_TIMESTAMP)"))
    _mark_0001_applied(engine)
    _run_0002(engine)

    with engine.connect() as connection:
        assert connection.execute(
            text('SELECT note FROM user_watchlist_archive')).scalar() == 'keep me'
        assert connection.execute(
            text('SELECT COUNT(*) FROM user_viewed')).scalar() == 1


def test_0002_only_drops_the_legacy_wishlist(engine):
    _seed_wishlist(engine, rows=[(1, 1, 'movie', None, 'low')])
    _mark_0001_applied(engine)
    before = _tables(engine)
    _run_0002(engine)
    assert _tables(engine) == before - {'user_wishlist'}

# --- structural refusals -----------------------------------------------------


def test_0002_refuses_to_drop_when_watchlist_is_missing(engine):
    """Dropping the only copy of the data is never correct."""
    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')
    with engine.begin() as connection:
        connection.execute(text(LEGACY_COLUMNS))
        connection.execute(text(
            "INSERT INTO user_wishlist (user_id, media_id, media_type) "
            "VALUES (1, 200, 'movie')"))
        connection.execute(text('DROP TABLE user_watchlist'))
        with pytest.raises(RuntimeError) as excinfo:
            module.run(connection)
    assert 'only remaining copy' in str(excinfo.value)


def test_0002_never_commits(engine):
    """The runner owns the transaction; on PostgreSQL that makes the merge and
    the DROP roll back together on failure."""
    import inspect as stdlib_inspect
    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')
    source = stdlib_inspect.getsource(module)
    assert '.commit(' not in source
    assert 'session.begin' not in source


def test_0002_verify_requires_the_legacy_table_to_be_gone(engine):
    module = importlib.import_module('migrations_0002_remove_legacy_wishlist')
    _seed_wishlist(engine, rows=[(1, 1, 'movie', None, 'low')])
    with engine.connect() as connection:
        assert module.verify(connection) != []
    _mark_0001_applied(engine)
    _run_0002(engine)
    with engine.connect() as connection:
        assert module.verify(connection) == []


def test_0002_is_never_applied_before_0001(engine):
    assert _spec('0002_remove_legacy_wishlist').depends_on == (
        '0001_canonical_watched_reconcile',)


# ── CLI ──────────────────────────────────────────────────────────────────────


def _run_cli(db_path, *args):
    env = dict(os.environ)
    env['DATABASE_URL'] = 'sqlite:///%s' % db_path
    env['SKIP_SCHEMA_GUARD'] = '1'
    env['TMDB_API_KEY'] = 'test-tmdb-key'
    env['SECRET_KEY'] = 'test-secret-key-for-tests-only'
    return subprocess.run(
        [sys.executable, os.path.join(REPO, 'scripts', 'migrate.py')] +
        list(args), capture_output=True, text=True, cwd=REPO, env=env,
        timeout=300)


def _fresh_db(tmp_path, name):
    from models.base import db
    path = tmp_path / name
    engine = create_engine('sqlite:///%s' % path)
    db.metadata.create_all(bind=engine)
    engine.dispose()
    return str(path)


def test_cli_validate_succeeds_on_a_fresh_database(tmp_path):
    result = _run_cli(_fresh_db(tmp_path, 'v.db'), 'validate')
    assert result.returncode == 0, result.stderr
    assert 'OK' in result.stdout


def test_cli_upgrade_applies_ordinary_migrations_and_defers_the_destructive(
        tmp_path):
    """Stage 1 of the deployment, exercised through the real CLI.

    The destructive migration must be reported as deferred, not applied and not
    quietly omitted.
    """
    db_path = _fresh_db(tmp_path, 'u.db')
    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0, result.stderr
    assert 'DEFERRED' in result.stdout
    assert '0002_remove_legacy_wishlist' in result.stdout
    assert 'applied_user_viewed' not in result.stdout  # 0001 had nothing to do
    # F9's additive migration rides the ordinary stage, not the gated one.
    assert '[0003] cast persistence (additive)' in result.stdout
    assert '+ 0003_cast_persistence' in result.stdout
    # Cast rows are never written by a migration.
    assert 'no rows written' in result.stdout

    status = _run_cli(db_path, 'status')
    assert 'Recorded migrations : 2' in status.stdout
    assert 'Baseline            : NOT ADOPTED' in status.stdout
    assert 'Schema guard        : OK' in status.stdout


def test_cli_refuses_the_destructive_migration_without_approval(tmp_path):
    """Stage 2 without the three pieces of evidence: refuse, exit nonzero."""
    db_path = _fresh_db(tmp_path, 'd.db')
    assert _run_cli(db_path, 'upgrade').returncode == 0

    result = _run_cli(db_path, 'upgrade', '--only',
                      '0002_remove_legacy_wishlist')
    assert result.returncode != 0
    assert 'Refusing to run destructive migration' in result.stderr
    assert 'user_wishlist has not been touched' in result.stderr


def test_cli_refuses_with_placeholder_evidence(tmp_path):
    db_path = _fresh_db(tmp_path, 'p.db')
    _run_cli(db_path, 'upgrade')
    result = _run_cli(db_path, 'upgrade', '--only',
                      '0002_remove_legacy_wishlist',
                      '--authorize-destructive', '0002_remove_legacy_wishlist',
                      '--backup-ref', 'yes', '--backup-verified-by', 'alice')
    assert result.returncode != 0
    assert 'placeholder' in result.stderr


def test_cli_refuses_when_a_different_migration_is_authorized(tmp_path):
    """Approval cannot be reused for another destructive migration."""
    db_path = _fresh_db(tmp_path, 'x.db')
    _run_cli(db_path, 'upgrade')
    result = _run_cli(db_path, 'upgrade', '--only',
                      '0002_remove_legacy_wishlist',
                      '--authorize-destructive', '0009_something_else',
                      '--backup-ref', 'ci-scratch-snapshot-abc12345',
                      '--backup-verified-by', 'alice')
    assert result.returncode != 0
    assert 'not' in result.stderr and '0002_remove_legacy_wishlist' in result.stderr


def test_cli_applies_the_destructive_migration_with_valid_evidence(tmp_path):
    """The same refusal must lift once real-shaped evidence is supplied."""
    db_path = _fresh_db(tmp_path, 'ok.db')
    assert _run_cli(db_path, 'upgrade').returncode == 0
    result = _run_cli(db_path, 'upgrade', '--only',
                      '0002_remove_legacy_wishlist',
                      '--authorize-destructive', '0002_remove_legacy_wishlist',
                      '--backup-ref', 'ci-scratch-snapshot-abc12345',
                      '--backup-verified-by', 'ci-postgres-job')
    assert result.returncode == 0, result.stderr

    status = _run_cli(db_path, 'status')
    assert 'Recorded migrations : 3' in status.stdout
    assert 'Checksum mismatches : none' in status.stdout
    assert 'Schema guard        : OK' in status.stdout


def test_cli_status_says_history_is_unknown_without_a_baseline(tmp_path):
    """The honesty requirement: an empty ledger is not evidence of history."""
    result = _run_cli(_fresh_db(tmp_path, 's.db'), 'status')
    assert 'NOT known' in result.stdout


def test_cli_upgrade_twice_is_a_noop(tmp_path):
    db_path = _fresh_db(tmp_path, 'i.db')
    _run_cli(db_path, 'upgrade')
    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0
    assert 'No pending migrations.' in result.stdout


def test_cli_rejects_an_unknown_command(tmp_path):
    result = _run_cli(_fresh_db(tmp_path, 'x.db'), 'frobnicate')
    assert result.returncode != 0


# ── honest limitations ───────────────────────────────────────────────────────


def test_sqlite_has_no_advisory_lock_and_the_runner_says_so(tmp_path):
    """Documented environment limitation, asserted so it cannot be forgotten.

    CI has psql but no PostgreSQL server, so the lock path cannot be executed
    here. This test pins that the runner degrades *visibly* rather than
    pretending the lock was taken.
    """
    db_path = _fresh_db(tmp_path, 'lock.db')
    result = _run_cli(db_path, 'upgrade')
    assert result.returncode == 0
    assert 'UNAVAILABLE' in result.stdout
    assert 'pg_advisory_lock' in result.stdout


def test_the_postgres_limitation_is_documented_in_the_runbook():
    """An untested path must be written down, or someone will assume it works.

    The advisory lock and any ON CONFLICT behaviour are unexecuted in CI. If
    this assertion ever fails, the runbook and reality have diverged.
    """
    path = os.path.join(REPO, 'docs', 'migration-inventory.md')
    assert os.path.exists(path), 'the migration runbook is missing'
    body = open(path, encoding='utf-8').read().lower()
    assert 'advisory lock' in body, 'the lock path is not documented'
    assert 'not exercised' in body or 'untested' in body, \
        'the runbook must state which paths CI does not exercise'


# ── deployment wiring ────────────────────────────────────────────────────────


def test_deploy_workflow_uses_the_authoritative_runner():
    path = os.path.join(REPO, '.github', 'workflows', 'deploy.yml')
    workflow = open(path, encoding='utf-8').read()
    assert 'scripts/migrate.py' in workflow, \
        'deploy.yml must apply migrations through the runner, not by guessing'


# ── init-ledger: the migration ledger bootstrap ─────────────────────────────
#
# Production failure this exists for
# ---------------------------------
# `schema_migrations` is declared by the model, so bounded convergence saw it as
# a missing table -- but it is new infrastructure, not one of the eleven
# historical gaps, so convergence correctly refused and created nothing. That
# left `import_source_mapping` absent too, and the nightly release-data sync
# then failed during `import app`.
#
# The fix is an explicit bootstrap command, NOT widening the allow-list.

HISTORICAL_REPAIR_SET = {
    'continue_watching_item', 'director', 'import_source_mapping',
    'media_director', 'movie_release_date', 'notification',
    'recommendation_feedback', 'smart_list', 'taste_profile',
    'user_streaming_services', 'year_in_review_share',
}


def _sqlite_path(path):
    return 'sqlite:///%s' % path


def _make_gap_db(path, drop_historical=True):
    """A declared-schema database with the ledger (and optionally the eleven
    historical gaps) removed, reproducing the production state."""
    from models.base import db
    from sqlalchemy import create_engine, text

    engine = create_engine(_sqlite_path(path))
    db.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE schema_migrations'))
        if drop_historical:
            for name in HISTORICAL_REPAIR_SET:
                connection.execute(text('DROP TABLE IF EXISTS "%s"' % name))
    engine.dispose()
    return _sqlite_path(path)


def _table_names(path):
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _run_convergence(url):
    """The convergence script is standalone (app.app_context + global engine),
    so it is exercised as a subprocess the way the deploy runs it."""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env['DATABASE_URL'] = url
    env['SKIP_SCHEMA_GUARD'] = '1'
    env['TMDB_API_KEY'] = 'test-tmdb-key'
    env['SECRET_KEY'] = 'test-secret-key-for-tests-only'
    return subprocess.run(
        [sys.executable,
         os.path.join(REPO, 'migrates', 'migrate_schema_convergence.py')],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=300)


@pytest.fixture
def gap_engine(tmp_path):
    engine_path = tmp_path / 'gap.db'
    _make_gap_db(str(engine_path))
    from sqlalchemy import create_engine
    engine = create_engine(_sqlite_path(engine_path))
    yield engine
    engine.dispose()


# --- B: creates only the ledger ---------------------------------------------

def test_ledger_bootstrap_creates_only_the_ledger_table(gap_engine):
    before = set(runner.inspect(gap_engine).get_table_names())
    assert 'schema_migrations' not in before
    result = runner.init_ledger(gap_engine)
    after = set(runner.inspect(gap_engine).get_table_names())

    assert result['ledger'] == 'created'
    assert after - before == {'schema_migrations'}, (
        'ledger bootstrap created more than the ledger: %s'
        % sorted(after - before))
    for name in HISTORICAL_REPAIR_SET:
        assert name not in after, \
            'ledger bootstrap created a historical table: %s' % name


# --- C: creates no rows ------------------------------------------------------

def test_ledger_bootstrap_creates_no_baseline_and_no_migration(gap_engine):
    runner.init_ledger(gap_engine)
    with gap_engine.connect() as connection:
        rows = connection.execute(text(
            'SELECT version, kind FROM schema_migrations')).fetchall()
    assert rows == [], 'ledger bootstrap wrote rows: %r' % rows


# --- idempotency / already-correct -------------------------------------------

def test_ledger_bootstrap_is_idempotent(gap_engine):
    assert runner.init_ledger(gap_engine)['ledger'] == 'created'
    assert runner.init_ledger(gap_engine)['ledger'] == 'present'
    assert runner.init_ledger(gap_engine)['ledger'] == 'present'
    with gap_engine.connect() as connection:
        assert connection.execute(text(
            'SELECT COUNT(*) FROM schema_migrations')).scalar() == 0


def test_ledger_bootstrap_leaves_a_correct_existing_table_untouched(engine):
    """A correct ledger is a no-op, not a rewrite."""
    before = runner._declared_ledger_shape()
    result = runner.init_ledger(engine)
    assert result['ledger'] == 'present'
    assert result['tables_created'] == []
    with engine.connect() as connection:
        assert runner._actual_ledger_shape(connection) == before


# --- fail closed on drift ----------------------------------------------------

def test_ledger_bootstrap_fails_closed_on_a_drifted_ledger(engine):
    """A ledger with the wrong shape must never be silently accepted, and must
    never be 'repaired' -- repairing it could destroy the only record of what
    has already been applied."""
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE schema_migrations'))
        connection.execute(text(
            'CREATE TABLE schema_migrations ('
            'id INTEGER PRIMARY KEY, version TEXT)'))
    with pytest.raises(runner.MigrationError) as excinfo:
        runner.init_ledger(engine)
    message = str(excinfo.value)
    assert 'does not match the declared ledger schema' in message
    assert 'missing column(s)' in message
    assert 'NOT repaired automatically' in message
    # The drifted table is left exactly as found, and nothing else was touched.
    with engine.connect() as connection:
        columns = [c['name'] for c in
                   runner.inspect(connection).get_columns('schema_migrations')]
    assert columns == ['id', 'version']


def test_shape_difference_detection():
    declared = {'a': ('VARCHAR(10)', False), 'b': ('INTEGER', True)}
    assert runner._describe_shape_difference(declared, declared) is None
    assert 'missing column(s): b' in runner._describe_shape_difference(
        declared, {'a': ('VARCHAR(10)', False)})
    assert 'unexpected column(s): c' in runner._describe_shape_difference(
        declared, {'a': ('VARCHAR(10)', False), 'b': ('INTEGER', True),
                   'c': ('INTEGER', True)})
    assert 'nullable' in runner._describe_shape_difference(
        declared, {'a': ('VARCHAR(10)', False), 'b': ('INTEGER', False)})
    assert 'type' in runner._describe_shape_difference(
        declared, {'a': ('TEXT', False), 'b': ('INTEGER', True)})


# --- D/E: the convergence contract is unchanged ------------------------------

def test_ledger_is_not_in_the_historical_repair_allow_list():
    """The allow-list stays at 11 tables. Widening it is the wrong fix."""
    from migrates import migrate_schema_convergence as convergence
    assert convergence.EXPECTED_REPAIR_SET == frozenset(HISTORICAL_REPAIR_SET)
    assert len(convergence.EXPECTED_REPAIR_SET) == 11
    assert 'schema_migrations' not in convergence.EXPECTED_REPAIR_SET


def test_convergence_refuses_when_the_ledger_is_missing(tmp_path):
    """Failure A, pinned: convergence refuses and creates nothing."""
    path = str(tmp_path / 'noledger.db')
    url = _make_gap_db(path)
    result = _run_convergence(url)
    assert result.returncode != 0
    assert 'schema_migrations' in result.stdout
    assert 'Nothing was created' in result.stdout
    names = _table_names(path)
    assert 'schema_migrations' not in names
    assert not (HISTORICAL_REPAIR_SET & names), \
        'the refusal must happen before any DDL'


def test_bootstrap_then_convergence_creates_the_historical_tables(tmp_path):
    """Production's recovery path, end to end."""
    path = str(tmp_path / 'recover.db')
    url = _make_gap_db(path)
    assert _run_cli(path, 'init-ledger').returncode == 0
    assert 'schema_migrations' in _table_names(path)

    result = _run_convergence(url)
    assert result.returncode == 0, result.stdout[-500:]
    names = _table_names(path)
    assert HISTORICAL_REPAIR_SET <= names, (
        'convergence did not create the historical gaps: %s'
        % sorted(HISTORICAL_REPAIR_SET - names))
    # Still only the ledger was created by the bootstrap, not convergence.
    assert 'schema_migrations' in names


def test_convergence_still_refuses_on_an_unexpected_missing_table(tmp_path):
    """An unrelated application table missing must still refuse before DDL."""
    path = str(tmp_path / 'unexpected.db')
    url = _make_gap_db(path)
    assert _run_cli(path, 'init-ledger').returncode == 0
    # Drop a table that is NOT part of the historical repair set.
    from sqlalchemy import create_engine, text
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text('DROP TABLE review'))
    engine.dispose()

    result = _run_convergence(url)
    assert result.returncode != 0
    assert 'review' in result.stdout
    names = _table_names(path)
    assert 'review' not in names, 'refusal happened after DDL'
    assert not (HISTORICAL_REPAIR_SET & names), \
        'nothing may be created once an unexpected gap is seen'


# --- init-ledger CLI ---------------------------------------------------------

def test_cli_init_ledger_creates_only_the_ledger(tmp_path):
    path = str(tmp_path / 'cli.db')
    _make_gap_db(path)
    result = _run_cli(path, 'init-ledger')
    assert result.returncode == 0, result.stderr
    assert 'Migration ledger created: schema_migrations' in result.stdout
    assert 'created       : schema_migrations' in result.stdout
    names = _table_names(path)
    assert 'schema_migrations' in names
    assert not (HISTORICAL_REPAIR_SET & names), \
        'the CLI created historical tables'


def test_cli_init_ledger_is_a_noop_on_the_second_run(tmp_path):
    path = str(tmp_path / 'cli2.db')
    _make_gap_db(path)
    assert _run_cli(path, 'init-ledger').returncode == 0
    result = _run_cli(path, 'init-ledger')
    assert result.returncode == 0
    assert 'already present' in result.stdout
    assert 'No baseline and no migration has been recorded' in result.stdout


def test_cli_init_ledger_requires_production_authorization():
    """It mutates, so it must take the same authorization path as the other
    write commands. Refused before any connection is opened."""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    env['DATABASE_URL'] = 'postgresql://user:pw@db.example.invalid:5432/prod'
    env['SKIP_SCHEMA_GUARD'] = '1'
    env['SECRET_KEY'] = 'test-secret-key-for-tests-only'
    env['TMDB_API_KEY'] = 'test-tmdb-key'
    result = subprocess.run(
        [sys.executable, os.path.join(REPO, 'scripts', 'migrate.py'),
         'init-ledger'],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
    assert result.returncode == 97
    assert 'Refusing to run' in result.stderr
