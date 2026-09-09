"""
Tests for watch routes — watch-intent START on page open, and the
intent-based Continue Watching endpoints (start/finish/remove).

Contract: opening a watch page records START (idempotent); "✓ Finished"
records canonical watched state and removes the item; "× Remove" hides the
item WITHOUT marking it watched. No playback telemetry is involved.
"""
import pytest

from models import ContinueWatchingItem
import api.continue_watching as cw


@pytest.fixture(autouse=True)
def _clean_cw_state(db):
    """Purge CW/diary/media rows around each test — the shared in-memory DB
    reuses user rowids, and orphaned diary rows would leak watched-state."""
    from models import DiaryEntry, MediaItem
    cw._memo.clear()
    yield
    DiaryEntry.query.delete(synchronize_session=False)
    MediaItem.query.delete(synchronize_session=False)
    ContinueWatchingItem.query.delete()
    db.session.commit()
    cw._memo.clear()


@pytest.fixture
def cw_user(db, app):
    from models import User
    with app.app_context():
        u = User(username='cwwatcher', email='cwwatcher@example.com',
                 email_verified=True)
        u.set_password('WatchPass1')
        db.session.add(u)
        db.session.commit()
        yield u
        # Raw-SQL deletes bypass ORM cascade; diary/media must go too so an
        # orphaned diary row can't leak watched-state via reused user rowids.
        from sqlalchemy import text
        uid = u.id
        db.session.execute(
            text("DELETE FROM continue_watching_item WHERE user_id = :uid"),
            {"uid": uid})
        db.session.execute(text("DELETE FROM diary_entry WHERE user_id = :uid"),
                           {"uid": uid})
        db.session.execute(
            text("DELETE FROM media_item WHERE id NOT IN "
                 "(SELECT DISTINCT media_id FROM diary_entry)"))
        db.session.execute(text('DELETE FROM "user" WHERE id = :uid'),
                           {"uid": uid})
        db.session.commit()


@pytest.fixture
def cw_client(client, cw_user):
    client.post('/login', data={'username': 'cwwatcher',
                                'password': 'WatchPass1'})
    return client


# ── START on watch-page open ─────────────────────────────────────────────────

class TestWatchPageStart:
    def test_opening_movie_watch_page_records_start(self, cw_client, cw_user,
                                                    db, app, monkeypatch):
        # Patch the real import source: routes.watch imports fetch_movie_details
        # inside the function from api.tmdb_client.
        monkeypatch.setattr(
            "api.tmdb_client.fetch_movie_details",
            lambda mid, **kw: {'id': mid, 'title': 'Test Movie',
                               'poster_path': '/p.jpg', 'overview': '',
                               'release_date': '', 'genres': [],
                               'vote_average': 0, 'recommendations': []})
        r = cw_client.get('/watch/movie/321321')
        assert r.status_code == 200
        with app.app_context():
            row = ContinueWatchingItem.query.filter_by(
                user_id=cw_user.id, media_type='movie',
                tmdb_id=321321).first()
            assert row is not None

    def test_reopening_movie_does_not_duplicate(self, cw_client, cw_user, db,
                                                app, monkeypatch):
        monkeypatch.setattr(
            "api.tmdb_client.fetch_movie_details",
            lambda mid, **kw: {'id': mid, 'title': 'Test Movie',
                               'poster_path': None, 'overview': '',
                               'release_date': '', 'genres': [],
                               'vote_average': 0, 'recommendations': []})
        cw_client.get('/watch/movie/321321')
        cw_client.get('/watch/movie/321321')
        with app.app_context():
            rows = ContinueWatchingItem.query.filter_by(
                user_id=cw_user.id, tmdb_id=321321).all()
            assert len(rows) == 1

    def test_opening_tv_episode_records_exact_position(self, cw_client,
                                                       cw_user, db, app,
                                                       monkeypatch):
        monkeypatch.setattr(
            "api.tmdb_client.fetch_tv_show_details",
            lambda sid, **kw: {'id': sid, 'name': 'Test Show', 'seasons': [],
                               'poster_path': None, 'overview': '',
                               'status': '', 'number_of_seasons': 0,
                               'vote_average': 0})
        r = cw_client.get('/watch/tv/17287/2/4')
        assert r.status_code == 200
        with app.app_context():
            row = ContinueWatchingItem.query.filter_by(
                user_id=cw_user.id, tmdb_id=17287,
                season=2, episode=4).first()
            assert row is not None

    def test_watch_page_start_failure_never_breaks_page(self, cw_client,
                                                        monkeypatch):
        """A CW-write failure must not 500 the watch page."""
        monkeypatch.setattr(
            cw, "start_item",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(
            "api.tmdb_client.fetch_movie_details",
            lambda mid, **kw: {'id': mid, 'title': 'T', 'poster_path': None,
                               'overview': '', 'release_date': '',
                               'genres': [], 'vote_average': 0,
                               'recommendations': []})
        r = cw_client.get('/watch/movie/4242')
        assert r.status_code == 200

    def test_anonymous_watch_page_does_not_record(self, client, db, app,
                                                  monkeypatch):
        monkeypatch.setattr(
            "api.tmdb_client.fetch_movie_details",
            lambda mid, **kw: {'id': mid, 'title': 'T', 'poster_path': None,
                               'overview': '', 'release_date': '',
                               'genres': [], 'vote_average': 0,
                               'recommendations': []})
        r = client.get('/watch/movie/603')
        assert r.status_code == 200
        with app.app_context():
            assert ContinueWatchingItem.query.count() == 0


# ── Continue Watching endpoints ──────────────────────────────────────────────

class TestContinueWatchingAPI:
    def test_list_redirects_when_unauthenticated(self, client):
        r = client.get('/api/continue-watching')
        assert r.status_code in (401, 302)

    def test_list_empty_for_new_user(self, cw_client):
        r = cw_client.get('/api/continue-watching')
        assert r.status_code == 200
        assert r.get_json()['items'] == []

    def test_start_endpoint_idempotent(self, cw_client):
        for _ in range(2):
            r = cw_client.post('/api/continue-watching/movie/603/start',
                               json={'title': 'The Matrix'})
            assert r.status_code == 200
        r = cw_client.get('/api/continue-watching')
        items = r.get_json()['items']
        assert len([i for i in items if i['tmdb_id'] == 603]) == 1

    def test_finish_movie_requires_auth(self, client):
        r = client.post('/api/continue-watching/movie/603/finish')
        assert r.status_code in (401, 302)

    def test_remove_movie_hides_without_watched(self, cw_client, cw_user, db,
                                                app):
        cw_client.post('/api/continue-watching/movie/603/start',
                       json={'title': 'The Matrix'})
        r = cw_client.post('/api/continue-watching/movie/603/remove')
        assert r.status_code == 200
        assert r.get_json()['removed'] is True
        # NOT marked watched
        with app.app_context():
            from models import DiaryEntry
            assert DiaryEntry.query.filter_by(user_id=cw_user.id).count() == 0
        # And gone from the shelf
        items = cw_client.get('/api/continue-watching').get_json()['items']
        assert [i for i in items if i['tmdb_id'] == 603] == []

    def test_user_isolation_user_a_cannot_touch_user_b(self, cw_client, db,
                                                       app):
        """User A's remove must not delete User B's item."""
        cw_client.post('/api/continue-watching/movie/777/start',
                       json={'title': 'A Movie'})
        # Create user B with the same item
        from models import User
        other = User(username='otheruser', email='other@example.com',
                     email_verified=True)
        other.set_password('OtherPass1')
        db.session.add(other)
        db.session.commit()
        cw.start_item(other.id, 'movie', 777, title='A Movie')

        cw_client.post('/api/continue-watching/movie/777/remove')

        with app.app_context():
            rows = ContinueWatchingItem.query.filter_by(tmdb_id=777).all()
            assert len(rows) == 1
            assert rows[0].user_id == other.id


# ── Legacy endpoints kept for compatibility ──────────────────────────────────

class TestWatchHistory:
    def test_history_redirects_when_unauthenticated(self, client):
        r = client.get('/api/watch/history')
        assert r.status_code in (401, 302)

    def test_history_ok_for_user(self, cw_client):
        r = cw_client.get('/api/watch/history')
        assert r.status_code == 200
        assert 'items' in r.get_json()
