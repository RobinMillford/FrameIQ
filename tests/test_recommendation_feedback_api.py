"""Recommendation Feedback API (Feature #7, Phase 2) — focused regression.

Covers: auth gating, single/batch request shapes, strict validation
(media_id / media_type / surface / event / position / source / reason_kind /
model_version / payload bounds), idempotent duplicate semantics via the
model's partial unique index, user isolation, CSRF/auth wiring, limiter
registration (the route IS limited — no 500s on storage errors), and
hygiene (no TMDb/network, no TasteProfile computation, counts-only
responses, no partial writes on invalid batches).
"""
import json
from datetime import datetime

import pytest

from models import db, User, MediaItem, RecommendationFeedback, DiaryEntry


# ── module-unique data — every row this file creates is removed after each
#    test (suite convention: each module cleans up what it creates, and the
#    shared session DB reuses freed PKs, so leftovers poison later files). ──
DOMAIN = 'recfb-api.test'


@pytest.fixture(autouse=True)
def _clean_feedback_rows(app, sample_user):
    """Remove this module's feedback + media + users before sample_user's
    own teardown (ordering via dependency)."""
    yield
    # DiaryEntry rows referencing MediaItems must go first: a leftover
    # diary row whose media was already freed turns a later ORM
    # MediaItem.delete() into "SET media_id=NULL" → IntegrityError
    # (shared session DB reuses freed rowids — same purge contract as
    # test_watch.py / test_continue_watching.py).
    DiaryEntry.query.delete()
    RecommendationFeedback.query.delete()
    MediaItem.query.delete()
    db.session.commit()
    User.query.filter(User.email.like(f'%@{DOMAIN}')).delete(
        synchronize_session=False)
    db.session.commit()


def _make_user(username):
    u = User(username=username, email=f'{username}@{DOMAIN}',
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def user(app):
    # Context stays open across the fixture's life (conftest.sample_user
    # convention) so the returned User stays session-bound.
    with app.app_context():
        yield _make_user('recfb')


@pytest.fixture
def second_user(app):
    with app.app_context():
        yield _make_user('recfb2')


def _login(client, user):
    # No follow_redirects: the session cookie is set on the POST response
    # itself, and the redirect target is the homepage (whose TMDb rails are
    # irrelevant here and make each test pay several slow network calls).
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1'})
    return client


@pytest.fixture
def auth_client(client, user):
    return _login(client, user)


def _post(client, payload, **kw):
    return client.post('/api/rec/feedback', json=payload, **kw)


VALID_EVENT = {
    'media_id': 550,
    'media_type': 'movie',
    'surface': 'home_for_you',
    'source': 'genre_discover',
    'event': 'click',
    'position': 3,
    'reason_kind': 'genre_affinity',
}


def _all_feedback():
    return RecommendationFeedback.query.all()


# ── Auth ─────────────────────────────────────────────────────────────────────

def test_unauthenticated_request_rejected(client):
    resp = client.post('/api/rec/feedback', json=VALID_EVENT)
    assert resp.status_code in (302, 401)


def test_authenticated_single_event_succeeds(auth_client):
    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'recorded': 1, 'duplicates': 0}
    rows = _all_feedback()
    assert len(rows) == 1
    assert rows[0].media_id == 550
    assert rows[0].source == 'genre_discover'


# ── Request shapes ───────────────────────────────────────────────────────────

def test_authenticated_batch_succeeds(auth_client):
    resp = _post(auth_client, {'events': [
        {**VALID_EVENT, 'event': 'impression', 'position': 1},
        {'media_id': 603, 'media_type': 'movie', 'surface': 'home_for_you',
         'source': 'similar_to:550', 'event': 'click', 'position': 2,
         'reason_kind': 'similar_title'},
    ]})
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'recorded': 2, 'duplicates': 0}


def test_single_event_shortcut_normalized(auth_client):
    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 200
    rows = _all_feedback()
    assert len(rows) == 1
    assert rows[0].user_id is not None


# ── Validation ───────────────────────────────────────────────────────────────

def test_missing_media_id_rejected(auth_client):
    payload = {k: v for k, v in VALID_EVENT.items() if k != 'media_id'}
    assert _post(auth_client, payload).status_code == 400


@pytest.mark.parametrize('bad', [0, -1, '550', 5.0, True, None])
def test_invalid_media_id_rejected(auth_client, bad):
    assert _post(auth_client, {**VALID_EVENT, 'media_id': bad}) \
        .status_code == 400


def test_invalid_media_type_rejected(auth_client):
    resp = _post(auth_client, {**VALID_EVENT, 'media_type': 'book'})
    assert resp.status_code == 400
    assert 'media_type' in resp.get_json()['error']


def test_invalid_surface_rejected(auth_client):
    resp = _post(auth_client, {**VALID_EVENT, 'surface': 'discover_page'})
    assert resp.status_code == 400
    assert 'surface' in resp.get_json()['error']


def test_invalid_event_rejected(auth_client):
    resp = _post(auth_client, {**VALID_EVENT, 'event': 'hover'})
    assert resp.status_code == 400
    assert 'event' in resp.get_json()['error']


def test_malformed_json_rejected(auth_client):
    resp = auth_client.post(
        '/api/rec/feedback', data='{not json',
        content_type='application/json')
    assert resp.status_code == 400


def test_non_object_json_rejected(auth_client):
    resp = _post(auth_client, [VALID_EVENT])
    assert resp.status_code == 400


def test_oversized_batch_rejected(auth_client):
    events = [{**VALID_EVENT, 'event': 'impression', 'media_id': 1000 + i}
              for i in range(101)]
    resp = _post(auth_client, {'events': events})
    assert resp.status_code == 400
    assert not _all_feedback()  # nothing written


def test_oversized_payload_rejected(auth_client):
    resp = _post(auth_client, {
        **VALID_EVENT, 'payload': {'blob': 'x' * 3000}})
    assert resp.status_code == 400
    assert not _all_feedback()


def test_invalid_payload_type_rejected(auth_client):
    resp = _post(auth_client, {**VALID_EVENT, 'payload': ['not', 'an object']})
    assert resp.status_code == 400


@pytest.mark.parametrize('bad', ['3', 3.5, True, -1, 10001])
def test_invalid_position_rejected(auth_client, bad):
    assert _post(auth_client, {**VALID_EVENT, 'position': bad}) \
        .status_code == 400


def test_position_bounds_accepted(auth_client):
    for pos in (0, 24, 10000):
        resp = _post(auth_client, {
            **VALID_EVENT, 'event': 'impression', 'media_id': 700 + pos,
            'position': pos})
        assert resp.status_code == 200


def test_source_bounds(auth_client):
    assert _post(auth_client, {
        **VALID_EVENT, 'source': 'x' * 121}).status_code == 400
    assert _post(auth_client, {
        **VALID_EVENT, 'source': 'x' * 120}).status_code == 200


def test_reason_kind_bounds(auth_client):
    assert _post(auth_client, {
        **VALID_EVENT, 'reason_kind': 'x' * 65}).status_code == 400
    assert _post(auth_client, {
        **VALID_EVENT, 'reason_kind': 'x' * 64}).status_code == 200


def test_model_version_bounds_and_fallback(auth_client):
    # Omitted → model's V1 default (1).
    _post(auth_client, VALID_EVENT)
    row = _all_feedback()[0]
    assert row.model_version == 1

    # model_version is stored as an Integer (Phase 1 model): numeric strings
    # are accepted and normalized; non-numeric strings are rejected.
    assert _post(auth_client, {
        **VALID_EVENT, 'model_version': 'x' * 65}).status_code == 400
    assert _post(auth_client, {
        **VALID_EVENT, 'model_version': 'taste-v1'}).status_code == 400
    resp = _post(auth_client, {
        **VALID_EVENT, 'event': 'impression', 'model_version': '2'})
    assert resp.status_code == 200
    assert RecommendationFeedback.query.filter_by(
        event='impression').first().model_version == 2


# ── Duplicate / idempotency semantics ────────────────────────────────────────

def test_duplicate_non_impression_idempotent(auth_client):
    assert _post(auth_client, VALID_EVENT).status_code == 200
    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'recorded': 0, 'duplicates': 1}
    assert len(_all_feedback()) == 1


def test_repeated_impression_accepted(auth_client):
    resp = None
    for _ in range(3):
        resp = _post(auth_client, {**VALID_EVENT, 'event': 'impression'})
        assert resp.status_code == 200
    assert resp.get_json()['recorded'] == 1
    assert len(_all_feedback()) == 3


def test_different_day_event_accepted(auth_client, monkeypatch):
    _post(auth_client, VALID_EVENT)
    # Rewind the model's clock so the once-per-day rule sees a new day.
    real_dt = datetime
    monkeypatch.setattr(
        'models.recommendation_feedback.datetime',
        type('FakeDT', (), {
            'utcnow': staticmethod(lambda: real_dt(2000, 1, 2)),
            '__getattr__': staticmethod(lambda name: getattr(real_dt, name)),
        }))
    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 200
    assert resp.get_json()['recorded'] == 1
    assert len(_all_feedback()) == 2


def test_different_surface_accepted(auth_client):
    resp = None
    for surface in ('home_for_you', 'profile_recs', 'more_like_this'):
        resp = _post(auth_client, {**VALID_EVENT, 'surface': surface})
        assert resp.status_code == 200
    assert resp.get_json()['recorded'] == 1
    assert len(_all_feedback()) == 3


def test_different_media_type_accepted(auth_client):
    for mt in ('movie', 'tv'):
        resp = _post(auth_client, {**VALID_EVENT, 'media_type': mt})
        assert resp.status_code == 200
    assert len(_all_feedback()) == 2


# ── Isolation ────────────────────────────────────────────────────────────────

def test_different_user_isolation(auth_client, app, second_user):
    assert _post(auth_client, VALID_EVENT).status_code == 200
    client2 = _login(app.test_client(), second_user)
    resp = _post(client2, VALID_EVENT)
    assert resp.status_code == 200
    assert resp.get_json()['recorded'] == 1  # not a duplicate for user 2
    assert len(_all_feedback()) == 2


def test_client_cannot_override_user_id(auth_client, second_user):
    resp = _post(auth_client, {**VALID_EVENT, 'user_id': second_user.id})
    assert resp.status_code == 400  # unknown field
    assert len(_all_feedback()) == 0


# ── CSRF / limiter wiring ────────────────────────────────────────────────────

@pytest.fixture
def csrf_app(app):
    app.config['WTF_CSRF_ENABLED'] = True
    yield app
    app.config['WTF_CSRF_ENABLED'] = False


def test_csrf_enforcement(csrf_app, user):
    client = csrf_app.test_client()
    # Render /login FIRST (it calls csrf_token(): the raw token goes into
    # this client's session, the signed token into the HTML), then log in —
    # the login form POST itself needs the token, like any real browser.
    html = client.get('/login').get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    assert marker in html, 'login page must render a CSRF token'
    token = html.split(marker, 1)[1].split('"', 1)[0]
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1',
        'csrf_token': token})

    # No header → CSRF failure → 400.
    assert _post(client, VALID_EVENT).status_code == 400
    # With the session's real signed token in the header → accepted.
    resp = client.post(
        '/api/rec/feedback', data=json.dumps(VALID_EVENT),
        content_type='application/json',
        headers={'X-CSRFToken': token})
    assert resp.status_code == 200


def test_route_is_registered_with_limiter(app):
    """The route IS limited: Flask-Limiter knows a decorated limit for
    feedback's endpoint (proves the decorator is actually attached)."""
    limiter = app.extensions['limiter'] if 'limiter' in app.extensions \
        else __import__('extensions').limiter
    endpoint = 'recommendation_feedback.api_record_feedback'
    names = [name for name in limiter.limit_manager._decorated_limits
             if endpoint in name]
    assert names, 'no Flask-Limiter limit registered for /api/rec/feedback'


def test_limiter_storage_failure_does_not_500(auth_client, monkeypatch):
    """Shared-storage error → native in-memory fallback keeps serving;
    the endpoint must never 500 purely because limiter storage failed."""
    from limits.storage.memory import MemoryStorage
    import extensions as extensions_mod

    class _Boom:
        def __getattr__(self, name):
            raise ConnectionError('valkey down')

    limiter = extensions_mod.limiter  # the app-wide singleton
    fallback = MemoryStorage()
    monkeypatch.setattr(limiter, '_storage', _Boom())
    monkeypatch.setattr(limiter, '_storage_dead', True)
    monkeypatch.setattr(limiter, '_in_memory_fallback', fallback)
    monkeypatch.setattr(limiter, '_in_memory_fallback_enabled', True)

    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 200


# ── Hygiene ──────────────────────────────────────────────────────────────────

def test_tmdb_only_media_id_needs_no_mediaitem(auth_client):
    """TMDb identity: feedback persists for ids with no local MediaItem row,
    and no MediaItem is created."""
    assert MediaItem.query.filter_by(tmdb_id=999999).first() is None
    resp = _post(auth_client, {**VALID_EVENT, 'media_id': 999999})
    assert resp.status_code == 200
    assert resp.get_json()['recorded'] == 1
    assert MediaItem.query.filter_by(tmdb_id=999999).first() is None


def test_no_external_network_calls(app, monkeypatch):
    """Any socket creation during the login POST + feedback write cycle is
    a test failure. (Login is followed only to its immediate response —
    following its redirect would hit the homepage's TMDb rails, which is
    out of scope here.)"""
    import socket

    def _no_socket(*a, **kw):
        raise AssertionError('network call attempted during feedback write')

    monkeypatch.setattr(socket, 'create_connection', _no_socket)
    monkeypatch.setattr(socket.socket, 'connect', _no_socket)

    _make_user('recfbnet')
    client = app.test_client()
    client.post('/login', data={'username': 'recfbnet',
                                'password': 'TestPass1'})
    resp = client.post('/api/rec/feedback', json=VALID_EVENT)
    assert resp.status_code == 200


def test_no_taste_profile_computation(auth_client, monkeypatch):
    """Recording feedback must not trigger TasteProfile work."""
    import api.taste_profile as tp

    def _boom(*a, **kw):
        raise AssertionError('TasteProfile computation attempted')

    monkeypatch.setattr(tp, 'compute_profile', _boom)
    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 200


def test_response_contains_only_counts(auth_client):
    resp = _post(auth_client, VALID_EVENT)
    assert set(resp.get_json()) == {'ok', 'recorded', 'duplicates'}


def test_batch_with_invalid_event_rejected_without_partial_writes(
        auth_client):
    resp = _post(auth_client, {'events': [
        {**VALID_EVENT, 'event': 'impression', 'media_id': 11},
        {**VALID_EVENT, 'event': 'not-an-event', 'media_id': 12},
    ]})
    assert resp.status_code == 400
    assert not _all_feedback()  # event 1 was valid but nothing persisted


def test_db_failure_returns_server_error_without_leaking_internals(
        auth_client, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError('SECRET-INTERNAL-DETAILS: db exploded')

    monkeypatch.setattr(RecommendationFeedback, 'record', _boom)
    resp = _post(auth_client, VALID_EVENT)
    assert resp.status_code == 500
    body = resp.get_json()
    assert body['error'] == 'Internal server error'
    assert 'SECRET-INTERNAL-DETAILS' not in json.dumps(body)


def test_endpoint_registered_exactly_once(app):
    routes = [r for r in app.url_map.iter_rules()
              if r.rule == '/api/rec/feedback']
    assert len(routes) == 1
