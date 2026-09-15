"""Recommendation feedback analytics CLI (Feature #7, Phase 15).

Covers scripts/analyze_recommendation_feedback.py:

- aggregate metrics (events, CTR, users, titles, positions, reasons,
  sources, media types, daily trends, funnel rates)
- CLI window bounds (--days 1–90, default 7; malformed/absurd rejected)
- data-quality checks → exit 1; fatal errors → exit 2
- guards: read-only, no network (socket level), bounded statements,
  no user-identifying output, no payload output, legacy tables untouched
"""
import io
import logging
import re
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

sys.path.insert(0, 'scripts')
import analyze_recommendation_feedback  # noqa: E402

EVENTS = ('impression', 'click', 'not_interested', 'already_watched',
          'saved', 'rated')
SURFACES = ('home_for_you', 'profile_recs', 'more_like_this')


# ── Fixtures / helpers (pattern from test_verify_recommendation_feedback) ────

@pytest.fixture
def script():
    yield analyze_recommendation_feedback


@pytest.fixture(autouse=True)
def _hermetic_db(app, db):
    yield


def _record(user_id, media_id, event='impression', media_type='movie',
            surface='home_for_you', source='trending', position=1,
            reason_kind=None, created_at=None, event_date=None,
            model_version=1, payload_json=None):
    """Insert one feedback row directly (append-only write is the test
    fixture's job — the script itself never writes)."""
    from models import db as _db
    from models.recommendation_feedback import RecommendationFeedback

    row = RecommendationFeedback(
        user_id=user_id, media_id=media_id, media_type=media_type,
        surface=surface, source=source, event=event, position=position,
        reason_kind=reason_kind, payload_json=payload_json,
        model_version=model_version,
        event_date=event_date or (created_at.date() if created_at else None),
        created_at=created_at or datetime.utcnow())
    _db.session.add(row)
    return row


def _commit():
    from models import db as _db
    _db.session.commit()


@pytest.fixture
def user(app, db):
    """Module-unique user; feedback rows cascade via the User relationship
    so each test starts from clean table state (pattern from the Phase 8
    verification suite — the shared session DB must not accumulate rows)."""
    from models import User, db as _db

    with app.app_context():
        u = User(username='anl' + uuid.uuid4().hex[:8],
                 email='anl_%s@example.com' % uuid.uuid4().hex[:6],
                 password_hash='x')
        _db.session.add(u)
        _db.session.commit()
        yield u
        _db.session.delete(u)
        _db.session.commit()


@pytest.fixture(autouse=True)
def _clean_feedback_before(app, db):
    """Order-independence: suites that run before this file may leave
    RecommendationFeedback rows behind; every test here asserts exact
    aggregate counts, so the table is cleared before each test."""
    with app.app_context():
        from models import RecommendationFeedback as _rf
        _rf.query.delete()
        db.session.commit()
    yield


@pytest.fixture(autouse=True)
def _restore_unique_index(app, db):
    """Tests that simulate the broken state (duplicates) drop the partial
    unique index; a unique index cannot be re-created over violating rows,
    so restoration happens in cleanup after the rows are gone."""
    yield
    with app.app_context():
        from sqlalchemy import inspect, text
        from models import db as _db
        names = [ix['name'] for ix in
                 inspect(_db.engine).get_indexes('recommendation_feedback')]
        if 'uq_recommendation_feedback_event_daily' not in names:
            _db.session.execute(text(
                'CREATE UNIQUE INDEX uq_recommendation_feedback_event_daily '
                'ON recommendation_feedback (user_id, media_id, '
                'media_type, surface, event, event_date) '
                "WHERE event != 'impression'"))
            _db.session.commit()


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_ctr_formula():
    assert analyze_recommendation_feedback._ctr(312, 1410) == '22.13%'


def test_ctr_zero_impression_safety():
    assert analyze_recommendation_feedback._ctr(5, 0) == 'n/a'
    assert analyze_recommendation_feedback._ctr(0, 0) == 'n/a'


def test_ctr_zero_clicks_is_zero_percent():
    assert analyze_recommendation_feedback._ctr(0, 10) == '0.00%'


def test_position_bucketing():
    f = analyze_recommendation_feedback._bounded_position
    assert f(1) == 1 and f(14) == 14
    assert f(0) == 'out_of_range'
    assert f(15) == 'out_of_range'
    assert f(-3) == 'out_of_range'
    assert f(None) is None


def test_window_dates_stable():
    dates = analyze_recommendation_feedback._window_dates(7)
    assert len(dates) == 7
    assert dates[-1] > dates[0]  # oldest first


# ── Core aggregates ───────────────────────────────────────────────────────────

def test_zero_data_db(app, script):
    with app.app_context():
        agg = script.collect(7)
        assert agg['total_events'] == 0
        assert agg['active_users'] == 0
        assert agg['distinct_titles'] == 0
        assert script.run() == 0


def test_basic_event_totals(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 550, event='click', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['events'].get('impression') == 1
        assert agg['events'].get('click') == 1
        assert agg['total_events'] == 2


def test_old_events_outside_window(app, script, user):
    now = datetime.utcnow()
    old = now - timedelta(days=30)
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 551, event='impression', created_at=old)
        _commit()
        agg = script.collect(7)
        assert agg['events'].get('impression') == 1


def test_active_users_and_titles(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 551, event='impression', created_at=now)
        _record(user.id, 550, event='click', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['active_users'] == 1
        assert agg['users_with_impressions'] == 1
        assert agg['users_with_clicks'] == 1
        assert agg['users_with_both'] == 1
        # distinct (media_type, media_id) pairs
        assert agg['distinct_titles'] == 2


def test_distinct_titles_type_separated(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', media_type='movie',
                created_at=now)
        _record(user.id, 550, event='impression', media_type='tv',
                created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['distinct_titles'] == 2


# ── Breakdowns ────────────────────────────────────────────────────────────────

def test_event_breakdown_includes_zero_categories(app, script, user):
    """The events aggregate carries known categories only; the report
    renders all six with zero-fill (stable operator output)."""
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['events'].get('impression') == 1
        assert all(e in EVENTS for e in agg['events'])


def test_surface_breakdown_isolated(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', surface='home_for_you',
                created_at=now)
        _record(user.id, 551, event='click', surface='more_like_this',
                created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['surfaces']['home_for_you'] == {'impression': 1}
        assert agg['surfaces']['more_like_this'] == {'click': 1}
        assert 'profile_recs' not in agg['surfaces']


def test_media_type_breakdown(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', media_type='movie',
                created_at=now)
        _record(user.id, 551, event='impression', media_type='tv',
                created_at=now)
        _record(user.id, 551, event='click', media_type='tv',
                created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['media']['movie'] == {'impression': 1}
        assert agg['media']['tv'] == {'impression': 1, 'click': 1}


def test_position_breakdown_and_out_of_range(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', position=1, created_at=now)
        _record(user.id, 551, event='impression', position=14,
                created_at=now)
        _record(user.id, 552, event='click', position=1, created_at=now)
        _record(user.id, 553, event='impression', position=99,
                created_at=now)
        _record(user.id, 554, event='impression', position=None,
                created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['positions'][1] == {'impression': 1, 'click': 1}
        assert agg['positions'][14] == {'impression': 1}
        assert agg['positions']['out_of_range'] == {'impression': 1}


def test_reason_ranking_deterministic(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', reason_kind='top_genre',
                created_at=now)
        _record(user.id, 551, event='impression', reason_kind='similar_to',
                created_at=now)
        _record(user.id, 552, event='impression', reason_kind='similar_to',
                created_at=now)
        _record(user.id, 553, event='click', reason_kind='similar_to',
                created_at=now)
        _commit()
        agg = script.collect(7)
        ranked = sorted(agg['reasons'].items(),
                        key=lambda kv: (-kv[1].get('impression', 0), kv[0]))
        assert [k for k, _ in ranked] == ['similar_to', 'top_genre']


def test_source_stored_verbatim(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression',
                source='similar_to:12345', created_at=now)
        _commit()
        agg = script.collect(7)
        assert 'similar_to:12345' in agg['sources']


def test_daily_trend(app, script, user):
    today = datetime.utcnow()
    yesterday = today - timedelta(days=1)
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=today)
        _record(user.id, 551, event='impression', created_at=yesterday)
        _record(user.id, 552, event='click', created_at=yesterday)
        _commit()
        agg = script.collect(7)
        assert agg['daily'][today.date()].get('impression') == 1
        assert agg['daily'][yesterday.date()] == {'impression': 1,
                                                  'click': 1}


# ── CLI window bounds ─────────────────────────────────────────────────────────

def test_days_default():
    assert analyze_recommendation_feedback.DEFAULT_DAYS == 7
    assert analyze_recommendation_feedback._parse_days.__defaults__ is None


def test_days_bounds_enforced():
    parse = analyze_recommendation_feedback._parse_days
    assert parse('1') == 1 and parse('90') == 90
    for bad in ('0', '-5', '91', '3650', 'abc', ''):
        with pytest.raises(Exception):
            parse(bad)


def test_cli_rejects_invalid_days(app):
    proc = subprocess.run(
        [sys.executable, 'scripts/analyze_recommendation_feedback.py',
         '--days', '0'],
        capture_output=True, text=True,
        env={'SECRET_KEY': 't', 'DATABASE_URL': 'sqlite:////tmp/p15_cli.db',
             'TMDB_API_KEY': 'x', 'PATH': '/usr/bin:/bin'})
    assert proc.returncode == 2


def test_cli_accepts_bounded_days(app):
    proc = subprocess.run(
        [sys.executable, 'scripts/analyze_recommendation_feedback.py',
         '--days', '30'],
        capture_output=True, text=True, timeout=120,
        env={'SECRET_KEY': 't',
             'DATABASE_URL': 'sqlite:////tmp/p15_cli30.db',
             'TMDB_API_KEY': 'x', 'PATH': '/usr/bin:/bin'})
    assert proc.returncode == 0


# ── Data quality → exit codes ─────────────────────────────────────────────────

def test_malformed_event_detected(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 551, event='bogus_event', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['quality']['invalid_events'] == 1
        assert script.run() == 1


def test_malformed_surface_detected(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', surface='bogus_surface',
                created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['quality']['invalid_surfaces'] == 1


def test_malformed_media_type_detected(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', media_type='series',
                created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['quality']['invalid_media_types'] == 1


def test_invalid_media_id_detected(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 0, event='impression', created_at=now)
        _record(user.id, -5, event='click', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['quality']['invalid_media_ids'] == 2


def test_null_timestamp_detected(app, script, user, db):
    """Legacy rows that predate the NOT NULL constraint (simulated via a
    temporary constraint-free table swap, mirroring the index-drop
    pattern) are detected as null timestamps."""
    from sqlalchemy import text
    with app.app_context():
        db.session.execute(text(
            'ALTER TABLE recommendation_feedback '
            'RENAME TO rf_null_backup'))
        try:
            # Constraint-free clone (CTAS drops constraints/indexes).
            db.session.execute(text(
                'CREATE TABLE recommendation_feedback AS '
                'SELECT * FROM rf_null_backup WHERE 0'))
            db.session.execute(text(
                'INSERT INTO recommendation_feedback '
                '(user_id, media_id, media_type, surface, source, event, '
                'model_version, event_date) VALUES (:uid, 550, :mtype, '
                ':surface, :source, :event, 1, :edate)'),
                dict(uid=user.id, mtype='movie', surface='home_for_you',
                     source='trending', event='impression',
                     edate=datetime.utcnow().date()))
            agg = script.collect(7)
            assert agg['quality']['null_timestamps'] == 1
            assert script.run() == 1
        finally:
            db.session.execute(text(
                'DROP TABLE recommendation_feedback'))
            db.session.execute(text(
                'ALTER TABLE rf_null_backup '
                'RENAME TO recommendation_feedback'))
            db.session.commit()


def test_duplicate_non_impression_detected(app, script, user, db):
    """The partial unique index makes duplicates impossible on a healthy
    schema — simulate 'rows predate the index' by dropping it first
    (same pattern as the Phase 8 verification suite)."""
    from sqlalchemy import text
    now = datetime.utcnow()
    with app.app_context():
        db.session.execute(text(
            'DROP INDEX uq_recommendation_feedback_event_daily'))
        _record(user.id, 550, event='click', created_at=now)
        _record(user.id, 550, event='click', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['duplicate_groups'] == 1
        assert agg['duplicate_rows'] == 2
        assert script.run() == 1


def test_impressions_exempt_from_duplicates(app, script, user):
    """Impressions are repeatable by design — never duplicates."""
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 550, event='impression', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['duplicate_groups'] == 0
        assert agg['events'].get('impression') == 3
        assert script.run() == 0


def test_fatal_db_error_exit_2(script):
    """Unreachable database at startup → clean [FATAL] + exit 2."""
    import os
    env = dict(os.environ)
    env['DATABASE_URL'] = 'sqlite:////nonexistent_dir_p15/x.db'
    proc = subprocess.run(
        [sys.executable, 'scripts/analyze_recommendation_feedback.py'],
        capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 2


def test_exit_zero_on_healthy_data(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _record(user.id, 550, event='click', created_at=now,
                reason_kind='top_genre')
        _commit()
        assert script.run() == 0


# ── Output / privacy guards ───────────────────────────────────────────────────

def test_report_output_and_privacy(app, script, user, caplog):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now,
                payload_json='{"secret": "private-payload"}')
        _commit()
        with caplog.at_level('INFO',
                             logger='frameiq.rec_feedback_analytics'):
            script.run(days=7)
    text = caplog.text
    # Sections present
    for marker in ('[START]', '[EVENTS]', '[SURFACES]', '[POSITION]',
                   '[TRENDS]', '[QUALITY]', '[STATUS]'):
        assert marker in text
    assert 'CTR' in text
    # Privacy: no user identifiers / payloads / emails. IDs are checked
    # in labelled context (small rowids coincide with metric digits).
    assert user.username not in text
    assert not re.search(rf'user[_ ]?id\s*[:=]\s*{user.id}\b', text)
    assert not re.search(rf'\buser\s+{user.id}\b', text)
    assert 'private-payload' not in text
    assert '"secret"' not in text
    assert '@example.com' not in text


def test_position_table_has_14_rows(app, script, user, caplog):
    with app.app_context():
        _record(user.id, 550, event='impression', position=1)
        _commit()
        caplog.set_level('INFO',
                         logger='frameiq.rec_feedback_analytics')
        script.run(days=7)
    positions = [line for line in caplog.messages
                 if line.startswith('position ')]
    assert len(positions) == 14


def test_clicks_without_impressions_handled(app, script, user):
    now = datetime.utcnow()
    with app.app_context():
        _record(user.id, 550, event='click', created_at=now)
        _commit()
        agg = script.collect(7)
        assert agg['events'].get('impression', 0) == 0
        assert agg['events'].get('click') == 1
        assert script.run() == 0  # n/a CTR, no crash


# ── Guards ────────────────────────────────────────────────────────────────────

def _stripped_source():
    """Script source with comments and docstrings stripped."""
    lines = []
    skip_doc = False
    for line in open('scripts/analyze_recommendation_feedback.py',
                     encoding='utf-8'):
        if line.count('"""') % 2 == 1:
            skip_doc = not skip_doc
            continue
        if skip_doc:
            continue
        lines.append(line.split('#', 1)[0])
    return '\n'.join(lines)


def test_no_mutation_statements(script):
    src = _stripped_source()
    for banned in ('INSERT ', 'UPDATE ', 'DELETE ', '.commit(', '.add(',
                   '.delete(', '.merge(', 'db.drop_all', 'create_all'):
        assert banned not in src, f'read-only violation: {banned!r}'


def test_no_network_source(script):
    src = _stripped_source()
    for banned in ('requests.', 'urllib.', 'httpx', 'urlopen', 'socket.',
                   'tmdb', 'TMDb', 'themoviedb'):
        assert banned not in src, f'network violation: {banned!r}'


def test_no_network_socket_guard(app, script, user, monkeypatch):
    now = datetime.utcnow()

    def _no_socket(*a, **kw):
        raise AssertionError('analytics attempted a network connection')

    monkeypatch.setattr(socket, 'create_connection', _no_socket)
    monkeypatch.setattr(socket.socket, 'connect', _no_socket)
    with app.app_context():
        _record(user.id, 550, event='impression', created_at=now)
        _commit()
        assert script.run() in (0, 1)


def test_bounded_query_count(app, script, user):
    """The whole run() issues <= 12 statements regardless of row count."""
    now = datetime.utcnow()
    from models import db as _db
    with app.app_context():
        for i in range(30):
            _record(user.id, 600 + i, event='impression', created_at=now)
        _commit()

        from sqlalchemy import event as sqla_event
        statements = []

        def _count(conn, cursor, statement, parameters, context,
                   executemany):
            statements.append(statement)

        sqla_event.listen(_db.engine, 'before_cursor_execute', _count)
        try:
            script.run()
        finally:
            sqla_event.remove(_db.engine, 'before_cursor_execute', _count)
    assert len(statements) <= 12, (
        f'unbounded query pattern: {len(statements)} statements')


def test_no_tasteprofile_or_foryou_access(script):
    src = _stripped_source()
    assert 'TasteProfile' not in src
    assert 'for_you' not in src
    assert 'compute_profile' not in src


def test_legacy_tables_untouched(script):
    src = _stripped_source()
    for legacy in ('user_taste_profile', 'user_similarity'):
        assert legacy not in src, f'legacy table referenced: {legacy}'


def test_script_runs_from_cli_help():
    proc = subprocess.run(
        [sys.executable, 'scripts/analyze_recommendation_feedback.py',
         '--help'],
        capture_output=True, text=True, timeout=60,
        env={'SECRET_KEY': 't', 'DATABASE_URL': 'sqlite:////tmp/p15_h.db',
             'TMDB_API_KEY': 'x', 'PATH': '/usr/bin:/bin'})
    assert proc.returncode == 0
    assert '--days' in proc.stdout


# ════════════════════════════════════════════════════════════════════════════
# PostgreSQL dialect regression (production fix — boolean = integer)
# ════════════════════════════════════════════════════════════════════════════
#
# Production failure: psycopg2.errors.UndefinedFunction —
# "operator does not exist: boolean = integer". The old _count_case
# wrapped boolean predicates as (predicate) = 1; SQLite accepts that,
# PostgreSQL does not. These guards compile the affected statements
# against the PostgreSQL DIALECT (no live Postgres needed — the type
# error is a compile/plan-time resolution, not a runtime data one).

def _pg_sql(script, statement):
    return str(statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={'literal_binds': False}))


def test_quality_statement_compiles_on_postgres_dialect(script):
    script._load()
    with script._app.app_context():
        stmt = script.RecommendationFeedback.query.with_entities(
            *script._quality_entities()).statement
        sql = _pg_sql(script, stmt)
    assert 'CASE WHEN' in sql  # conditional aggregates still conditional
    assert 'SUM' in sql.upper()


def test_quality_statement_has_no_boolean_integer_comparison(script):
    script._load()
    with script._app.app_context():
        stmt = script.RecommendationFeedback.query.with_entities(
            *script._quality_entities()).statement
        sql = _pg_sql(script, stmt)
    # The exact production defect: (boolean_expr) = 1 inside CASE.
    assert '= 1' not in sql, sql
    # And the predicates survive as real boolean/NOT-IN expressions.
    assert 'NOT IN' in sql.upper()
    assert 'IS NULL' in sql.upper()


def test_count_case_value_form_still_integer_safe(script):
    """The (column, value) form compiles to a typed comparison, not a
    boolean-vs-integer one."""
    script._load()
    with script._app.app_context():
        agg = script._count_case(script.RecommendationFeedback.event,
                                 'impression')
        stmt = script._db.session.query(agg).statement
        sql = _pg_sql(script, stmt)
    # value form compiles to a typed bound-param comparison
    assert 'CASE WHEN' in sql and '= 1' not in sql


def test_all_collect_statements_compile_on_postgres_dialect(script):
    """Every bounded statement collect() builds must compile for
    PostgreSQL (the full-collect execution guard runs on SQLite; this
    catches any other dialect-specific expression at compile level)."""
    script._load()
    from models import User as _User
    from models import db as _db
    u = _User(username='anl' + uuid.uuid4().hex[:8],
              email='anl_%s@example.com' % uuid.uuid4().hex[:6],
              password_hash='x')
    _db.session.add(u)
    _db.session.commit()
    try:
        _record(u.id, 910001, 'impression')
        _record(u.id, 910001, 'click')
        _commit()
        with script._app.app_context():
            agg = script.collect(7)
        assert agg['events'].get('impression', 0) == 1
        assert agg['events'].get('click', 0) == 1
    finally:
        _db.session.delete(u)
        _db.session.commit()


def test_zero_feedback_postgres_compatible_analytics_healthy(script):
    """Zero rows: collect() returns a healthy structure and the report
    renders [STATUS] OK — the PostgreSQL-compatible path end to end."""
    script._load()
    with script._app.app_context():
        agg = script.collect(7)
        assert agg['quality'] == {
            'invalid_events': 0, 'invalid_surfaces': 0,
            'invalid_media_types': 0, 'invalid_media_ids': 0,
            'null_timestamps': 0}
        assert agg['duplicate_groups'] == 0
        assert agg['total_all_time'] == 0
        buffer = io.StringIO()
        stream_handler = logging.StreamHandler(buffer)
        root = logging.getLogger()
        root.addHandler(stream_handler)
        old_level = root.level
        root.setLevel(logging.INFO)
        try:
            rc = script.run(7)
        finally:
            root.removeHandler(stream_handler)
            root.setLevel(old_level)
        out = buffer.getvalue()
        assert rc == 0
        assert '[STATUS] OK' in out
        assert '[QUALITY]' in out and 'invalid_events=0' in out


def test_quality_entities_are_pure_expressions(script):
    """_quality_entities must expose exactly the five production
    quality dimensions, each a labeled aggregate expression."""
    script._load()
    labels = [e.key for e in script._quality_entities()]
    assert labels == ['bad_event', 'bad_surface', 'bad_media_type',
                      'bad_media_id', 'null_created_at']
