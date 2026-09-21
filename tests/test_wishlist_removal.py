"""Wishlist→Watchlist consolidation contract tests.

Pins the post-consolidation product surface:

  - /wishlist redirects to the canonical /watchlist (no Wishlist page)
  - legacy add/remove wishlist routes redirect to Watchlist actions
  - the wishlist branch of the priority API is rejected
  - watchlist add/remove/priority remain functional end-to-end
  - no Wishlist UI renders on detail/search/genre/recommendation/profile
  - no Wishlist link remains in global navigation
  - get_user_collection_ids returns the two-value (watchlist, viewed) shape
  - no user_wishlist ORM declaration remains importable
"""
import pytest

from models import db, User, MediaItem, user_watchlist, user_viewed

DOMAIN = 'wlremove.test'


def _make_user(username):
    u = User(username=username, email=f'{username}@{DOMAIN}',
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture(autouse=True)
def _clean_rows(app):
    yield
    db.session.execute(user_viewed.delete())
    db.session.execute(user_watchlist.delete())
    db.session.commit()
    MediaItem.query.filter(
        MediaItem.tmdb_id.between(880001, 889999)).delete(
        synchronize_session=False)
    db.session.commit()
    User.query.filter(User.email.like(f'%@{DOMAIN}')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture
def auth_user(app, client):
    user = _make_user('wl_owner')
    client.post('/login', data={
        'username': 'wl_owner', 'password': 'TestPass1'},
        follow_redirects=True)
    return user


def _media(tmdb_id, title='WL Test Movie'):
    m = MediaItem(tmdb_id=tmdb_id, media_type='movie', title=title)
    db.session.add(m)
    db.session.commit()
    return m


# ── Route-level behavior ─────────────────────────────────────────────────────

def test_wishlist_page_redirects_to_watchlist(auth_user, client):
    resp = client.get('/wishlist')
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/watchlist')


def test_add_to_wishlist_redirects_to_watchlist_add(auth_user, client):
    m = _media(880001)
    resp = client.get(f'/add_to_wishlist/{m.tmdb_id}/movie',
                      follow_redirects=False)
    assert resp.status_code == 302
    assert f'/add_to_watchlist/{m.tmdb_id}/movie' in resp.headers['Location']


def test_add_to_wishlist_preserves_priority_param(auth_user, client):
    m = _media(880002)
    resp = client.get(
        f'/add_to_wishlist/{m.tmdb_id}/movie?priority=high',
        follow_redirects=False)
    assert 'priority=high' in resp.headers['Location']


def test_remove_from_wishlist_redirects_to_watchlist_remove(
        auth_user, client):
    m = _media(880003)
    resp = client.get(f'/remove_from_wishlist/{m.tmdb_id}/movie',
                      follow_redirects=False)
    assert resp.status_code == 302
    assert (f'/remove_from_watchlist/{m.tmdb_id}/movie'
            in resp.headers['Location'])


def test_legacy_wishlist_routes_require_auth(client):
    # No user logged in: legacy routes must not bypass login.
    assert client.get('/wishlist').status_code in (302, 401)
    assert client.get('/add_to_wishlist/1/movie').status_code in (302, 401)
    assert client.get('/remove_from_wishlist/1/movie').status_code in (302, 401)


def test_priority_api_rejects_wishlist_list_type(auth_user, client):
    m = _media(880004)
    db.session.execute(user_watchlist.insert().values(
        user_id=auth_user.id, media_id=m.id, media_type='movie',
        priority='medium'))
    db.session.commit()
    resp = client.post(f'/api/update_priority/wishlist/{m.tmdb_id}/movie',
                       json={'priority': 'high'})
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False


def test_wishlist_add_redirect_creates_watchlist_row_not_wishlist(
        auth_user, client):
    # Chasing the legacy redirect performs a REAL watchlist add —
    # wishlist state can no longer be created anywhere.
    m = _media(880005)
    client.get(f'/add_to_wishlist/{m.tmdb_id}/movie?priority=high',
               follow_redirects=True)
    rows = db.session.execute(user_watchlist.select().where(
        user_watchlist.c.user_id == auth_user.id)).fetchall()
    assert len(rows) == 1, "watchlist row must exist after redirect add"
    assert rows[0].priority == 'high'
    # No wishlist model/table exists to receive anything.
    from models import associations
    assert not hasattr(associations, 'user_wishlist')


# ── Watchlist remains fully functional ───────────────────────────────────────

def test_watchlist_add_remove_still_works(auth_user, client):
    m = _media(880006)
    client.get(f'/add_to_watchlist/{m.tmdb_id}/movie')
    rows = db.session.execute(user_watchlist.select().where(
        user_watchlist.c.user_id == auth_user.id)).fetchall()
    assert len(rows) == 1

    client.get(f'/remove_from_watchlist/{m.tmdb_id}/movie')
    rows = db.session.execute(user_watchlist.select().where(
        user_watchlist.c.user_id == auth_user.id)).fetchall()
    assert len(rows) == 0


def test_watchlist_page_renders(auth_user, client):
    m = _media(880007)
    db.session.execute(user_watchlist.insert().values(
        user_id=auth_user.id, media_id=m.id, media_type='movie',
        priority='medium'))
    db.session.commit()
    resp = client.get('/watchlist')
    assert resp.status_code == 200
    assert b'My Watchlist' in resp.data


def test_watchlist_priority_update_api(auth_user, client):
    m = _media(880008)
    db.session.execute(user_watchlist.insert().values(
        user_id=auth_user.id, media_id=m.id, media_type='movie',
        priority='low'))
    db.session.commit()
    resp = client.post(f'/api/update_priority/watchlist/{m.tmdb_id}/movie',
                       json={'priority': 'high'})
    assert resp.status_code == 200
    assert resp.get_json()['priority'] == 'high'


# ── No Wishlist UI anywhere ──────────────────────────────────────────────────

def _assert_no_wishlist_ui(html):
    assert b'wishlist' not in html.lower(), \
        "user-visible Wishlist UI leaked into a rendered page"


def test_no_wishlist_ui_on_movie_detail(auth_user, client, monkeypatch):
    import routes.details as details
    monkeypatch.setattr(details, 'fetch_movie_details', lambda _: {
        'id': 880009, 'genres': []})
    resp = client.get('/movie/880009')
    assert resp.status_code == 200
    _assert_no_wishlist_ui(resp.data)


def test_no_wishlist_ui_on_tv_detail(auth_user, client, monkeypatch):
    import routes.details as details
    monkeypatch.setattr(details, 'fetch_tv_show_details', lambda _: {
        'id': 880010, 'genres': []})
    resp = client.get('/tv/880010')
    assert resp.status_code == 200
    _assert_no_wishlist_ui(resp.data)


def test_no_wishlist_ui_on_search(auth_user, client, monkeypatch):
    import routes.browse as browse
    monkeypatch.setattr(browse, 'search_media', lambda *a, **k: [])
    monkeypatch.setattr(browse, '_format_search_results',
                        lambda *a, **k: [])
    resp = client.get('/search?q=anything')
    assert resp.status_code == 200
    _assert_no_wishlist_ui(resp.data)


def test_no_wishlist_ui_on_genre_page(auth_user, client, monkeypatch):
    import routes.browse as browse
    monkeypatch.setattr(browse, 'fetch_movies_by_genre',
                        lambda genre_id: [])
    resp = client.get('/genre/Action')
    assert resp.status_code == 200
    _assert_no_wishlist_ui(resp.data)


def test_no_wishlist_ui_on_profile_recommendations(
        auth_user, client, monkeypatch):
    import routes.auth as auth_routes
    monkeypatch.setattr(auth_routes, '_build_recommendations',
                        lambda items, **k: [])
    resp = client.get('/profile/recommendations')
    assert resp.status_code in (200, 302), resp.status_code
    if resp.status_code == 200:
        _assert_no_wishlist_ui(resp.data)


def test_no_wishlist_link_in_global_nav(client):
    # Unauthenticated shell still renders base nav chrome.
    resp = client.get('/login')
    assert resp.status_code == 200
    assert b'>Wishlist<' not in resp.data
    assert b'wishlist' not in resp.data.lower()


def test_no_user_wishlist_orm_declaration():
    import models
    import models.user
    import models.associations
    assert 'user_wishlist' not in models.__all__
    assert not hasattr(models.user.User, 'wishlist')
    assert not hasattr(models.associations, 'user_wishlist')


# ── Utility contract ─────────────────────────────────────────────────────────

def test_get_user_collection_ids_returns_watchlist_viewed_pair(
        auth_user, app):
    from utils.collections import get_user_collection_ids
    m = _media(880011)
    db.session.execute(user_watchlist.insert().values(
        user_id=auth_user.id, media_id=m.id, media_type='movie',
        priority='medium'))
    db.session.commit()

    with app.app_context():
        from flask_login import login_user
        login_user(auth_user)
        watchlist_ids, viewed_ids = get_user_collection_ids(auth_user)
        assert (m.tmdb_id, 'movie') in watchlist_ids
        assert viewed_ids == set()


def test_get_user_collection_ids_anonymous(auth_user, app):
    from utils.collections import get_user_collection_ids
    anon = type('Anon', (), {'is_authenticated': False})()
    assert get_user_collection_ids(anon) == (set(), set())
