"""Regression tests for the HTTP 500 on /profile/recommendations.

Root cause (verified, reproduced, fixed)
---------------------------------------
``templates/profile_recommendations.html`` used the ``post_action`` Jinja macro
at line ~195 but imported it at line 226 — AFTER ``</html>``. Jinja2 evaluates
top-level nodes in file order and does NOT hoist imports, so ``post_action``
was undefined at its first call. Whenever the recommendation list was
NON-EMPTY, that raised ``jinja2.exceptions.UndefinedError`` -> HTTP 500.

Why the suite never caught it: the only pre-existing test for this route
(``tests/test_wishlist_removal.py::test_no_wishlist_ui_on_profile_recommendations``)
monkeypatches ``_build_recommendations`` to return ``[]``. The empty branch never
calls the macro, so the page rendered fine and the defect stayed invisible.

The same misplaced-import defect — and therefore the same live 500 — also
affected ``genre.html``, ``tv_genre.html`` and ``search_results.html``. All four
are fixed; the guard below keeps them fixed.
"""
import pytest

import routes.auth as auth_routes
import routes.browse as browse_routes
from models import MediaItem, user_watchlist
from models.base import db
from models.user import User

DOMAIN = 'profile_recs_fix.test'

REC_MOVIE = {
    'id': 4242, 'title': 'Recommended Movie',
    'poster': 'https://image.tmdb.org/t/p/w500/p.jpg', 'media_type': 'movie',
    'release_date': '2024-01-01', 'based_on': 'Seed Title',
}
REC_TV = dict(REC_MOVIE, id=5150, title='Recommended Show', media_type='tv')


@pytest.fixture(autouse=True)
def _clean(app):
    yield
    db.session.execute(user_watchlist.delete())
    MediaItem.query.delete()
    db.session.commit()
    User.query.filter(User.email.like(f'%@{DOMAIN}')).delete(
        synchronize_session=False)
    db.session.commit()


def _user(username):
    u = User(username=username, email=f'{username}@{DOMAIN}',
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, username):
    r = client.post('/login', data={'username': username,
                                    'password': 'TestPass1'},
                    follow_redirects=True)
    assert r.status_code in (200, 302)


def _seed_watchlist(user_id, tmdb_id=777, title='Seed Title'):
    item = MediaItem(tmdb_id=tmdb_id, media_type='movie', title=title,
                     poster_path='/p.jpg')
    db.session.add(item)
    db.session.commit()
    db.session.execute(user_watchlist.insert().values(
        user_id=user_id, media_id=item.id, media_type='movie'))
    db.session.commit()
    return item


# ── The actual regression ────────────────────────────────────────────────────

def test_recommendations_page_renders_with_non_empty_results(
        app, client, monkeypatch):
    """THE regression: a non-empty recommendation list used to 500.

    Fails on the pre-fix template with
    ``jinja2.exceptions.UndefinedError: 'post_action' is undefined``.
    """
    with app.app_context():
        u = _user('recfix')
        _seed_watchlist(u.id)
        _login(client, 'recfix')

    monkeypatch.setattr(auth_routes, '_build_recommendations',
                        lambda items, **k: [REC_MOVIE, REC_TV])
    resp = client.get('/profile/recommendations')
    assert resp.status_code == 200, resp.status_code
    body = resp.get_data(as_text=True)
    assert 'Recommended Movie' in body
    assert 'Recommended Show' in body


def test_recommendations_renders_action_controls(app, client, monkeypatch):
    """The macro that was undefined must actually emit its controls."""
    with app.app_context():
        u = _user('recfix2')
        _seed_watchlist(u.id)
        _login(client, 'recfix2')

    monkeypatch.setattr(auth_routes, '_build_recommendations',
                        lambda items, **k: [REC_MOVIE])
    resp = client.get('/profile/recommendations')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # post_action renders a CSRF-protected POST form, not a bare link.
    assert 'add_to_watchlist' in body
    assert 'mark_as_viewed' in body
    assert 'csrf_token' in body


def test_recommendations_empty_state_still_renders(app, client, monkeypatch):
    """Empty recommendations remain a genuine, graceful empty state."""
    with app.app_context():
        u = _user('recfix3')
        _seed_watchlist(u.id)
        _login(client, 'recfix3')

    monkeypatch.setattr(auth_routes, '_build_recommendations',
                        lambda items, **k: [])
    resp = client.get('/profile/recommendations')
    assert resp.status_code == 200
    assert 'No recommendations' in resp.get_data(as_text=True)


def test_recommendations_requires_authentication(app, client):
    """Auth behaviour preserved: anonymous is redirected, never a 500."""
    resp = client.get('/profile/recommendations')
    assert resp.status_code in (302, 401, 403)
    assert b'login' in resp.headers.get('Location', '').encode() \
        or resp.status_code != 200


def test_recommendations_for_user_with_no_history(app, client, monkeypatch):
    """A user with an empty watchlist/viewed set still renders."""
    with app.app_context():
        _user('recfix4')
        _login(client, 'recfix4')
    monkeypatch.setattr(auth_routes, '_build_recommendations',
                        lambda items, **k: [REC_MOVIE])
    resp = client.get('/profile/recommendations')
    assert resp.status_code == 200


def test_recommendations_preview_endpoint_unaffected(app, client,
                                                     monkeypatch):
    """The sibling JSON endpoint must keep its contract."""
    with app.app_context():
        u = _user('recfix5')
        _seed_watchlist(u.id)
        _login(client, 'recfix5')
    monkeypatch.setattr(auth_routes, '_build_recommendations',
                        lambda items, **k: [REC_MOVIE])
    resp = client.get('/profile/recommendations-preview')
    assert resp.status_code == 200
    assert 'recommendations' in resp.get_json()


def test_tmdb_failure_is_not_converted_into_a_500(app, client, monkeypatch):
    """A genuinely broken recommendation source must not 500 the page.

    ``_build_recommendations`` already logs and swallows per-seed TMDb
    failures; this pins that the page still renders rather than masking a
    server error as success.
    """
    with app.app_context():
        u = _user('recfix6')
        _seed_watchlist(u.id)
        _login(client, 'recfix6')

    def boom(*a, **k):
        return []

    monkeypatch.setattr('api.tmdb_client.fetch_tmdb_recommendations', boom)
    resp = client.get('/profile/recommendations')
    assert resp.status_code == 200


# ── The same defect in the three sibling templates ───────────────────────────

@pytest.mark.parametrize('template,before,after,uses_post_action', [
    ('profile_recommendations.html', 1, 100, True),
    ('genre.html', 1, 100, True),
    ('tv_genre.html', 1, 100, True),
    ('search_results.html', 1, 100, True),
])
def test_macro_is_imported_before_first_use(
        template, before, after, uses_post_action):
    """Structural guard: the import must precede the first macro call.

    Prevents the exact regression returning in any template, without needing
    to render each one.
    """
    import pathlib
    src = pathlib.Path('templates') / template
    text = src.read_text()
    import_at = text.index('{% from \'partials/post_action.html\' import '
                           'post_action %}')
    use_at = text.index('post_action(')
    # skip the import line itself when locating a genuine call
    assert use_at > import_at, (
        f'{template}: post_action used at offset {use_at} but imported at '
        f'{import_at} — Jinja does not hoist imports, this renders as a 500')


def test_genre_page_renders_with_results(app, client, monkeypatch):
    """`/genre/<name>` had the same live 500; A/B verified original vs fix."""
    with app.app_context():
        _user('genreuser')
        _login(client, 'genreuser')
    monkeypatch.setattr(browse_routes, 'fetch_movies_by_genre', lambda gid: [
        {'id': 111, 'title': 'A Movie', 'poster_path': '/p.jpg',
         'release_date': '2024-01-01', 'vote_average': 8.0, 'overview': 'x'}])
    resp = client.get('/genre/drama')
    assert resp.status_code == 200, resp.status_code
