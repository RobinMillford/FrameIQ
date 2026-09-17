"""Year-in-Review share layer (Feature #8, Phase 9) — focused suite.

Covers the FIRST intentionally public statistics surface, on top of the
canonical recap stack (tests/test_year_in_review.py,
tests/test_year_in_review_experience.py):

Model (models.year_in_review_share.YearInReviewShare):
- minimal authorization row only; raw token never persisted
- one ACTIVE share per (user, year); regeneration replaces the token

Transformation (api.year_in_review_share):
- pure: canonical recap → approved public model; private fields omitted;
  empty recap rejected; deterministic; no IDs anywhere

Private API (POST/DELETE /api/year-in-review/share):
- authenticated, session-scoped, CSRF-protected; strict integer year;
  builder called exactly once (never on invalid input); empty year →
  400; owner-only revoke; regeneration reissues the token

Public route (GET /share/year-in-review/<token>):
- no auth; token is the sole authorization; generic 404 for unknown/
  malformed/revoked/inactive-owner; token-year binding (no ?year);
  no-store + noindex; one builder call → one statistics call; no IDs in
  HTML; deterministic content

Frontend (static/js/yir-share.js):
- no share request on load; one request per click; guarded duplicate
  clicks; clipboard fallback; accessible status; safe DOM only
"""
import json
import re
import socket
import uuid
from datetime import date, datetime
from unittest.mock import patch

import pytest


# ── Fixtures (Phase 8 suite conventions) ─────────────────────────────────────

def _make_user(username, active=True):
    from models import User, db as _db
    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    if not active and hasattr(u, 'is_active'):
        u.is_active = False
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def user(app):
    with app.app_context():
        yield _make_user('p9a' + uuid.uuid4().hex[:6])


@pytest.fixture
def other_user(app):
    with app.app_context():
        yield _make_user('p9b' + uuid.uuid4().hex[:6])


def _media(title, runtime=90, genres=None, media_type='movie'):
    from models import MediaItem, db as _db
    m = MediaItem(tmdb_id=9_900_000 + uuid.uuid4().int % 900_000,
                  media_type=media_type, title=title, runtime=runtime,
                  genres=genres)
    _db.session.add(m)
    _db.session.commit()
    return m


def _diary(user, media, watched_date, rating=None, is_rewatch=False):
    from models import DiaryEntry, db as _db
    e = DiaryEntry(user_id=user.id, media_id=media.id,
                   media_type=media.media_type,
                   watched_date=watched_date, rating=rating,
                   is_rewatch=is_rewatch)
    _db.session.add(e)
    _db.session.commit()
    return e


def _director(name, tmdb_person_id):
    from models import Director
    d = Director(tmdb_person_id=tmdb_person_id, name=name)
    from models import db as _db
    _db.session.add(d)
    _db.session.commit()
    return d


def _attach_director(media, director):
    from models import MediaDirector
    from models import db as _db
    link = MediaDirector(media_item_id=media.id, director_id=director.id)
    _db.session.add(link)
    _db.session.commit()
    return link


def _episode(user, show, season, episode, watched, rating=None):
    from models import TVEpisodeWatch, db as _db
    row = TVEpisodeWatch(user_id=user.id, show_id=show.tmdb_id,
                         season_number=season, episode_number=episode,
                         watched_date=watched, rating=rating)
    _db.session.add(row)
    _db.session.commit()
    return row


def _create_share_record(app, user, year=2026):
    """Create a share authorization row directly (no login) so tests
    that exercise the PUBLIC route do so from a genuinely anonymous
    client — pytest-flask shares the login-user cache across clients
    created in the same test, so viewer tests must never log in."""
    import api.year_in_review_share as svc
    from models import YearInReviewShare, db as _db
    with app.app_context():
        token = svc.generate_share_token()
        share = YearInReviewShare(
            user_id=user.id, year=year,
            token_hash=svc.hash_share_token(token))
        _db.session.add(share)
        _db.session.commit()
    return token


@pytest.fixture
def auth_client(client, user):
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1'})
    return client


TEMPLATE = 'templates/year_in_review_share.html'
PRIVATE_TEMPLATE = 'templates/year_in_review.html'
JS = 'static/js/yir-share.js'


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


# ════════════════════════════════════════════════════════════════════════════
# Share model (§46)
# ════════════════════════════════════════════════════════════════════════════

def test_share_model_record_creation(user, app):
    import api.year_in_review_share as svc
    from models import YearInReviewShare, db as _db
    with app.app_context():
        token = svc.generate_share_token()
        share = YearInReviewShare(
            user_id=user.id, year=2026,
            token_hash=svc.hash_share_token(token))
        _db.session.add(share)
        _db.session.commit()
        stored = YearInReviewShare.query.filter_by(
            token_hash=share.token_hash).one()
        assert stored.user_id == user.id
        assert stored.year == 2026
        assert stored.revoked_at is None
        _db.session.delete(stored)
        _db.session.commit()


def test_share_model_raw_token_never_persisted(user, app):
    """§46.12: only the hash is stored; the raw token appears nowhere in
    the persisted row (and is not derivable from it)."""
    import api.year_in_review_share as svc
    from models import YearInReviewShare, db as _db
    with app.app_context():
        token = svc.generate_share_token()
        share = YearInReviewShare(
            user_id=user.id, year=2026,
            token_hash=svc.hash_share_token(token))
        _db.session.add(share)
        _db.session.commit()
        stored = YearInReviewShare.query.filter_by(
            token_hash=share.token_hash).one()
        blob = json.dumps({
            'user_id': stored.user_id, 'year': stored.year,
            'token_hash': stored.token_hash,
        })
        assert token not in blob
        _db.session.delete(stored)
        _db.session.commit()


def test_share_token_entropy_and_format():
    """§46.3: 256-bit URL-safe opaque token; unique across draws."""
    import api.year_in_review_share as svc
    tokens = {svc.generate_share_token() for _ in range(25)}
    assert len(tokens) == 25  # collision-free sampling
    for token in tokens:
        assert isinstance(token, str)
        assert len(token) >= 40  # 32 bytes base64url
        assert re.fullmatch(r'[A-Za-z0-9_-]+', token)


def test_share_token_hash_lookup_and_malformed_rejection():
    import api.year_in_review_share as svc
    token = svc.generate_share_token()
    digest = svc.hash_share_token(token)
    assert re.fullmatch(r'[0-9a-f]{64}', digest)
    assert svc.hash_share_token(token) == digest  # deterministic lookup
    for bad in (None, 42, '', '  '):
        with pytest.raises(ValueError):
            svc.hash_share_token(bad)


def test_share_model_one_active_per_user_year(user, other_user, app):
    """§46.2/§14: the partial unique index backs one ACTIVE share per
    (user, year); another user's same year is unaffected; revoked rows
    free the slot."""
    import api.year_in_review_share as svc
    from models import YearInReviewShare, db as _db
    from sqlalchemy.exc import IntegrityError
    with app.app_context():
        first = YearInReviewShare(
            user_id=user.id, year=2026,
            token_hash=svc.hash_share_token(svc.generate_share_token()))
        _db.session.add(first)
        _db.session.commit()

        duplicate = YearInReviewShare(
            user_id=user.id, year=2026,
            token_hash=svc.hash_share_token(svc.generate_share_token()))
        _db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            _db.session.commit()
        _db.session.rollback()

        # Different user, same year: allowed.
        other = YearInReviewShare(
            user_id=other_user.id, year=2026,
            token_hash=svc.hash_share_token(svc.generate_share_token()))
        _db.session.add(other)
        _db.session.commit()

        # Revoked rows free the slot for regeneration.
        first.revoked_at = datetime.utcnow()
        _db.session.commit()
        replacement = YearInReviewShare(
            user_id=user.id, year=2026,
            token_hash=svc.hash_share_token(svc.generate_share_token()))
        _db.session.add(replacement)
        _db.session.commit()  # no IntegrityError

        for row in (other, replacement):
            _db.session.delete(row)
        _db.session.commit()


# ════════════════════════════════════════════════════════════════════════════
# Public transformation (§49)
# ════════════════════════════════════════════════════════════════════════════

def _canonical_recap():
    """A representative canonical recap model (Phase 8 shapes)."""
    return {
        'year': 2026,
        'available': True,
        'state': 'ready',
        'summary': {
            'total_watch_events': 127,
            'distinct_titles': 84,
            'total_hours_watched': 142,
            'average_rating': 4.1,
        },
        'highlights': {
            'top_genre': {'name': 'Drama', 'count': 40,
                          'text': 'Most watched genre: Drama.'},
            'busiest_month': {'month': '2026-10', 'count': 22,
                              'text': 'Your busiest month was October '
                                      '2026 with 22 watch events.'},
            'ratings': {'average_rating': 4.1, 'count': 90,
                        'distribution': {}, 'text': 'Average rating: 4.1.'},
            'rewatches': {'count': 3, 'rate': 0.15,
                          'text': 'Rewatched 3 times (15% of watch events).'},
            'media_split': {'movie': 90, 'tv': 37,
                            'description': 'mostly movies',
                            'text': 'Mostly movies: 90 movies and '
                                    '37 TV events.'},
        },
        'genres': [{'name': 'Drama', 'count': 40},
                   {'name': 'Comedy', 'count': 20}],
        'media_type': {'movie': 90, 'tv': 37},
        'rewatches': {'count': 3, 'rate': 0.15},
        'runtime': {'hours': 142, 'covered_events': 127,
                    'missing_events': 0, 'complete': True,
                    'text': '142 hours watched.'},
        'people': {'directors': [
            {'name': 'Ada Director', 'watch_event_count': 5,
             'distinct_title_count': 3},
            {'name': 'Bo Director', 'watch_event_count': 2,
             'distinct_title_count': 2},
            {'name': 'Cy Director', 'watch_event_count': 1,
             'distinct_title_count': 1},
            {'name': 'Di Director', 'watch_event_count': 1,
             'distinct_title_count': 1},
        ], 'actors': []},
        'season_quality': [
            {'show_name': 'Show One', 'season_number': 1,
             'rating_count': 8, 'average_rating': 4.25,
             'rating_distribution': {'5.0': 3}},
            {'show_name': 'Show Two', 'season_number': 2,
             'rating_count': 5, 'average_rating': 3.5,
             'rating_distribution': {'4.0': 2}},
            {'show_name': 'Show Three', 'season_number': 1,
             'rating_count': 2, 'average_rating': 3.0,
             'rating_distribution': {'3.0': 2}},
            {'show_name': 'Show Four', 'season_number': 1,
             'rating_count': 1, 'average_rating': 2.0,
             'rating_distribution': {'2.0': 1}},
        ],
        'daily_activity': [{'date': '2026-01-03', 'count': 2}],
        'monthly': [{'month': f'2026-{m:02d}',
                     'count': 22 if m == 10 else 0} for m in range(1, 13)],
    }


def test_public_transformation_expected_public_model():
    import api.year_in_review_share as svc
    public = svc.build_public_year_in_review(_canonical_recap())
    assert public['year'] == 2026
    assert public['summary'] == {
        'total_watch_events': 127, 'distinct_titles': 84,
        'total_hours_watched': 142, 'average_rating': 4.1}
    assert public['highlights']['top_genre']['name'] == 'Drama'
    assert public['highlights']['busiest_month']['text'].startswith(
        'Your busiest month')
    assert public['highlights']['rewatches']['text'].startswith('Rewatched')
    assert public['highlights']['media_split']['description'] == \
        'mostly movies'
    assert public['genres'] == [{'name': 'Drama', 'count': 40},
                                {'name': 'Comedy', 'count': 20}]
    assert len(public['directors']) == svc.PUBLIC_DIRECTORS_LIMIT
    assert len(public['season_quality']) == svc.PUBLIC_SEASON_LIMIT
    assert public['runtime']['text'] == '142 hours watched.'


def test_public_transformation_omits_private_only_fields():
    import api.year_in_review_share as svc
    recap = _canonical_recap()
    public = svc.build_public_year_in_review(recap)
    for banned in ('daily_activity', 'monthly', 'media_type', 'people',
                   'state', 'available', 'rewatches', 'highlights.ratings'):
        if '.' in banned:
            continue
        assert banned not in public
    # rating distribution/counts stay off the public surface; the ratings
    # highlight is omitted entirely (public card shows the summary value)
    assert 'ratings' not in public['highlights']
    assert all('rating_distribution' not in s for s in
               public['season_quality'])


def test_public_transformation_no_ids_structural():
    import api.year_in_review_share as svc
    public = svc.build_public_year_in_review(_canonical_recap())

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                leaky = any(
                    fragment in lowered for fragment in (
                        'id', 'user', 'email', 'username', 'token', 'tmdb'))
                assert not leaky, f'leaky key: {key}'
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(public)


def test_public_transformation_rejects_empty_model():
    import api.year_in_review_share as svc
    for recap in (None, {}, {'available': False, 'state': 'empty'},
                  {'year': 2026, 'available': True, 'state': 'empty'}):
        with pytest.raises(ValueError):
            svc.build_public_year_in_review(recap)


def test_public_transformation_deterministic():
    import api.year_in_review_share as svc
    recap = _canonical_recap()
    assert (svc.build_public_year_in_review(recap) ==
            svc.build_public_year_in_review(recap))


def test_public_transformation_runtime_wording_preserved():
    import api.year_in_review_share as svc
    recap = _canonical_recap()
    recap['runtime'] = {'hours': 142, 'covered_events': 120,
                        'missing_events': 7, 'complete': False,
                        'text': 'At least 142 hours watched (runtime '
                                'missing for 7 watch events).'}
    public = svc.build_public_year_in_review(recap)
    assert public['runtime']['complete'] is False
    assert public['runtime']['text'] == (
        'At least 142 hours watched (runtime missing for 7 watch events).')


def test_public_transformation_actors_omitted_completion_omitted():
    import api.year_in_review_share as svc
    recap = _canonical_recap()
    recap['tv_completion'] = {'completion_rate': 1.0}
    public = svc.build_public_year_in_review(recap)
    assert 'actors' not in public
    assert 'tv_completion' not in public


def test_public_transformation_no_subjective_language():
    import api.year_in_review_share as svc
    public = json.dumps(svc.build_public_year_in_review(_canonical_recap()))
    for banned in ('favorite', 'best', 'greatest', 'top-rated',
                   'most talented', 'obsessed', 'addicted'):
        assert banned not in public.lower()


# ════════════════════════════════════════════════════════════════════════════
# Private share API (§47)
# ════════════════════════════════════════════════════════════════════════════

def _seed_watch_events(user, count=1, year=2026):
    for i in range(count):
        m = _media(f'Seeded {user.username} {i}', runtime=90)
        _diary(user, m, date(year, 3, 3), rating=4.0)


def test_share_create_requires_auth(client):
    r = client.post('/api/year-in-review/share', json={'year': 2026})
    assert r.status_code in (302, 401)


def test_share_create_happy_path(auth_client, user, app):
    _seed_watch_events(user, 2)
    r = auth_client.post('/api/year-in-review/share', json={'year': 2026})
    assert r.status_code == 200
    body = r.get_json()
    assert body['year'] == 2026
    assert body['active'] is True
    url = body['share_url']
    token = url.rstrip('/').split('/')[-1]
    import api.year_in_review_share as svc
    # The URL carries the raw token; the DB stores only its hash.
    with app.app_context():
        from models import YearInReviewShare
        row = YearInReviewShare.query.filter_by(
            user_id=user.id, year=2026, revoked_at=None).one()
        assert row.token_hash == svc.hash_share_token(token)


def test_share_create_malformed_and_invalid_year(auth_client, user, app):
    _seed_watch_events(user, 1)
    for payload, label in (
            ({}, 'missing'),
            ({'year': '2026'}, 'string'),
            ({'year': True}, 'bool'),
            ({'year': 2026.0}, 'float'),
            ({'year': 1899}, 'too-old'),
            ({'year': 2099}, 'future')):
        r = auth_client.post('/api/year-in-review/share', json=payload)
        assert r.status_code == 400, (label, payload)


def test_share_create_invalid_year_never_invokes_builder(
        auth_client, user, app):
    import routes.statistics as route
    with patch.object(route.year_in_review_service,
                      'build_year_in_review') as builder:
        r = auth_client.post('/api/year-in-review/share',
                             json={'year': 'nope'})
    assert r.status_code == 400
    builder.assert_not_called()


def test_share_create_empty_year_rejected(auth_client, user):
    _seed_watch_events(user, 1)
    r = auth_client.post('/api/year-in-review/share', json={'year': 2001})
    assert r.status_code == 400
    assert r.get_json()['error']


def test_share_create_builder_called_exactly_once(auth_client, user, app):
    _seed_watch_events(user, 1)
    import routes.statistics as route
    with patch.object(
            route.year_in_review_service, 'build_year_in_review',
            wraps=route.year_in_review_service.build_year_in_review) as spy:
        r = auth_client.post('/api/year-in-review/share',
                             json={'year': 2026})
    assert r.status_code == 200
    assert spy.call_count == 1


def test_share_create_reuses_active_share(auth_client, user):
    _seed_watch_events(user, 1)
    auth_client.post('/api/year-in-review/share',
                     json={'year': 2026})
    auth_client.post('/api/year-in-review/share',
                     json={'year': 2026})
    from models import YearInReviewShare
    active = YearInReviewShare.query.filter_by(
        user_id=user.id, year=2026, revoked_at=None).all()
    assert len(active) == 1


def test_share_regeneration_reissues_token(auth_client, user, app):
    """§14 documented regeneration: a fresh token; the old one dies."""
    _seed_watch_events(user, 1)
    first = auth_client.post('/api/year-in-review/share',
                             json={'year': 2026}).get_json()
    old_token = first['share_url'].rstrip('/').split('/')[-1]
    second = auth_client.post('/api/year-in-review/share',
                              json={'year': 2026}).get_json()
    new_token = second['share_url'].rstrip('/').split('/')[-1]
    assert new_token != old_token
    # Old link is dead immediately; new link works.
    assert auth_client.get(
        f'/share/year-in-review/{old_token}').status_code == 404
    assert auth_client.get(
        f'/share/year-in-review/{new_token}').status_code == 200


def test_share_revoke_owner_only(auth_client, user, app):
    """§13/§33: revocation is session-scoped — a no-op for a year the
    owner never shared cannot touch the active share; revoking the real
    year kills the public link instantly."""
    _seed_watch_events(user, 1)
    created = auth_client.post('/api/year-in-review/share',
                               json={'year': 2026}).get_json()
    token = created['share_url'].rstrip('/').split('/')[-1]

    # A different year's revoke is a harmless no-op.
    r = auth_client.delete('/api/year-in-review/share',
                           json={'year': 2001})
    assert r.status_code == 200
    assert auth_client.get(
        f'/share/year-in-review/{token}').status_code == 200

    # Owner revokes → public link dies.
    r = auth_client.delete('/api/year-in-review/share',
                           json={'year': 2026})
    assert r.status_code == 200
    assert auth_client.get(
        f'/share/year-in-review/{token}').status_code == 404


def test_share_revoke_is_idempotent(auth_client, user):
    _seed_watch_events(user, 1)
    assert auth_client.delete('/api/year-in-review/share',
                              json={'year': 2026}).status_code == 200
    assert auth_client.delete('/api/year-in-review/share',
                              json={'year': 2026}).status_code == 200


def test_share_revoke_requires_valid_year(auth_client):
    assert auth_client.delete('/api/year-in-review/share',
                              json={}).status_code == 400


def test_share_endpoints_csrf_enforced(app, user):
    """§40: global CSRF stays enabled on both mutations."""
    with app.app_context():
        uname = user.username
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        c = app.test_client()
        html = c.get('/login').get_data(as_text=True)
        token = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
        c.post('/login', data={'username': uname,
                               'password': 'TestPass1',
                               'csrf_token': token},
               follow_redirects=True)
        r = c.post('/api/year-in-review/share', json={'year': 2026})
        assert r.status_code in (400, 403)  # blocked without the header
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_share_endpoints_no_direct_watch_or_media_queries(user):
    """§47.27/§28: the route module never references the models it must
    not query (all statistics work lives in the canonical builder)."""
    import inspect
    import routes.statistics as route
    source = inspect.getsource(route)
    assert 'DiaryEntry' not in source
    assert 'MediaItem' not in source
    assert 'for_you' not in source
    assert 'taste_profile' not in source


def test_share_endpoints_error_leak_protection(auth_client, user):
    _seed_watch_events(user, 1)
    r = auth_client.post('/api/year-in-review/share',
                         json={'year': '2026-ish'})
    body = r.get_data(as_text=True)
    assert 'Traceback' not in body
    assert 'sqlalchemy' not in body.lower()


def test_share_endpoints_no_network(auth_client, user):
    """§47.30: share creation is network-free (sockets blocked)."""
    _seed_watch_events(user, 1)

    def _blocked(*args, **kwargs):
        raise AssertionError('network call attempted')

    with patch.object(socket.socket, '__init__', _blocked), \
         patch.object(socket.socket, 'connect', _blocked), \
         patch.object(socket.socket, 'connect_ex', _blocked):
        r = auth_client.post('/api/year-in-review/share',
                             json={'year': 2026})
    assert r.status_code == 200


def test_share_endpoints_no_recommendation_imports():
    import inspect
    import routes.statistics as route
    import api.year_in_review_share as svc
    for module in (route, svc):
        source = inspect.getsource(module)
        for banned in ('for_you', 'taste_profile', 'recommendation_feedback',
                       'smart_lists', 'cinebot', 'taste_dna'):
            assert banned not in source, (module, banned)


# ════════════════════════════════════════════════════════════════════════════
# Public share route (§48)
# ════════════════════════════════════════════════════════════════════════════

def _create_share(auth_client, year=2026):
    body = auth_client.post('/api/year-in-review/share',
                            json={'year': year}).get_json()
    return body['share_url'].rstrip('/').split('/')[-1]


def test_public_share_valid_token(client, auth_client, user):
    _seed_watch_events(user, 2)
    token = _create_share(auth_client)
    r = client.get(f'/share/year-in-review/{token}')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Year in Review' in html
    assert '2' in html  # summary value rendered


def test_public_share_unknown_malformed_revoked_tokens(client):
    for token in ('no-such-token', '', 'x' * 200):
        assert client.get(
            f'/share/year-in-review/{token}').status_code == 404
    assert client.get('/share/year-in-review/').status_code == 404


def test_public_share_revoked_token_404(client, auth_client, user):
    _seed_watch_events(user, 1)
    token = _create_share(auth_client)
    assert client.get(f'/share/year-in-review/{token}').status_code == 200
    auth_client.delete('/api/year-in-review/share', json={'year': 2026})
    assert client.get(f'/share/year-in-review/{token}').status_code == 404


def test_public_share_no_year_override(client, auth_client, user, app):
    """§25: the token authorizes exactly its year; a ?year parameter is
    never honored by the public route."""
    with app.app_context():
        m = _media('Year Bind', runtime=90)
        _diary(user, m, date(2025, 3, 3), rating=4.0)
    token = _create_share(auth_client, year=2025)
    viewer = app.test_client()  # genuinely anonymous viewer
    for url in (f'/share/year-in-review/{token}?year=2026',
                f'/share/year-in-review/{token}?year=2001'):
        r = viewer.get(url)
        html = r.get_data(as_text=True)
        if r.status_code == 200:
            assert '2025 IN REVIEW' in html
            assert '2026 IN REVIEW' not in html
            assert '2001 IN REVIEW' not in html


def test_public_share_inactive_owner_fails_closed(client, user, app):
    """§26: deactivated/deleted owner → generic 404 for a genuinely
    anonymous client (no login anywhere in this test)."""
    from models import User, db as _db
    m = _media('Owner Gone', runtime=90)
    _diary(user, m, date(2026, 3, 3), rating=4.0)
    token = _create_share_record(app, user)
    with app.app_context():
        u = _db.session.get(User, user.id)
        u.is_active = False
        _db.session.commit()
    try:
        assert client.get(
            f'/share/year-in-review/{token}').status_code == 404
    finally:
        with app.app_context():
            u = _db.session.get(User, user.id)
            u.is_active = True
            _db.session.commit()


def test_public_share_no_auth_required(client):
    # An unknown token is simply 404 — the route itself needs no login.
    r = client.get('/share/year-in-review/no-such-token')
    assert r.status_code == 404


def test_public_share_no_private_api_leak(client, user, app):
    """§7/§48.41: the share page is public, but the private endpoints
    stay anonymous-gated (this test never logs in)."""
    m = _media('Leak Probe', runtime=90)
    _diary(user, m, date(2026, 3, 3), rating=4.0)
    token = _create_share_record(app, user)
    assert client.get(f'/share/year-in-review/{token}').status_code == 200
    assert client.get('/api/year-in-review').status_code in (302, 401)
    assert client.get('/api/statistics').status_code in (302, 401)


def test_public_share_no_ids_or_identity_in_html(client, user, app):
    """§23/§24/§48.42–44: no media identity, email, or username in the
    public HTML (anonymous client; share created via the model)."""
    m = _media('Secret Movie Title', runtime=90)
    _diary(user, m, date(2026, 3, 3), rating=4.0)
    tmdb_id = m.tmdb_id
    token = _create_share_record(app, user)
    html = client.get(
        f'/share/year-in-review/{token}').get_data(as_text=True)
    assert 'Secret Movie Title' not in html  # no title-level ranking exists
    assert user.email not in html
    assert user.username not in html
    assert str(tmdb_id) not in html


def test_public_share_headers_and_metadata(client, auth_client, user):
    """§17/§48.51/§54: no-store (revocation correctness), noindex."""
    _seed_watch_events(user, 1)
    token = _create_share(auth_client)
    r = client.get(f'/share/year-in-review/{token}')
    assert r.headers.get('Cache-Control') == 'no-store'
    assert r.headers.get('X-Robots-Tag') == 'noindex, nofollow'
    html = r.get_data(as_text=True)
    assert 'noindex' in html


def test_public_share_call_counts(client, auth_client, user, app):
    """§38/§48.45–46: one builder call → one statistics call per view."""
    _seed_watch_events(user, 1)
    token = _create_share(auth_client)
    import routes.main as main_module
    import api.year_in_review as builder_module
    with patch.object(main_module.year_in_review_service,
                      'build_year_in_review',
                      wraps=main_module.year_in_review_service
                      .build_year_in_review) as builder_spy:
        with patch.object(builder_module, 'get_statistics',
                          wraps=builder_module.get_statistics) as stats_spy:
            r = client.get(f'/share/year-in-review/{token}')
    assert r.status_code == 200
    assert builder_spy.call_count == 1
    assert stats_spy.call_count == 1


def test_public_share_bounded_query_count(client, user, app):
    """§38: fixed bounded work — an anonymous public view performs a
    FIXED 10 statements: 1 share lookup + 1 owner read + the canonical
    8-statement statistics build (no login, no auth query, nothing that
    scales with events/directors/seasons)."""
    for i in range(12):
        _seed_watch_events(user, 1)
        _diary(user, _media(f'Bulk {i}', runtime=90), date(2026, 3, 3))
    token = _create_share_record(app, user)
    from models import db as _db
    import sqlalchemy.event as sa_event
    statements = []

    def _record(conn, cursor, statement, *args, **kwargs):
        statements.append(statement)

    with app.app_context():
        sa_event.listen(_db.engine, 'before_cursor_execute', _record)
        try:
            r = client.get(f'/share/year-in-review/{token}')
        finally:
            sa_event.remove(_db.engine, 'before_cursor_execute', _record)
    assert r.status_code == 200
    assert len(statements) == 10, statements  # 1 share + 1 owner + 8 canonical


def test_public_share_deterministic_content(client, auth_client, user):
    _seed_watch_events(user, 2)
    token = _create_share(auth_client)
    first = client.get(f'/share/year-in-review/{token}').get_data(as_text=True)
    second = client.get(
        f'/share/year-in-review/{token}').get_data(as_text=True)
    assert first == second


def test_public_share_no_network(client, auth_client, user):
    """§48.47: the entire public view is network-free."""
    _seed_watch_events(user, 1)
    token = _create_share(auth_client)

    def _blocked(*args, **kwargs):
        raise AssertionError('network call attempted')

    with patch.object(socket.socket, '__init__', _blocked), \
         patch.object(socket.socket, 'connect', _blocked), \
         patch.object(socket.socket, 'connect_ex', _blocked):
        r = client.get(f'/share/year-in-review/{token}')
    assert r.status_code == 200


# ════════════════════════════════════════════════════════════════════════════
# End-to-end director + season flow through the public card
# ════════════════════════════════════════════════════════════════════════════

def test_public_share_directors_and_seasons_flow(
        client, auth_client, user):
    d = _director('Nolan-ish', 777777)
    m = _media('Directed Movie', runtime=120)
    _attach_director(m, d)
    _diary(user, m, date(2026, 2, 2), rating=4.0)
    tv = _media('Season Show', runtime=45, media_type='tv')
    _diary(user, tv, date(2026, 2, 3))
    for episode, rating in ((1, 5.0), (2, 4.0), (3, 4.0)):
        _episode(user, tv, 1, episode, date(2026, 2, 3), rating=rating)
    token = _create_share(auth_client)
    html = client.get(f'/share/year-in-review/{token}').get_data(as_text=True)
    assert 'Nolan-ish' in html
    assert '1 watches' in html or '1 watch' in html
    assert 'Season Show' in html
    assert 'S1' in html


# ════════════════════════════════════════════════════════════════════════════
# Frontend share controls (§50)
# ════════════════════════════════════════════════════════════════════════════

def test_share_js_no_request_on_load():
    """§50.70: nothing fires until the user clicks Share — real code only
    (block comments stripped; prose mentions must not trip guards)."""
    code = re.sub(r'/\*.*?\*/', '', _read(JS), flags=re.S)
    assert code.count('fetch(') == 1
    # The single fetch lives inside the request helper, which only the
    # click handlers call; init() wires listeners but never requests.
    init_index = code.index('function init()')
    assert code.index('fetch(') < init_index
    assert "addEventListener('click'" in code
    assert "addEventListener('load'" not in code
    assert "setInterval" not in code  # no polling (§50.71)


def test_share_js_single_request_helper_and_guarded_clicks():
    code = _read(JS)
    assert 'inFlight' in code  # duplicate-click guard
    assert "method: method" in code  # one shared POST/DELETE helper


def test_share_js_csrf_header_and_credentials():
    code = _read(JS)
    assert 'X-CSRFToken' in code
    assert "credentials: 'same-origin'" in code


def test_share_js_clipboard_fallback_and_status():
    code = _read(JS)
    assert 'navigator.clipboard' in code
    assert 'execCommand' in code  # safe fallback path
    assert 'yir-share-status' in code


def test_share_js_no_innerhtml_and_no_tmdb():
    # Strip block AND line comments — prose must never trip guards.
    code = re.sub(r'/\*.*?\*/', '', _read(JS), flags=re.S)
    code = re.sub(r'(^|\s)//.*$', '', code, flags=re.M)
    assert 'innerHTML' not in code
    assert 'tmdb' not in code.lower()
    assert '/api/statistics' not in code


def test_share_js_only_own_share_endpoint():
    code = _read(JS)
    assert "'/api/year-in-review/share'" in code
    assert code.count("'/api/") == 1


def test_share_controls_template_wiring():
    html = _read(PRIVATE_TEMPLATE)
    assert 'yir-share-btn' in html
    assert 'yir-copy-btn' in html
    assert 'yir-revoke-btn' in html
    assert 'yir-share-status' in html
    assert 'aria-live' in html
    assert 'js/yir-share.js' in html


def test_public_template_no_private_controls():
    html = _read(TEMPLATE)
    for banned in ('csrf', 'login_required', 'yir-share-btn',
                   'profile', 'settings'):
        assert banned not in html.lower()


def test_public_template_no_fetch_no_dynamic_js_logic():
    """§18/§23: the public page is self-contained server-rendered HTML;
    no app JS ships on it and crawlers need no scripting."""
    html = _read(TEMPLATE)
    # Only the CDN Tailwind script tag is present — no local app JS.
    local_scripts = [line for line in html.splitlines()
                     if '<script src=' in line
                     and 'cdn.tailwindcss.com' not in line]
    assert local_scripts == []
    assert 'fetch(' not in html


def test_public_template_escaping():
    """§41: Jinja autoescape is on by default for .html templates; this
    pins that no |safe filter bypasses it."""
    html = _read(TEMPLATE)
    assert '|safe' not in html


# ════════════════════════════════════════════════════════════════════════════
# Source guards — security invariants (§51)
# ════════════════════════════════════════════════════════════════════════════

def test_guard_public_route_never_queries_watch_or_media_models():
    source = _read('routes/main.py')
    assert 'DiaryEntry' not in source
    assert 'MediaItem' not in source


def test_guard_no_public_statistics_endpoints():
    """§56: the private endpoints carry login_required; no public
    profile/statistics surface exists."""
    import inspect
    import routes.statistics as route
    for func in (route.api_statistics, route.api_year_in_review,
                 route.api_year_in_review_share_create,
                 route.api_year_in_review_share_revoke):
        source = inspect.getsource(func)
        assert 'login_required' in source, func.__name__
    module_source = inspect.getsource(route)
    assert '/profile/' not in module_source
    assert '/api/users/' not in module_source


def test_guard_no_user_id_parameters():
    source = _read('routes/main.py')
    share_source = source[source.index('year_in_review_share_page'):]
    assert 'user_id=' not in share_source
    assert 'request.args' not in share_source


def test_guard_no_raw_token_logging():
    """§15/§46.12: the raw token is never logged or re-persisted; the
    statistics route module contains no print debugging either."""
    source = _read('routes/statistics.py')
    assert 'logger.info(token' not in source
    assert re.search(r'\bprint\(', source) is None
    import api.year_in_review_share as svc
    import inspect
    transform_source = inspect.getsource(svc)
    assert 'logger.' not in transform_source


def test_guard_no_llm_or_tmdb_in_share_layer():
    import inspect
    import api.year_in_review_share as svc
    source = inspect.getsource(svc)
    for banned in ('openai', 'tmdb', 'requests.', 'httpx', 'urllib'):
        assert banned not in source.lower()


def test_guard_no_completion_metric_in_share_layer():
    import inspect
    import api.year_in_review_share as svc
    source = inspect.getsource(svc)
    assert 'completion' not in source.lower()
