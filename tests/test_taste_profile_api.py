"""Private Taste DNA API (Feature #6, Phase 14) — GET /api/taste-profile.

Covers the presentation-model contract of routes/taste_profile.py +
api.taste_profile.taste_dna():

- privacy: session-user-only, no cross-user access, no internal fields
- presentation: bounded strength labels, no raw weights/negatives/IDs
- confidence levels from canonical thresholds
- cold start (missing and evidence-free profiles) is 200, never 404
- guards: one profile load, no recomputation, no network, no mutation
"""
import socket
import uuid

import pytest


# ── Helpers / fixtures ────────────────────────────────────────────────────────

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


def _uid(user):
    return user.id if not callable(getattr(user, 'id', None)) else user.id


@pytest.fixture
def dna_user(app):
    with app.app_context():
        yield _make_user('dna' + uuid.uuid4().hex[:6])


@pytest.fixture
def auth_client(client, dna_user):
    return _login(client, dna_user)


@pytest.fixture
def second_dna_user(app):
    with app.app_context():
        yield _make_user('dnab' + uuid.uuid4().hex[:6])


def _add_profile(uid, *, genres=None, decades=None, directors=None,
                 runtime=None, media_pref=None, confidence=0.7,
                 titles=10, signals=20):
    from models import TasteProfile, db as _db

    p = TasteProfile(user_id=uid)
    p.genre_weights = genres or {}
    p.decade_weights = decades or {}
    p.director_affinity = directors or {}
    p.runtime_pref = runtime or {}
    p.media_type_pref = media_pref or {}
    p.confidence = confidence
    p.distinct_title_count = titles
    p.signal_count = signals
    _db.session.add(p)
    _db.session.commit()
    return p


def _get_json(auth_client):
    r = auth_client.get('/api/taste-profile')
    assert r.status_code == 200
    return r.get_json()


# ── Security (spec §23) ───────────────────────────────────────────────────────

def test_anonymous_rejected(client):
    # Same convention as the For You API suite: flask-login redirects
    # unauthenticated browser requests (302); 401 would also be acceptable.
    assert client.get('/api/taste-profile').status_code in (302, 401)


def test_authenticated_allowed(auth_client):
    assert _get_json(auth_client) is not None


def test_user_id_query_param_cannot_select_another_user(
        auth_client, dna_user, second_dna_user):
    _add_profile(second_dna_user.id, genres={"Thriller": 0.9})
    r = auth_client.get('/api/taste-profile?user_id=%d' % second_dna_user.id)
    assert r.status_code == 200
    body = r.get_json()
    # The session user has no profile → cold start, never the other user's.
    assert body.get('available') is False


def test_username_query_param_ignored(auth_client):
    r = auth_client.get('/api/taste-profile?username=someoneelse')
    assert r.status_code == 200
    assert r.get_json().get('available') is False


def test_profile_id_query_param_ignored(auth_client):
    r = auth_client.get('/api/taste-profile?profile_id=42')
    assert r.status_code == 200
    assert r.get_json().get('available') is False


def test_no_internal_ids_returned(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, directors={
        "Denis Villeneuve": 0.8})
    body = _get_json(auth_client)
    text = str(body)
    assert 'user_id' not in text
    assert str(dna_user.id) not in text
    assert 'profile_version' not in text
    assert 'tmdb' not in text.lower()
    for director in body.get('top_directors', []):
        assert set(director.keys()) == {'name', 'strength'}


def test_no_raw_weights(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9123, "Drama": 0.4},
                 media_pref={"movie": 0.81, "tv": 0.19})
    body = _get_json(auth_client)
    text = str(body)
    assert '0.9123' not in text
    assert 'genre_weights' not in text
    assert 'director_affinity' not in text
    assert 'signal_count' not in text


def test_no_negative_numbers(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9, "Horror": -0.7})
    body = _get_json(auth_client)
    assert '[-' not in str(body)
    for item in body['avoid_genres']:
        assert item['strength'] in ('high', 'moderate', 'low')


def test_no_profile_version(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    assert 'profile_version' not in str(_get_json(auth_client))


def test_no_feedback_rows(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    text = str(_get_json(auth_client))
    assert 'feedback' not in text.lower()
    assert 'impression' not in text.lower()


def test_no_raw_json_blobs(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    body = _get_json(auth_client)
    for key in ('genre_weights_json', 'decade_weights_json',
                'director_affinity_json', 'runtime_pref_json',
                'media_type_pref_json', 'mood_tags_json'):
        assert key not in body


def test_route_source_has_no_user_id_param():
    """The route must never read identity from the query string."""
    with open('routes/taste_profile.py', encoding='utf-8') as fh:
        src = fh.read()
    assert 'request.args' not in src
    assert 'request.values' not in src


# ── Cold start / levels (spec §5, §6) ─────────────────────────────────────────

def test_missing_profile_cold_start_not_404(auth_client):
    r = auth_client.get('/api/taste-profile')
    assert r.status_code == 200
    body = r.get_json()
    assert body == {'available': False, 'state': 'cold_start'}


def test_evidence_free_profile_cold_start(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Horror": -0.5}, confidence=0.0,
                 titles=0, signals=0)
    body = _get_json(auth_client)
    assert body == {'available': False, 'state': 'cold_start'}


def test_limited_level(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, confidence=0.1,
                 titles=2)
    assert _get_json(auth_client)['level'] == 'limited'


def test_developing_level(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, confidence=0.3,
                 titles=4)
    assert _get_json(auth_client)['level'] == 'developing'


def test_strong_level_canonical_threshold(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, confidence=0.4,
                 titles=5)
    assert _get_json(auth_client)['level'] == 'strong'


def test_just_below_strong(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, confidence=0.39,
                 titles=5)
    assert _get_json(auth_client)['level'] == 'developing'


def test_titles_below_five_not_strong(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, confidence=0.9,
                 titles=4)
    assert _get_json(auth_client)['level'] != 'strong'


# ── Presentation sections (spec §7–§12) ──────────────────────────────────────

def test_positive_genres(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9, "Science Fiction": 0.7,
                                      "Drama": 0.35})
    body = _get_json(auth_client)
    names = [g['name'] for g in body['top_genres']]
    assert names == ['Thriller', 'Science Fiction', 'Drama']
    strengths = [g['strength'] for g in body['top_genres']]
    assert strengths == ['high', 'high', 'moderate']


def test_negative_genres_calibrated(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9, "Horror": -0.7})
    body = _get_json(auth_client)
    avoid = body['avoid_genres']
    assert avoid == [{'name': 'Horror', 'strength': 'high'}]
    # No absolute wording anywhere in the payload.
    assert 'hate' not in str(body).lower()
    assert 'never' not in str(body).lower()


def test_directors(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9},
                 directors={"Denis Villeneuve": 0.8, "Greta Gerwig": 0.35})
    body = _get_json(auth_client)
    assert body['top_directors'] == [
        {'name': 'Denis Villeneuve', 'strength': 'high'},
        {'name': 'Greta Gerwig', 'strength': 'moderate'},
    ]


def test_empty_directors_omitted(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    assert 'top_directors' not in _get_json(auth_client)


def test_decades(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9},
                 decades={"2010s": 0.8, "1990s": 0.3})
    assert _get_json(auth_client)['eras'] == [
        {'name': '2010s', 'strength': 'high'},
        {'name': '1990s', 'strength': 'moderate'},
    ]


def test_runtime(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9},
                 runtime={"p25": 105.4, "p75": 145.6, "sample_count": 9})
    assert _get_json(auth_client)['runtime'] == {'min': 105, 'max': 146}


def test_missing_runtime_omitted(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    assert 'runtime' not in _get_json(auth_client)


def test_movie_preference(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9},
                 media_pref={"movie": 0.85, "tv": 0.15})
    assert _get_json(auth_client)['media_preference'] == {
        'movie': 'high', 'tv': 'low'}


def test_tv_preference(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9},
                 media_pref={"movie": 0.2, "tv": 0.8})
    assert _get_json(auth_client)['media_preference'] == {
        'tv': 'high', 'movie': 'low'}


def test_balanced_preference(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9},
                 media_pref={"movie": 0.5, "tv": 0.5})
    assert _get_json(auth_client)['media_preference'] == {
        'movie': 'moderate', 'tv': 'moderate'}


def test_title_count(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, titles=42)
    assert _get_json(auth_client)['titles_analyzed'] == 42


def test_signal_count_not_exposed(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9}, signals=99)
    assert 'signal_count' not in str(_get_json(auth_client))


# ── Bounds + determinism (spec §18, §20) ─────────────────────────────────────

def test_field_bounds(auth_client, dna_user):
    many_genres = {f'Genre{i}': 0.9 - i * 0.01 for i in range(12)}
    many_decades = {f'{1980 + i * 10}s': 0.8 for i in range(7)}
    many_dirs = {f'Director {i}': 0.7 for i in range(9)}
    _add_profile(dna_user.id, genres=many_genres, decades=many_decades,
                 directors=many_dirs)
    body = _get_json(auth_client)
    assert len(body['top_genres']) <= 8
    assert len(body['eras']) <= 5
    assert len(body['top_directors']) <= 5


def test_avoid_genres_bounded(auth_client, dna_user):
    genres = {f'Neg{i}': -0.5 - i * 0.01 for i in range(8)}
    genres['Thriller'] = 0.9
    _add_profile(dna_user.id, genres=genres)
    assert len(_get_json(auth_client)['avoid_genres']) <= 5


def test_deterministic_response(auth_client, dna_user):
    _add_profile(dna_user.id, genres={"Thriller": 0.9, "Drama": 0.4},
                 directors={"Villeneuve": 0.8}, decades={"2010s": 0.8})
    first = _get_json(auth_client)
    second = _get_json(auth_client)
    assert first == second


# ── Guards (spec §21, §24) ────────────────────────────────────────────────────

def test_exactly_one_profile_load(auth_client, dna_user, monkeypatch):
    import api.taste_profile as tp

    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    calls = []
    real = tp.get_profile

    def counting(uid, create=False):
        calls.append(uid)
        return real(uid, create=create)

    monkeypatch.setattr(tp, 'get_profile', counting)
    auth_client.get('/api/taste-profile')
    assert calls == [dna_user.id]


def test_no_compute_profile(auth_client, dna_user, monkeypatch):
    import api.taste_profile as tp

    def boom(*a, **k):
        raise AssertionError("recomputation attempted on GET")

    monkeypatch.setattr(tp, 'compute_profile', boom)
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    auth_client.get('/api/taste-profile')
    # Missing-profile path must not recompute either.
    from models import TasteProfile, db as _db
    TasteProfile.query.filter_by(user_id=dna_user.id).delete()
    _db.session.commit()
    r = auth_client.get('/api/taste-profile')
    assert r.get_json() == {'available': False, 'state': 'cold_start'}


def test_no_recommendation_feedback_query(auth_client, dna_user,
                                          monkeypatch):
    from models import RecommendationFeedback

    def boom(*a, **k):
        raise AssertionError("feedback queried by the taste API")

    monkeypatch.setattr(RecommendationFeedback, 'query', property(
        lambda self: boom()))
    _add_profile(dna_user.id, genres={"Thriller": 0.9})
    auth_client.get('/api/taste-profile')


def test_no_network(app, dna_user, auth_client, monkeypatch):
    class _Blocked(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, 'socket', _Blocked)
    monkeypatch.setattr(socket, 'create_connection',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("network access attempted")))
    with app.app_context():
        _add_profile(dna_user.id, genres={"Thriller": 0.9}, directors={
            "Villeneuve": 0.8})
    auth_client.get('/api/taste-profile')


def _profile_columns(uid):
    from models import TasteProfile

    p = TasteProfile.query.filter_by(user_id=uid).first()
    return (p.genre_weights_json, p.decade_weights_json,
            p.director_affinity_json, p.runtime_pref_json,
            p.media_type_pref_json, p.confidence, p.signal_count,
            p.distinct_title_count, p.profile_version)


def test_response_does_not_mutate_profile(app, auth_client, dna_user):
    with app.app_context():
        _add_profile(dna_user.id, genres={"Thriller": 0.9})
        before = _profile_columns(dna_user.id)
    auth_client.get('/api/taste-profile')
    with app.app_context():
        assert _profile_columns(dna_user.id) == before


def test_no_profile_creation_on_get(app, auth_client, dna_user):
    auth_client.get('/api/taste-profile')
    with app.app_context():
        from models import TasteProfile
        assert TasteProfile.query.filter_by(
            user_id=dna_user.id).first() is None


def test_no_negative_weights_in_any_response(auth_client, dna_user):
    """taste_dna() never emits a raw negative float anywhere."""
    _add_profile(dna_user.id, genres={"Thriller": 0.9, "Horror": -0.99},
                 directors={"Villeneuve": 0.8, "Bad Dir": -0.6},
                 decades={"2020s": 0.5, "1970s": -0.4})
    body = _get_json(auth_client)
    assert '-' not in str(body.get('avoid_genres'))
    assert body.get('top_directors') == [
        {'name': 'Villeneuve', 'strength': 'high'}]  # negative dropped
    assert body.get('eras') == [{'name': '2020s', 'strength': 'moderate'}]
