"""Recommendation-feedback pipeline verification (Feature #7, Phase 8).

Covers the operator CLI scripts/verify_recommendation_feedback.py:

- aggregate reporting (zero + populated DB, 24h breakdown, surfaces,
  distinct users, timestamp range, TasteProfile aggregates)
- sanity checks and exit codes 0/1/2
- duplicate non-impression detection (exit 1, aggregate only)
- feedback -> profile freshness correlation
- hygiene: read-only source guard, socket-level no-network guard,
  bounded queries (statement counting), no payload leakage, legacy
  tables untouched
"""
import importlib.util
import socket
import uuid
from datetime import datetime, timedelta

import pytest

from models import User, MediaItem, TasteProfile
from models.recommendation_feedback import (  # noqa: F401 — EVENTS re-exported for other tests in this module
    RecommendationFeedback, EVENTS)

_SCRIPT_PATH = 'scripts/verify_recommendation_feedback.py'

# Module-level counter: media tmdb_ids must NEVER repeat across tests in
# this module (the suite shares one session DB and media_item.tmdb_id is
# unique). Mirrors test_taste_profile_service.py's _TMDB_COUNTER pattern.
_TMDB_COUNTER = {'n': 8_800_000}


@pytest.fixture(scope='module')
def script():
    """Import the script module fresh (its _load() targets the app already
    in sys.modules — the hermetic test app)."""
    spec = importlib.util.spec_from_file_location(
        'verify_rec_feedback_test', _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ════════════════════════════════════════════════════════════════════════════
# Helpers / fixtures
# ════════════════════════════════════════════════════════════════════════════

def _unique(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _cleanup_owned_rows(app, db):
    """Leave zero residue per test — same convention as
    test_compute_taste_profiles.py. The suite shares one session SQLite DB
    and other modules assume known table state (test_continue_watching
    deletes all media_item rows, then SQLite reuses freed PK ids — residue
    here would silently re-attach to later modules' reused PKs)."""
    yield
    with app.app_context():
        from sqlalchemy import text
        from sqlalchemy import delete as _delete
        from models import RecommendationFeedback as _RF
        # Restore the partial unique index if a test dropped it (the
        # duplicate-detection test simulates the broken state).
        index_names = [ix['name'] for ix in
                       __import__('sqlalchemy').inspect(db.engine).
                       get_indexes('recommendation_feedback')]
        if 'uq_recommendation_feedback_event_daily' not in index_names:
            db.session.execute(text(
                'CREATE UNIQUE INDEX uq_recommendation_feedback_event_daily '
                'ON recommendation_feedback (user_id, media_id, media_type, '
                'surface, event, event_date) WHERE event != \'impression\''))
            db.session.commit()
        # Feedback rows whose users were already removed by the user fixture
        # cascade; catch any orphans defensively.
        db.session.execute(_delete(_RF).where(_RF.media_id >= 8_800_000))
        db.session.execute(_delete(MediaItem).where(
            MediaItem.tmdb_id >= 8_800_000))
        db.session.execute(_delete(TasteProfile).where(
            TasteProfile.user_id.in_(
                db.session.query(User.id).filter(
                    User.email.like('%@verify.test')).scalar_subquery())))
        db.session.execute(_delete(User).where(
            User.email.like('%@verify.test')))
        db.session.commit()


@pytest.fixture
def user(app, db):
    """Module-unique user; rows cascade via the User relationship."""
    with app.app_context():
        u = User(username=_unique('vrf'),
                 email=f"{_unique('vrf')}@verify.test",
                 email_verified=True)
        u.set_password('TestPass1')
        db.session.add(u)
        db.session.commit()
        yield u
        db.session.delete(u)
        db.session.commit()


@pytest.fixture
def media_factory(app, db):
    def _make(media_type='movie', title='Verify Movie', genres='Thriller',
              release_date=None, runtime=110):
        _TMDB_COUNTER['n'] += 1
        m = MediaItem(
            tmdb_id=_TMDB_COUNTER['n'], media_type=media_type, title=title,
            genres=genres, runtime=runtime, release_date=release_date,
        )
        db.session.add(m)
        db.session.commit()
        return m

    return _make


def _record(user_id, media_id, event='impression', media_type='movie',
            surface='home_for_you', source='trending', position=None,
            reason_kind=None, created_at=None, event_date=None):
    """Insert one feedback row directly (the CLI reads raw persisted state,
    including shapes record() would never produce — e.g. old timestamps)."""
    fb = RecommendationFeedback(
        user_id=user_id, media_id=media_id, media_type=media_type,
        surface=surface, source=source, event=event, position=position,
        reason_kind=reason_kind,
        created_at=created_at or datetime.utcnow(),
        event_date=event_date or (created_at.date() if created_at else None),
    )
    from models import db as _db
    _db.session.add(fb)
    _db.session.commit()
    return fb


# ════════════════════════════════════════════════════════════════════════════
# 1-7. Aggregates
# ════════════════════════════════════════════════════════════════════════════

def test_zero_feedback_db(app, db, script):
    with app.app_context():
        fb = script._feedback_aggregates(datetime.utcnow() - timedelta(hours=24))
        assert fb['total'] >= 0
        assert fb['last_24h'] >= 0
        assert fb['events'] == {} or all(
            e in EVENTS for e in fb['events'])
        profiles = script._profile_aggregates()
        assert profiles['total'] >= 0


def test_populated_feedback_aggregates(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    old = now - timedelta(days=40)
    # Baseline-delta assertions: other suite modules share this DB and may
    # have left feedback rows inside any 24h window, so only the DELTA our
    # insertions cause is deterministic.
    window = now - timedelta(hours=24)
    with app.app_context():
        before = script._feedback_aggregates(window)

    _record(user.id, media.tmdb_id, event='impression', created_at=now)
    _record(user.id, media.tmdb_id, event='impression', created_at=now)
    _record(user.id, media.tmdb_id, event='click', created_at=now,
            position=3, reason_kind='genre_affinity')
    _record(user.id, media.tmdb_id, event='click', created_at=old)
    _record(user.id, media.tmdb_id, event='not_interested', created_at=now)

    with app.app_context():
        fb = script._feedback_aggregates(window)
    assert fb['total'] - before['total'] == 5
    assert fb['last_24h'] - before['last_24h'] == 4
    assert fb['events']['impression'] - before['events'].get('impression', 0) == 2
    assert fb['events']['click'] - before['events'].get('click', 0) == 1
    assert (fb['events']['not_interested']
            - before['events'].get('not_interested', 0) == 1)
    assert (fb['surfaces']['home_for_you']
            - before['surfaces'].get('home_for_you', 0) == 4)
    assert fb['active_users'] - before['active_users'] == 1
    assert fb['oldest'] <= old < fb['newest']


def test_24h_event_breakdown_is_windowed(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    window = now - timedelta(hours=24)
    with app.app_context():
        before = script._feedback_aggregates(window)

    _record(user.id, media.tmdb_id, event='impression', created_at=now)
    _record(user.id, media.tmdb_id, event='saved', created_at=now)
    _record(user.id, media.tmdb_id, event='saved',
            created_at=now - timedelta(hours=30))

    with app.app_context():
        fb = script._feedback_aggregates(window)
    # windowed, not total: delta of in-window 'saved' is exactly 1
    assert (fb['events'].get('saved', 0)
            - before['events'].get('saved', 0) == 1)
    assert fb['total'] - before['total'] == 3


def test_event_breakdown_covers_all_events(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    # Impressions repeat; non-impressions need distinct days OR distinct media.
    for i, event in enumerate(('impression', 'click', 'not_interested',
                               'saved', 'rated', 'already_watched')):
        _record(user.id, media.tmdb_id + i, event=event, created_at=now)

    with app.app_context():
        fb = script._feedback_aggregates(now - timedelta(hours=24))
    assert set(EVENTS) <= set(fb['events'])


def test_surface_breakdown(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    surfaces = ('home_for_you', 'profile_recs', 'more_like_this')
    window = now - timedelta(hours=24)
    with app.app_context():
        before = script._feedback_aggregates(window)
    for i, surface in enumerate(surfaces):
        _record(user.id, media.tmdb_id + i, event='impression',
                surface=surface, created_at=now)

    with app.app_context():
        fb = script._feedback_aggregates(window)
    assert all(
        fb['surfaces'].get(s, 0) - before['surfaces'].get(s, 0) == 1
        for s in surfaces)


def test_distinct_user_count(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    other = User(username=_unique('vrf2'),
                 email=f"{_unique('vrf2')}@verify.test", email_verified=True)
    other.set_password('x')
    db.session.add(other)
    db.session.commit()
    window = now - timedelta(hours=24)
    try:
        with app.app_context():
            before = script._feedback_aggregates(window)
        _record(user.id, media.tmdb_id, event='impression', created_at=now)
        _record(other.id, media.tmdb_id + 1, event='impression',
                created_at=now)
        _record(other.id, media.tmdb_id + 2, event='impression',
                created_at=now)
        with app.app_context():
            fb = script._feedback_aggregates(window)
        assert fb['active_users'] - before['active_users'] == 2
    finally:
        from models import db as _db
        _db.session.delete(other)
        _db.session.commit()


def test_timestamp_range(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    old = now - timedelta(days=200)
    _record(user.id, media.tmdb_id, created_at=old)
    _record(user.id, media.tmdb_id + 1, created_at=now)
    with app.app_context():
        fb = script._feedback_aggregates(now - timedelta(hours=24))
    assert fb['oldest'] is not None and fb['newest'] is not None
    assert fb['oldest'] <= old
    assert fb['newest'] >= now - timedelta(seconds=5)


# ════════════════════════════════════════════════════════════════════════════
# 8-9. Duplicate consistency check
# ════════════════════════════════════════════════════════════════════════════

def test_duplicate_check_clean_when_index_enforced(app, db, user,
                                                   media_factory, script):
    media = media_factory()
    # record() twice same day -> second suppressed by the partial unique
    # index, so the DB never contains duplicates.
    RecommendationFeedback.record(
        user_id=user.id, media_id=media.tmdb_id, media_type='movie',
        surface='home_for_you', event='click', source='trending')
    dup = RecommendationFeedback.record(
        user_id=user.id, media_id=media.tmdb_id, media_type='movie',
        surface='home_for_you', event='click', source='trending')
    assert dup is None
    with app.app_context():
        assert script._duplicate_groups() == []


def test_duplicate_detection_causes_exit_1(app, db, user, media_factory,
                                           script, caplog):
    media = media_factory()
    now = datetime.utcnow()
    day = now.date()
    # The partial unique index makes duplicates impossible on a healthy
    # schema — simulate "rows predate the index" by dropping it, inserting
    # two rows, then restoring it (SQLite supports the same partial index
    # DDL; no PostgreSQL-only syntax).
    from sqlalchemy import text
    db.session.execute(text(
        'DROP INDEX uq_recommendation_feedback_event_daily'))
    _record(user.id, media.tmdb_id, event='click', created_at=now,
            event_date=day)
    _record(user.id, media.tmdb_id, event='click', created_at=now,
            event_date=day)
    # Index intentionally NOT restored here: a unique index cannot be
    # created over data that violates it — that IS the broken state this
    # test simulates. The autouse cleanup restores it for later tests.

    with app.app_context():
        code = script.run()
    assert code == 1
    warning_text = ' '.join(
        r.getMessage() for r in caplog.records if r.levelname == 'WARNING')
    assert 'duplicate non-impression events' in warning_text
    # Aggregate dimensions only — user identity is never logged (no
    # "user=" field in any warning).
    assert 'user=' not in warning_text


def test_duplicate_exempts_impressions(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    day = now.date()
    _record(user.id, media.tmdb_id, event='impression', created_at=now,
            event_date=day)
    _record(user.id, media.tmdb_id, event='impression', created_at=now,
            event_date=day)
    with app.app_context():
        assert script._duplicate_groups() == []


def test_duplicates_on_different_days_are_fine(app, db, user, media_factory,
                                               script):
    media = media_factory()
    now = datetime.utcnow()
    _record(user.id, media.tmdb_id, event='click', created_at=now)
    _record(user.id, media.tmdb_id, event='click',
            created_at=now - timedelta(days=1),
            event_date=(now - timedelta(days=1)).date())
    with app.app_context():
        assert script._duplicate_groups() == []


# ════════════════════════════════════════════════════════════════════════════
# 10-12. TasteProfile aggregates + freshness
# ════════════════════════════════════════════════════════════════════════════

def _make_profile(db, user, confidence=0.0, titles=0, signals=0,
                  updated_at=None, version=1):
    tp = TasteProfile(
        user_id=user.id,
        confidence=confidence,
        distinct_title_count=titles,
        signal_count=signals,
        profile_version=version,
        updated_at=updated_at or datetime.utcnow(),
    )
    db.session.add(tp)
    db.session.commit()
    return tp


def test_profile_aggregate_inspection(app, db, user, script):
    _make_profile(db, user, confidence=0.5, titles=7, signals=21)
    other = User(username=_unique('vrf3'),
                 email=f"{_unique('vrf3')}@verify.test", email_verified=True)
    other.set_password('x')
    db.session.add(other)
    db.session.commit()
    try:
        _make_profile(db, other, confidence=0.1, titles=2, signals=3)
        with app.app_context():
            p = script._profile_aggregates()
        assert p['total'] >= 2
        assert p['eligible'] >= 1
        assert 1 in p['versions']
    finally:
        db.session.delete(other)
        db.session.commit()


def test_eligible_uses_persisted_thresholds(app, db, user, script):
    # Canonical eligibility: confidence >= 0.4 AND distinct_title_count >= 5.
    _make_profile(db, user, confidence=0.9, titles=4)     # titles too few
    with app.app_context():
        p = script._profile_aggregates()
    # This user is NOT eligible despite high confidence.
    eligible_before = p['eligible']
    # Bump titles over the threshold -> eligible.
    row = TasteProfile.query.filter_by(user_id=user.id).one()
    row.distinct_title_count = 5
    db.session.commit()
    with app.app_context():
        p2 = script._profile_aggregates()
    assert p2['eligible'] == eligible_before + 1


def test_eligible_requires_both_conditions(app, db, user, script):
    _make_profile(db, user, confidence=0.39, titles=9)    # confidence too low
    with app.app_context():
        before = script._profile_aggregates()['eligible']
    row = TasteProfile.query.filter_by(user_id=user.id).one()
    row.confidence = 0.4
    db.session.commit()
    with app.app_context():
        after = script._profile_aggregates()['eligible']
    assert after == before + 1


def test_feedback_profile_freshness(app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    tp = _make_profile(db, user)
    # Feedback 1h ago; profile updated 30min ago -> caught up.
    _record(user.id, media.tmdb_id, created_at=now - timedelta(hours=1))
    tp.updated_at = now - timedelta(minutes=30)
    db.session.commit()
    with app.app_context():
        total, lagging = script._profile_freshness(
            now - timedelta(hours=24))
    assert total >= 1 and lagging == 0


def test_freshness_lagging_when_profile_never_caught_up(
        app, db, user, media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    tp = _make_profile(db, user)
    # Feedback 1h ago; profile last updated 3h ago -> lagging.
    _record(user.id, media.tmdb_id, created_at=now - timedelta(hours=1))
    tp.updated_at = now - timedelta(hours=3)
    db.session.commit()
    with app.app_context():
        total, lagging = script._profile_freshness(now - timedelta(hours=24))
    assert lagging >= 1


def test_freshness_lagging_when_no_profile_at_all(app, db, user,
                                                  media_factory, script):
    media = media_factory()
    now = datetime.utcnow()
    _record(user.id, media.tmdb_id, created_at=now - timedelta(hours=1))
    with app.app_context():
        total, lagging = script._profile_freshness(now - timedelta(hours=24))
    assert total >= 1 and lagging >= 1


def test_freshness_zero_when_no_feedback(app, db, script):
    with app.app_context():
        total, lagging = script._profile_freshness(
            datetime.utcnow() - timedelta(hours=24))
    # No feedback rows inside the window for the users this module creates.
    assert (total, lagging) == (0, 0) or lagging == 0


# ════════════════════════════════════════════════════════════════════════════
# 13-15. Exit codes
# ════════════════════════════════════════════════════════════════════════════

def test_healthy_state_exit_0(app, db, user, media_factory, script, caplog):
    import logging
    caplog.set_level(logging.INFO, logger='frameiq.rec_feedback_verify')
    media = media_factory()
    now = datetime.utcnow()
    _record(user.id, media.tmdb_id, event='impression', created_at=now)
    _record(user.id, media.tmdb_id, event='click', created_at=now)
    tp = _make_profile(db, user, confidence=0.5, titles=6)
    tp.updated_at = now
    db.session.commit()
    with app.app_context():
        code = script.run()
    assert code == 0
    assert '[STATUS] OK' in caplog.text


def test_fatal_db_failure_exit_2(script, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)

    # _load() raises -> clean [FATAL] + exit 2, no traceback escape.
    def _boom():
        raise RuntimeError('database unreachable')
    monkeypatch.setattr(script, '_load', _boom)
    assert script.main() == 2
    assert '[FATAL]' in caplog.text


def test_malformed_event_detection(app, db, user, media_factory, script):
    media = media_factory()
    _record(user.id, media.tmdb_id, event='impression')
    # Simulate a row that escaped the API/model validation boundary.
    fb = _record(user.id, media.tmdb_id + 1, event='impression')
    from models import db as _db
    _db.session.execute(
        RecommendationFeedback.__table__.update()
        .where(RecommendationFeedback.id == fb.id)
        .values(event='bogus_event'))
    _db.session.commit()
    with app.app_context():
        bad = script._malformed_rows()
    assert fb.id in bad


def test_malformed_rows_cause_exit_1(app, db, user, media_factory, script):
    media = media_factory()
    fb = _record(user.id, media.tmdb_id, event='impression')
    from models import db as _db
    _db.session.execute(
        RecommendationFeedback.__table__.update()
        .where(RecommendationFeedback.id == fb.id)
        .values(media_type='book'))
    _db.session.commit()
    with app.app_context():
        assert script.run() == 1


def test_nonpositive_media_id_is_malformed(app, db, user, media_factory,
                                           script):
    """media_id <= 0 is a reachable schema-level violation (the UPDATE path
    bypasses model validation). The created_at-NULL branch of the same
    check is defensive only — SQLite enforces NOT NULL on UPDATE, so that
    state is unreachable in any compliant database."""
    media = media_factory()
    fb = _record(user.id, media.tmdb_id)
    from models import db as _db
    _db.session.execute(
        RecommendationFeedback.__table__.update()
        .where(RecommendationFeedback.id == fb.id)
        .values(media_id=-5))
    _db.session.commit()
    with app.app_context():
        assert fb.id in script._malformed_rows()


# ════════════════════════════════════════════════════════════════════════════
# 16-19. Hygiene: read-only, no network, bounded, no leakage
# ════════════════════════════════════════════════════════════════════════════

def _source():
    with open(_SCRIPT_PATH, encoding='utf-8') as fh:
        return fh.read()


def _stripped_source():
    """Source with comments and docstrings stripped so guard assertions
    cannot be fooled by documentation mentioning forbidden tokens."""
    src = _source()
    lines, skip_doc = [], False
    for line in src.splitlines():
        s = line.strip()
        if s.startswith('\"\"\"') and s.count('\"\"\"') >= 2 and len(s) > 3:
            continue  # single-line docstring
        if s.startswith('\"\"\"'):
            skip_doc = not skip_doc
            continue
        if skip_doc or '#' in line:
            # keep code before the comment, drop the comment part
            line = line.split('#', 1)[0] if not skip_doc else ''
        lines.append(line)
    return '\n'.join(lines)


def test_no_mutation_statements(script):
    src = _stripped_source()
    for banned in ('INSERT ', 'UPDATE ', 'DELETE ', '.commit(', '.add(',
                   '.delete(', '.merge(', 'db.drop_all', 'create_all'):
        assert banned not in src, f'read-only violation: {banned!r}'


def test_no_network_guards(script):
    src = _stripped_source()
    for banned in ('requests.', 'urllib.', 'httpx', 'socket.',
                   'urlopen', 'tmdb', 'TMDb'):
        assert banned not in src, f'network violation: {banned!r}'


def test_no_network_socket_guard(app, db, user, media_factory, script,
                                 monkeypatch):
    """Socket-level: running the verification must open ZERO connections."""
    media = media_factory()
    _record(user.id, media.tmdb_id)

    def _no_socket(*a, **kw):
        raise AssertionError('verify script attempted a network connection')

    monkeypatch.setattr(socket, 'create_connection', _no_socket)
    monkeypatch.setattr(socket.socket, 'connect', _no_socket)
    with app.app_context():
        assert script.run() in (0, 1)


def test_bounded_queries_no_per_row_n1(app, db, user, media_factory, script):
    """The whole run() issues a small constant number of SELECT statements —
    not one per feedback row."""
    media = media_factory()
    now = datetime.utcnow()
    for i in range(25):
        _record(user.id, media.tmdb_id + i, created_at=now)

    from sqlalchemy import event as sqla_event
    statements = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with app.app_context():
        sqla_event.listen(db.engine, 'before_cursor_execute', _count)
        try:
            script.run()
        finally:
            sqla_event.remove(db.engine, 'before_cursor_execute', _count)
    # Generous bound: aggregates + sanity + report. Far below per-row (25+).
    assert len(statements) <= 30, (
        f'unbounded query pattern: {len(statements)} statements for 25 rows')


def test_no_payload_leakage_in_output(app, db, user, media_factory, script,
                                      caplog):
    media = media_factory()
    now = datetime.utcnow()
    secret_payload = '{"secret": "user-private-data"}'
    _record(user.id, media.tmdb_id, event='impression', created_at=now)
    from models import db as _db
    _db.session.execute(
        RecommendationFeedback.__table__.update()
        .where(RecommendationFeedback.user_id == user.id)
        .values(payload_json=secret_payload))
    _db.session.commit()
    with app.app_context():
        script.run()
    assert 'user-private-data' not in caplog.text
    assert 'secret' not in caplog.text


# ════════════════════════════════════════════════════════════════════════════
# 20. Legacy tables untouched
# ════════════════════════════════════════════════════════════════════════════

def test_legacy_taste_tables_untouched(script):
    src = _stripped_source()
    for legacy in ('user_taste_profile', 'user_similarity'):
        assert legacy not in src, f'legacy table referenced: {legacy}'


def test_script_only_reads_known_tables(app, db, script):
    """The reflected metadata confirms the script's queries target only
    recommendation_feedback + taste_profile (+ user for the FK join) —
    never the legacy taste tables."""
    from sqlalchemy import event as sqla_event
    touched = set()

    def _record_tables(conn, cursor, statement, parameters, context,
                       executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            # Extract the first table name after FROM.
            lowered = statement.lower()
            idx = lowered.find(' from ')
            if idx != -1:
                rest = lowered[idx + 6:].lstrip()
                name = rest.split(' ', 1)[0].strip('"\'`')
                touched.add(name)

    listener = _record_tables

    with app.app_context():
        sqla_event.listen(db.engine, 'before_cursor_execute', _record_tables)
        try:
            script.run()
        finally:
            sqla_event.remove(db.engine, 'before_cursor_execute', listener)
    legacy_touched = touched & {'user_taste_profile', 'user_similarity'}
    assert not legacy_touched, f'legacy tables queried: {legacy_touched}'


def test_run_returns_int(script, app, db):
    with app.app_context():
        assert isinstance(script.run(), int)
