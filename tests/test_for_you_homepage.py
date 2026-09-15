"""For You homepage rail (Feature #6/#7 Phase 5) — focused regression.

Verifies the homepage integration: authenticated-only placeholder shell,
single client fetch of the canonical GET /api/for-you, cold-start/error
hiding, existing rails untouched, and client hygiene (no TMDb calls, no
feedback/taste calls, no scoring logic, no polling) plus route/API contract
tests for the shapes the JS relies on.

No JS test framework exists in this repo, so client behavior is pinned by
source assertions (the established repo pattern, cf. continue-watching
telemetry guards) and by contract tests against the real route.
"""
import inspect

import pytest

from models import db, User, TasteProfile, MediaItem


# ── module-unique data (suite convention: clean up everything) ───────────────
DOMAIN = 'foryouhome.test'


@pytest.fixture(autouse=True)
def _clean_home_rows(app):
    yield
    TasteProfile.query.delete()
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
    with app.app_context():
        yield _make_user('fuyhome')


def _login(client):
    client.post('/login', data={
        'username': 'fuyhome', 'password': 'TestPass1'})
    return client


@pytest.fixture
def auth_client(client, user):
    return _login(client)


def _js_source():
    with open('static/js/for-you.js', encoding='utf-8') as f:
        return f.read()


def _js_code():
    """JS source with /*…*/ and //… comments stripped — source guards must
    pin executable behavior, not prose."""
    src = _js_source()
    lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(('/*', '*', '//')):
            continue
        lines.append(line)
    return '\n'.join(lines)


# ── 1/25. Anonymous homepage: unchanged, no For You ─────────────────────────

def test_anonymous_homepage_has_no_for_you(client):
    r = client.get('/')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'data-for-you-placeholder' not in html
    assert 'for-you.js' not in html


def test_anonymous_homepage_has_no_empty_heading(client):
    html = client.get('/').data.decode()
    assert '>For You<' not in html


def test_anonymous_homepage_other_rails_structurally_intact(client):
    """Anonymous page still renders (hero/rails region present, no errors)."""
    r = client.get('/')
    assert r.status_code == 200
    assert b'rail' in r.data


# ── 2/3/23. Authenticated homepage renders the For You container ────────────

def test_authenticated_homepage_renders_for_you_container(auth_client):
    html = auth_client.get('/').data.decode()
    assert 'data-for-you-placeholder' in html
    assert html.count('data-for-you-placeholder') == 1
    assert '>For You<' in html          # semantic h2 heading
    assert 'aria-label="For You"' in html


def test_authenticated_homepage_includes_for_you_js_once(auth_client):
    html = auth_client.get('/').data.decode()
    assert html.count('js/for-you.js') == 1


def test_for_you_positioned_before_trending_rail(auth_client, monkeypatch):
    """Shell must sit after personal rails, before generic discovery rails."""
    monkeypatch.setattr(
        'routes.browse.cached_tmdb_request',
        lambda url: {'results': [{'id': 1, 'title': 'Hit',
                                  'poster_path': '/x.jpg',
                                  'release_date': '2024-01-01'}]})
    html = auth_client.get('/').data.decode()
    assert 'Trending This Week' in html
    assert html.index('data-for-you-placeholder') \
        < html.index('Trending This Week')


def test_rail_keeps_scroll_arrow_convention(auth_client):
    """Existing rail machinery (arrows target rail-<key>) works unchanged."""
    html = auth_client.get('/').data.decode()
    assert 'data-rail-target="rail-for_you"' in html
    assert 'id="rail-for_you"' in html


def test_skeleton_placeholder_rendered(auth_client):
    html = auth_client.get('/').data.decode()
    assert 'loading rounded-xl' in html


# ── 6/18. Engine never invoked during homepage render ───────────────────────

def test_engine_not_invoked_during_homepage_render(auth_client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError('engine must not run during homepage render')
    monkeypatch.setattr('api.for_you.get_for_you', boom)
    assert auth_client.get('/').status_code == 200


def test_homepage_ok_when_tmdb_unreachable(auth_client):
    """Fake key → upstream 401s; homepage must still render with the shell."""
    r = auth_client.get('/')
    assert r.status_code == 200
    assert 'data-for-you-placeholder' in r.data.decode()


# ── 4/5/7/8/24. API contract (shapes the JS consumes) ───────────────────────

def _personalized_shape():
    """Mirrors api/for_you.py::_item + get_for_you response contract."""
    return {
        'personalized': True, 'mode': 'full', 'confidence': 0.73,
        'reason_state': 'learned_taste',
        'items': [{
            'tmdb_id': 550, 'media_type': 'movie', 'title': 'Fight Club',
            'poster_path': '/p.jpg', 'release_date': '1999-10-15',
            'source': 'genre_discover',
            'reason': {'kind': 'genre_affinity',
                       'text': 'Because you like thriller',
                       'evidence': [{'genre': 'Thriller'}]},
        }],
    }


def test_api_personalized_shape_matches_js_contract(auth_client, monkeypatch):
    monkeypatch.setattr('api.for_you.get_for_you',
                        lambda *a, **kw: _personalized_shape())
    r = auth_client.get('/api/for-you')
    assert r.status_code == 200
    data = r.get_json()
    assert data['personalized'] is True
    assert data['confidence'] == 0.73
    item = data['items'][0]
    for key in ('tmdb_id', 'media_type', 'title', 'poster_path', 'reason'):
        assert key in item
    assert item['reason']['text'].startswith('Because you')


def test_api_items_expose_no_scoring_internals(auth_client, monkeypatch):
    monkeypatch.setattr('api.for_you.get_for_you',
                        lambda *a, **kw: _personalized_shape())
    item = auth_client.get('/api/for-you').get_json()['items'][0]
    for banned in ('score', 'components', 'genre_affinity_score',
                   'ranking', 'breakdown'):
        assert banned not in item


def test_api_cold_start_shape(auth_client, monkeypatch):
    monkeypatch.setattr('api.for_you.get_for_you', lambda *a, **kw: {
        'personalized': False, 'mode': 'cold', 'confidence': 0.0,
        'reason_state': 'no_taste_profile_yet', 'items': []})
    r = auth_client.get('/api/for-you')
    assert r.status_code == 200
    data = r.get_json()
    assert data['personalized'] is False
    assert data['items'] == []


def test_api_requires_auth(client):
    assert client.get('/api/for-you').status_code in (302, 401)


def test_route_rejects_malformed_limit(auth_client):
    assert auth_client.get('/api/for-you?limit=abc').status_code == 400


def test_route_rejects_oversized_limit(auth_client):
    assert auth_client.get('/api/for-you?limit=50').status_code == 400


def test_client_cannot_supply_another_user(auth_client, user, monkeypatch):
    seen = {}

    def fake(uid, **kw):
        seen['uid'] = uid
        return {'personalized': False, 'items': []}
    monkeypatch.setattr('api.for_you.get_for_you', fake)
    auth_client.get('/api/for-you?user_id=999999')
    assert seen['uid'] == user.id


def test_engine_failure_returns_json_error(auth_client, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError('db down')
    monkeypatch.setattr('api.for_you.get_for_you', boom)
    r = auth_client.get('/api/for-you')
    assert r.status_code == 500
    assert r.get_json()['error'] == 'Internal server error'


def test_route_uses_session_identity_not_request_args():
    """Structural guard: the route signature accepts no user selector."""
    from routes.for_you import api_for_you
    params = inspect.signature(api_for_you).parameters
    assert all(p not in params for p in ('user_id', 'uid', 'user'))


# ── 12–20. Client hygiene (source guards, repo-established pattern) ─────────

def test_js_makes_exactly_one_fetch():
    """Exactly ONE /api/for-you request per page load (the second fetch in
    the module is the Phase 7 feedback flush, pinned separately)."""
    assert _js_code().count("fetch('/api/for-you'") == 1


def test_js_has_no_polling():
    src = _js_code()
    for banned in ('setInterval', 'scroll'):
        assert banned not in src, f'banned polling/loop token: {banned}'


def test_js_calls_only_the_canonical_api():
    src = _js_code()
    assert '/api/for-you' in src
    assert 'api.themoviedb.org' not in src       # no direct TMDb
    assert 'taste' not in src                    # no TasteProfile calls
    # Phase 7: exactly one feedback endpoint, the canonical one.
    assert src.count("'/api/rec/feedback'") == 1
    assert src.count('/api/rec/') == 1


def test_js_contains_no_scoring_or_ranking_logic():
    src = _js_source()
    for banned in ('vote_average', 'genre_affinity_score', 'score =',
                   'ranking', 'sort(', 'candidate'):
        assert banned not in src, f'banned scoring token: {banned}'


def test_js_renders_reason_verbatim_not_fabricated():
    src = _js_source()
    assert 'reason.text' in src                  # server-provided only
    assert 'Because you' not in src              # no client-side prose


def test_js_uses_canonical_detail_urls():
    src = _js_source()
    assert "'/movie/' + item.tmdb_id" in src
    assert "'/tv/' + item.tmdb_id" in src


def test_js_hides_section_on_cold_start_or_error():
    src = _js_source()
    assert 'personalized !== true' in src        # cold start → hide
    assert '.catch(hide)' in src                 # network/parse error → hide
    assert 'removeChild' in src                  # section actually removed


def test_js_poster_placeholder_follows_existing_behavior():
    src = _js_source()
    assert '/static/images/no-poster.svg' in src
    assert 'no-poster.png' not in src


def test_js_dedupes_within_rail():
    """Guard against duplicate cards for one (media_type, tmdb_id)."""
    assert 'seen[' in _js_source()


# ── 22. Existing rails untouched (server-rendered path) ─────────────────────

def test_existing_trending_rail_still_renders(auth_client, monkeypatch):
    monkeypatch.setattr(
        'routes.browse.cached_tmdb_request',
        lambda url: {'results': [{'id': 42, 'title': 'Hit Movie',
                                  'poster_path': '/h.jpg',
                                  'release_date': '2024-02-01'}]})
    html = auth_client.get('/').data.decode()
    assert 'Trending This Week' in html
    assert 'Hit Movie' in html
    assert 'data-for-you-placeholder' in html
