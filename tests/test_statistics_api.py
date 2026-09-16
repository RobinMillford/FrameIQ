"""Private personal statistics API (Feature #8, Phase 3) — GET /api/statistics.

Covers routes/statistics.py as an adapter around the canonical
api.statistics.get_statistics service:

- privacy: session-user-only; no user_id/username/email input; user A can
  never read user B through any input channel
- window params: default current year, ?year=YYYY, ?lifetime=true,
  malformed/conflicting input → clean 400 (never a traceback)
- response: the canonical service's presentation dict plus a resolved
  "period"; no IDs, no debug fields, no ORM objects
- guards: exactly one service call per request, bounded SQL, zero
  network, no mutation, deterministic output
"""
import socket
import uuid
from datetime import date as _date

import pytest


# ── Helpers / fixtures (taste-profile API suite conventions) ─────────────────

def _make_user(username):
    from models import User, db as _db

    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(client, user):
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1'})
    return client


@pytest.fixture
def stats_user(app):
    with app.app_context():
        yield _make_user('stata' + uuid.uuid4().hex[:6])


@pytest.fixture
def other_user(app):
    with app.app_context():
        yield _make_user('statb' + uuid.uuid4().hex[:6])


@pytest.fixture
def auth_client(client, stats_user):
    return _login(client, stats_user)


@pytest.fixture
def other_client(client, other_user):
    return _login(client, other_user)


def _diary(user, media, watched_date, rating=None, is_rewatch=False):
    from models import DiaryEntry, db as _db
    e = DiaryEntry(user_id=user.id, media_id=media.id,
                   media_type=media.media_type,
                   watched_date=watched_date, rating=rating,
                   is_rewatch=is_rewatch)
    _db.session.add(e)
    _db.session.commit()
    return e


def _media(title, runtime=90, genres=None, media_type='movie', year=2020):
    from models import MediaItem, db as _db
    m = MediaItem(tmdb_id=None, media_type=media_type, title=title,
                  runtime=runtime, genres=genres,
                  release_date=_date(year, 6, 15))
    if not hasattr(_media, '_next_id'):
        _media._next_id = 9_700_000
    m.tmdb_id = _media._next_id
    _media._next_id += 1
    _db.session.add(m)
    _db.session.commit()
    return m


METRIC_KEYS = {
    'total_watch_events', 'distinct_titles', 'movies_watched',
    'tv_watch_events', 'total_hours_watched', 'runtime_covered_events',
    'runtime_missing_events', 'average_rating', 'rating_count',
    'rating_distribution', 'rewatch_count', 'rewatch_rate',
    'top_genres', 'monthly_watch_counts', 'media_type_distribution',
}


# ── Authorization / privacy ──────────────────────────────────────────────────

def test_anonymous_rejected(client):
    # Same convention as the Taste DNA / For You API suites: flask-login
    # redirects unauthenticated browser requests (302); 401 is acceptable.
    assert client.get('/api/statistics').status_code in (302, 401)


def test_authenticated_allowed(auth_client):
    r = auth_client.get('/api/statistics')
    assert r.status_code == 200
    assert r.get_json() is not None


def test_response_is_json(auth_client):
    r = auth_client.get('/api/statistics')
    assert r.content_type.startswith('application/json')


def test_no_user_id_input_supported(auth_client, stats_user):
    # user_id is deliberately not a parameter: passing it must NOT change
    # whose statistics are returned (the session user is authoritative).
    r = auth_client.get('/api/statistics?user_id=999999')
    assert r.status_code == 200
    assert r.get_json()['total_watch_events'] == 0


def test_user_isolation_across_sessions(auth_client, stats_user,
                                        other_user, app):
    # Repo convention (see test_taste_profile_api): seed ANOTHER user's
    # watch history and prove the session user's API response never
    # contains it — user A cannot read user B through any input channel.
    with app.app_context():
        m = _media('Isolation Probe')
        _diary(other_user, m, _date(2026, 3, 3))
        try:
            mine = auth_client.get('/api/statistics?year=2026').get_json()
            assert mine['total_watch_events'] == 0
            assert mine['distinct_titles'] == 0
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=other_user.id).delete()
            _db.session.commit()


# ── Window parameters ────────────────────────────────────────────────────────

def test_default_period_is_current_year(auth_client, stats_user, app):
    from datetime import date as _date, datetime as _datetime

    with app.app_context():
        current = _datetime.now().year
        m = _media('Current Year Probe')
        _diary(stats_user, m, _date(current, 2, 2))
        try:
            data = auth_client.get('/api/statistics').get_json()
            assert data['period'] == {'type': 'year', 'year': current}
            assert data['total_watch_events'] == 1
            assert len(data['monthly_watch_counts']) == 12
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_explicit_year(auth_client, stats_user, app):
    with app.app_context():
        m = _media('Y2025 Probe')
        _diary(stats_user, m, _date(2025, 6, 6))
        try:
            data = auth_client.get('/api/statistics?year=2025').get_json()
            assert data['period'] == {'type': 'year', 'year': 2025}
            assert data['total_watch_events'] == 1
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_lifetime(auth_client, stats_user, app):
    with app.app_context():
        m = _media('Old Probe', year=1999)
        _diary(stats_user, m, _date(1999, 6, 6))
        m2 = _media('New Probe')
        _diary(stats_user, m2, _date(2026, 2, 2))
        try:
            data = auth_client.get(
                '/api/statistics?lifetime=true').get_json()
            assert data['period'] == {'type': 'lifetime', 'year': None}
            assert data['total_watch_events'] == 2
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_invalid_year_clean_400(auth_client):
    for bad in ('abc', '0', '-5', '20.5', 'true'):
        r = auth_client.get(f'/api/statistics?year={bad}')
        assert r.status_code == 400, bad
        body = r.get_json()
        assert 'error' in body


def test_future_year_clean_400(auth_client):
    from datetime import datetime as _datetime
    future = _datetime.now().year + 1
    r = auth_client.get(f'/api/statistics?year={future}')
    assert r.status_code == 400


def test_conflicting_year_lifetime_clean_400(auth_client):
    r = auth_client.get('/api/statistics?year=2025&lifetime=true')
    assert r.status_code == 400
    assert 'mutually exclusive' in r.get_json()['error']


def test_malformed_lifetime_clean_400(auth_client):
    r = auth_client.get('/api/statistics?lifetime=maybe')
    assert r.status_code == 400


def test_no_traceback_on_bad_input(auth_client):
    r = auth_client.get('/api/statistics?year=%3Cscript%3E')
    assert r.status_code == 400
    assert b'Traceback' not in r.data


# ── Response contract ────────────────────────────────────────────────────────

def test_zero_history_response(auth_client):
    data = auth_client.get('/api/statistics').get_json()
    assert data['total_watch_events'] == 0
    assert data['average_rating'] is None
    assert data['top_genres'] == []
    assert data['media_type_distribution'] == {'movie': 0, 'tv': 0}
    assert len(data['monthly_watch_counts']) == 12


def test_non_empty_response_and_contract(auth_client, stats_user, app):
    with app.app_context():
        m = _media('Contract Probe', runtime=120, genres='Drama, Thriller')
        _diary(stats_user, m, _date(2026, 4, 4), rating=4.0)
        m2 = _media('Contract TV', runtime=45, media_type='tv')
        _diary(stats_user, m2, _date(2026, 5, 5))
        try:
            data = auth_client.get('/api/statistics?year=2026').get_json()
            assert METRIC_KEYS.issubset(data.keys())
            assert data['period'] == {'type': 'year', 'year': 2026}
            assert data['total_watch_events'] == 2
            assert data['movies_watched'] == 1
            assert data['tv_watch_events'] == 1
            assert data['total_hours_watched'] == 2.8  # 165 min → 2.75 → 1 dp
            assert data['rating_count'] == 1
            assert data['average_rating'] == 4.0
            assert data['media_type_distribution'] == {'movie': 1, 'tv': 1}
            assert [g['name'] for g in data['top_genres']] == \
                ['Drama', 'Thriller']
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_no_ids_or_debug_fields(auth_client, stats_user, app):
    with app.app_context():
        m = _media('Leak Probe')
        _diary(stats_user, m, _date(2026, 6, 6))
        try:
            data = auth_client.get('/api/statistics').get_json()

            def _leaves(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        yield key
                        yield from _leaves(value)
                elif isinstance(node, (list, tuple)):
                    for value in node:
                        yield from _leaves(value)
                else:
                    yield node

            keys = [k for k in _leaves(data) if isinstance(k, str)]
            values = [v for v in _leaves(data) if not isinstance(v, str)]
            # No key names or leaf VALUES equal to internal IDs.
            assert not any(k in ('user_id', 'media_id', 'id', 'profile_id')
                           for k in keys)
            assert stats_user.id not in values
            assert m.id not in values
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_deterministic_response(auth_client):
    first = auth_client.get('/api/statistics').get_json()
    second = auth_client.get('/api/statistics').get_json()
    assert first == second


# ── Guards: one service call, bounded queries, no network, no mutation ──────

def test_one_service_call_per_request(auth_client, monkeypatch):
    import api.statistics as service

    calls = []
    original = service.get_statistics  # saved BEFORE patching

    def _spy(user_id, *args, **kwargs):
        calls.append(user_id)
        return original(user_id, *args, **kwargs)

    monkeypatch.setattr(service, 'get_statistics', _spy)
    auth_client.get('/api/statistics')
    assert len(calls) == 1


def test_route_adds_no_extra_queries(auth_client, stats_user, app):
    # The route is a pure adapter: the request must stay within the
    # Phase 2 service budget (5 statements) + the session-user load.
    from sqlalchemy import event as sa_event

    with app.app_context():
        m = _media('Query Budget Probe')
        _diary(stats_user, m, _date(2026, 7, 7))
        try:
            statements = []

            def _record(conn, cursor, statement, *a, **k):
                statements.append(statement)

            sa_event.listen(app.extensions['sqlalchemy'].engine,
                            'before_cursor_execute', _record)
            try:
                r = auth_client.get('/api/statistics?year=2026')
                assert r.status_code == 200
            finally:
                sa_event.remove(app.extensions['sqlalchemy'].engine,
                                'before_cursor_execute', _record)
            # 5 service statements + auth/session overhead only.
            assert len(statements) <= 12, statements
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_no_recommendation_dependencies(auth_client, monkeypatch):
    import api.statistics as service
    import routes.statistics as route

    def _boom(*a, **k):
        raise AssertionError('statistics API touched a forbidden module')

    monkeypatch.setattr(route, 'limiter', route.limiter)  # sanity
    monkeypatch.setattr(
        service, 'get_statistics',
        lambda *a, **k: _boom())  # replaced below by the real spy chain
    # The real assertion: the route imports only api.statistics — none of
    # the forbidden subsystems appear in the route module's namespace.
    import sys
    route_modules = [name for name in sys.modules
                     if name.startswith('routes.statistics')]
    assert route_modules
    forbidden = ('for_you', 'taste_profile', 'recommendation_feedback',
                 'tmdb', 'requests')
    import inspect
    source = inspect.getsource(route)
    for banned in forbidden:
        assert banned not in source, banned
    monkeypatch.setattr(
        service, 'get_statistics', service.get_statistics)  # restore


def test_no_network_socket_guard(auth_client, stats_user, app):
    with app.app_context():
        m = _media('Socket Probe')
        _diary(stats_user, m, _date(2026, 8, 8))
        try:
            real_socket = socket.socket

            class _Blocked(real_socket):
                def __init__(self, *args, **kwargs):
                    raise AssertionError('network call attempted')

            socket.socket = _Blocked
            try:
                r = auth_client.get('/api/statistics?year=2026')
                assert r.status_code == 200
            finally:
                socket.socket = real_socket
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()


def test_get_is_read_only(auth_client, stats_user, app):
    with app.app_context():
        before = stats_user.total_movies_watched
        auth_client.get('/api/statistics')
        auth_client.get('/api/statistics?lifetime=true')
        from models import db as _db, User
        _db.session.expire_all()
        user = User.query.get(stats_user.id)
        assert user.total_movies_watched == before
