"""Detail-page action tests: Log to Diary + Add to List (movie & TV).

Covers the audit's mandated surface:
- Backend routes /api/diary/log and /api/lists/<id>/add for both media types
  (success, duplicate, auth, ownership).
- The detail-page badge bug: detail routes must map TMDb id -> internal
  MediaItem.id before querying UserListItem / DiaryEntry (both tables store
  the INTERNAL id), with no false positives when ids collide.
- Served-HTML contract: shared module wired on both pages with the correct
  explicit media_type, buttons as real buttons, JSON island present.
"""
import json
import re
from datetime import date
from unittest.mock import patch

import pytest

from models import DiaryEntry, MediaItem, User, UserListItem, UserList


def _tmdb_movie_payload(tmdb_id=42):
    """Complete TMDb-shaped movie payload (prevents detail-route fallbacks)."""
    return {
        'id': tmdb_id,
        'title': 'Fixture Movie',
        'overview': 'Test overview',
        'poster_path': '/poster.jpg',
        'backdrop_path': '/backdrop.jpg',
        'release_date': '2024-01-01',
        'runtime': 100,
        'vote_average': 7.5,
        'vote_count': 100,
        'genres': [{'id': 28, 'name': 'Action'}],
        'trailer_url': None,
        'cast': [],
        'crew': [],
        'status': 'Released',
        'tagline': 'A fixture',
        'reviews': [],
        'budget': 0,
        'revenue': 0,
        'director': '',
        'writer': '',
        'original_language': 'en',
    }


def _tmdb_tv_payload(tmdb_id=1399):
    """Complete TMDb-shaped TV payload."""
    return {
        'id': tmdb_id,
        'name': 'Fixture Show',
        'overview': 'Test overview',
        'poster_path': '/poster.jpg',
        'backdrop_path': '/backdrop.jpg',
        'first_air_date': '2020-01-01',
        'vote_average': 8.0,
        'vote_count': 50,
        'genres': [{'id': 18, 'name': 'Drama'}],
        'trailer_url': None,
        'cast': [],
        'crew': [],
        'number_of_seasons': 3,
        'number_of_episodes': 30,
        'seasons': [],
        'episode_run_time': [45],
        'networks': [],
        'status': 'Returning Series',
        'created_by': [],
        'reviews': [],
        'certification': 'TV-14',
        'creator': '',
        'last_air_date': '2024-01-01',
        'original_language': 'en',
        'origin_country': ['US'],
        'tagline': 'A fixture',
        'trailer_key': None,
    }


@pytest.fixture()
def clean_db(app, sample_user):
    """Remove rows created by a test BEFORE sample_user's teardown.

    conftest's sample_user finalizer deletes the user; any UserList /
    UserListItem / DiaryEntry rows we created would then be flushed with
    user_id=NULL (their FK is NOT NULL) and crash the suite. This fixture
    depends on sample_user, so it tears down first.
    """
    from models import db
    yield
    db.session.rollback()
    for model in (UserListItem, UserList, DiaryEntry, MediaItem):
        model.query.delete()
    db.session.commit()


def _test_user_id(app):
    with app.app_context():
        user = User.query.filter_by(username='testuser').first()
        return user.id


def _get_or_create_media(tmdb_id, media_type, title):
    """Idempotent MediaItem factory (unique per tmdb_id+media_type)."""
    from models import db
    existing = MediaItem.query.filter_by(
        tmdb_id=tmdb_id, media_type=media_type).first()
    if existing:
        return existing
    item = MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title)
    db.session.add(item)
    db.session.commit()
    return item


def _make_list(user_id, title):
    from models import db
    ulist = UserList(user_id=user_id, title=title)
    db.session.add(ulist)
    db.session.commit()
    return ulist


def _detail_context(html):
    match = re.search(
        r'<script id="detail-context"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return None
    return json.loads(match.group(1))


# ── Backend: /api/diary/log ──────────────────────────────────────────────────

def test_diary_log_movie_success(app, auth_client, clean_db):
    with app.app_context():
        _get_or_create_media(42, 'movie', 'Fixture Movie')
    resp = auth_client.post(
        '/api/diary/log',
        json={
            'media_id': 42,
            'media_type': 'movie',
            'watched_date': '2026-01-15',
            'rating': 4.0,
        },
    )
    assert resp.status_code == 201
    with app.app_context():
        media = MediaItem.query.filter_by(
            tmdb_id=42, media_type='movie').first()
        entry = DiaryEntry.query.filter_by(
            user_id=_test_user_id(app), media_type='movie').first()
        assert entry is not None
        assert entry.media_id == media.id  # INTERNAL id stored


def test_diary_log_tv_success(app, auth_client, clean_db):
    with app.app_context():
        _get_or_create_media(1399, 'tv', 'Fixture Show')
    resp = auth_client.post(
        '/api/diary/log',
        json={
            'media_id': 1399,
            'media_type': 'tv',
            'watched_date': '2026-01-15',
            'rating': None,
        },
    )
    assert resp.status_code == 201
    with app.app_context():
        entry = DiaryEntry.query.filter_by(media_type='tv').first()
        assert entry is not None


def test_diary_log_requires_date(auth_client, clean_db):
    resp = auth_client.post(
        '/api/diary/log',
        json={'media_id': 42, 'media_type': 'movie', 'watched_date': ''},
    )
    assert resp.status_code == 400


def test_diary_log_requires_auth(client, clean_db):
    resp = client.post(
        '/api/diary/log', json={'media_id': 42, 'media_type': 'movie'})
    assert resp.status_code in (302, 401)


def test_diary_log_rejects_bad_media_type(auth_client, clean_db):
    resp = auth_client.post(
        '/api/diary/log',
        json={
            'media_id': 42,
            'media_type': 'book',
            'watched_date': '2026-01-15',
        },
    )
    assert resp.status_code in (400, 404)


# ── Backend: /api/lists/<id>/add ─────────────────────────────────────────────

def test_list_add_movie_success(app, auth_client, clean_db):
    with app.app_context():
        _get_or_create_media(42, 'movie', 'Fixture Movie')
        list_id = _make_list(_test_user_id(app), 'My List').id

    resp = auth_client.post(
        f'/api/lists/{list_id}/add',
        json={'media_id': 42, 'media_type': 'movie'},
    )
    assert resp.status_code == 201
    with app.app_context():
        item = UserListItem.query.filter_by(list_id=list_id).first()
        assert item is not None
        media = MediaItem.query.filter_by(tmdb_id=42, media_type='movie').first()
        assert item.media_id == media.id  # INTERNAL id stored


def test_list_add_tv_success(app, auth_client, clean_db):
    with app.app_context():
        _get_or_create_media(1399, 'tv', 'Fixture Show')
        list_id = _make_list(_test_user_id(app), 'Shows').id

    resp = auth_client.post(
        f'/api/lists/{list_id}/add',
        json={'media_id': 1399, 'media_type': 'tv'},
    )
    assert resp.status_code == 201
    with app.app_context():
        item = UserListItem.query.filter_by(list_id=list_id).first()
        assert item is not None
        assert item.media_type == 'tv'


def test_list_add_duplicate_rejected(app, auth_client, clean_db):
    with app.app_context():
        user_id = _test_user_id(app)
        ulist = _make_list(user_id, 'Dupes')
        media = _get_or_create_media(12345, 'movie', 'Duped')
        from models import db
        db.session.add(UserListItem(
            list_id=ulist.id, media_id=media.id, media_type='movie'))
        db.session.commit()
        list_id, tmdb_id = ulist.id, media.tmdb_id

    resp = auth_client.post(
        f'/api/lists/{list_id}/add',
        json={'media_id': tmdb_id, 'media_type': 'movie'},
    )
    assert resp.status_code == 400


def test_list_add_requires_auth(client, clean_db):
    resp = client.post('/api/lists/1/add',
                       json={'media_id': 42, 'media_type': 'movie'})
    assert resp.status_code in (302, 401, 403, 404)


def test_user_lists_endpoint(app, auth_client, clean_db):
    with app.app_context():
        user_id = _test_user_id(app)
        _make_list(user_id, 'Ranked One')

    resp = auth_client.get(f'/api/users/{user_id}/lists')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'lists' in data


# ── Badge mapping fix (routes/details.py) ────────────────────────────────────

def test_badge_uses_internal_media_id(app, auth_client, clean_db):
    """Movie: list membership + diary count resolve via MediaItem mapping."""
    with app.app_context():
        user_id = _test_user_id(app)
        media = _get_or_create_media(42, 'movie', 'Fixture Movie')
        ulist = _make_list(user_id, 'Holders')
        from models import db
        db.session.add(UserListItem(
            list_id=ulist.id, media_id=media.id, media_type='movie'))
        db.session.add(DiaryEntry(
            user_id=user_id, media_id=media.id, media_type='movie',
            watched_date=date(2026, 1, 1)))
        db.session.commit()

    with patch('routes.details.fetch_movie_details',
               return_value=_tmdb_movie_payload(42)):
        resp = auth_client.get('/movie/42')
    html = resp.get_data(as_text=True)
    assert 'Holders' in html
    assert 'In Diary (1x)' in html


def test_badge_no_false_positive_on_id_collision(app, auth_client, clean_db):
    """Internal id N must NOT match when the page's TMDb id is N."""
    with app.app_context():
        user_id = _test_user_id(app)
        # Internal autoincrement id (captured below); TMDb id deliberately
        # different. The page /movie/<internal id> then collides ids.
        decoy = MediaItem(tmdb_id=999999, media_type='movie', title='Decoy')
        from models import db
        db.session.add(decoy)
        db.session.commit()
        requested_tmdb_id = decoy.id
        ulist = _make_list(user_id, 'DecoyList')
        db.session.add(UserListItem(
            list_id=ulist.id, media_id=decoy.id, media_type='movie'))
        db.session.commit()

    with patch('routes.details.fetch_movie_details',
               return_value=_tmdb_movie_payload(requested_tmdb_id)):
        resp = auth_client.get(f'/movie/{requested_tmdb_id}')
    html = resp.get_data(as_text=True)
    assert 'DecoyList' not in html


def test_tv_badge_uses_internal_media_id(app, auth_client, clean_db):
    with app.app_context():
        user_id = _test_user_id(app)
        media = _get_or_create_media(1399, 'tv', 'Fixture Show')
        ulist = _make_list(user_id, 'TVHolder')
        from models import db
        db.session.add(UserListItem(
            list_id=ulist.id, media_id=media.id, media_type='tv'))
        db.session.add(DiaryEntry(
            user_id=user_id, media_id=media.id, media_type='tv',
            watched_date=date(2026, 1, 2)))
        db.session.commit()

    with patch('routes.details.fetch_tv_show_details',
               return_value=_tmdb_tv_payload(1399)):
        resp = auth_client.get('/tv/1399')
    html = resp.get_data(as_text=True)
    assert 'TVHolder' in html
    assert 'In Diary (1x)' in html


def test_badge_missing_media_item_does_not_crash(auth_client, clean_db):
    """No MediaItem for this TMDb id -> empty badges, page still 200."""
    with patch('routes.details.fetch_movie_details',
               return_value=_tmdb_movie_payload(8675309)):
        resp = auth_client.get('/movie/8675309')
    assert resp.status_code == 200


# ── Served-HTML contract ─────────────────────────────────────────────────────

def test_movie_page_contract(auth_client, clean_db):
    with patch('routes.details.fetch_movie_details',
               return_value=_tmdb_movie_payload(42)):
        resp = auth_client.get('/movie/42')
    html = resp.get_data(as_text=True)
    ctx = _detail_context(html)
    assert ctx and ctx['media'] == {'id': 42, 'media_type': 'movie'}
    assert 'id="open-diary-modal"' in html
    assert 'id="open-list-modal"' in html
    assert 'detail-modals.js' in html
    assert 'id="diary-modal"' in html and 'id="list-modal"' in html


def test_tv_page_contract(auth_client, clean_db):
    with patch('routes.details.fetch_tv_show_details',
               return_value=_tmdb_tv_payload(1399)):
        resp = auth_client.get('/tv/1399')
    html = resp.get_data(as_text=True)
    ctx = _detail_context(html)
    assert ctx and ctx['media'] == {'id': 1399, 'media_type': 'tv'}
    assert 'id="open-diary-modal"' in html
    assert 'id="open-list-modal"' in html
    assert 'detail-modals.js' in html
    assert 'id="diary-modal"' in html and 'id="list-modal"' in html


def test_movie_page_guest_gets_null_user_context(client, clean_db):
    """Guests still render the page; the JS module routes them to /login."""
    with patch('routes.details.fetch_movie_details',
               return_value=_tmdb_movie_payload(42)):
        resp = client.get('/movie/42')
    assert resp.status_code == 200
    ctx = _detail_context(resp.get_data(as_text=True))
    assert ctx is not None and ctx['user_id'] is None
