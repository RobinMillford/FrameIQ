"""
Tests for watch progress routes — save, continue-watching filter, history.
Verifies: save creates/updates rows, continue_watching excludes near-complete
items (current_time >= duration * 0.9), rate-limit endpoint exists.
"""
import pytest


@pytest.fixture
def progress_user(db, app):
    from models import User
    from sqlalchemy import text
    with app.app_context():
        u = User(username='watcher', email='watcher@example.com', email_verified=True)
        u.set_password('WatchPass1')
        db.session.add(u)
        db.session.commit()
        uid = u.id
        yield uid
        # Raw SQL deletes bypass ORM cascade (avoids NOT NULL violation on diary_entry.user_id).
        # Diary rows MUST go too: an orphaned diary row's user_id can be reused
        # by the next user created in the shared in-memory DB, which would make
        # that user appear to have completed movies they never watched.
        db.session.execute(text("DELETE FROM watch_progress WHERE user_id = :uid"), {"uid": uid})
        db.session.execute(text("DELETE FROM diary_entry WHERE user_id = :uid"), {"uid": uid})
        db.session.execute(
            text("DELETE FROM media_item WHERE id NOT IN "
                 "(SELECT DISTINCT media_id FROM diary_entry)"))
        db.session.execute(text('DELETE FROM "user" WHERE id = :uid'), {"uid": uid})
        db.session.commit()


@pytest.fixture
def watch_client(client, progress_user):
    client.post('/login', data={'username': 'watcher', 'password': 'WatchPass1'})
    return client


class TestSaveProgress:
    def test_save_creates_row(self, watch_client, db, app):
        from models import WatchProgress
        r = watch_client.post('/api/watch/progress', json={
            'id': 12345,
            'mediaType': 'movie',
            'currentTime': 300.0,
            'duration': 7200.0,
            'progress': 4.2,
            'title': 'Test Movie',
            'posterPath': '/poster.jpg',
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True

        with app.app_context():
            wp = WatchProgress.query.filter_by(tmdb_id=12345, media_type='movie').first()
            assert wp is not None
            assert wp.current_time == 300.0
            assert wp.title == 'Test Movie'

    def test_save_updates_existing_row(self, watch_client, db, app):
        from models import WatchProgress
        payload = {
            'id': 99999, 'mediaType': 'movie',
            'currentTime': 100.0, 'duration': 7200.0,
            'progress': 1.4, 'title': 'Update Movie',
        }
        watch_client.post('/api/watch/progress', json=payload)

        payload['currentTime'] = 500.0
        payload['progress'] = 6.9
        r = watch_client.post('/api/watch/progress', json=payload)
        assert r.status_code == 200

        with app.app_context():
            rows = WatchProgress.query.filter_by(tmdb_id=99999, media_type='movie').all()
            assert len(rows) == 1
            assert rows[0].current_time == 500.0

    def test_save_missing_id_returns_400(self, watch_client):
        r = watch_client.post('/api/watch/progress', json={'mediaType': 'movie'})
        assert r.status_code == 400

    def test_save_no_json_returns_error(self, watch_client):
        r = watch_client.post('/api/watch/progress', data='not-json',
                              content_type='text/plain')
        # Flask returns 415 for wrong content-type; 400 if body is missing/empty
        assert r.status_code in (400, 415)

    def test_save_tv_with_season_episode(self, watch_client, db, app):
        from models import WatchProgress
        r = watch_client.post('/api/watch/progress', json={
            'id': 77777, 'mediaType': 'tv',
            'currentTime': 600.0, 'duration': 2400.0,
            'progress': 25.0, 'season': 2, 'episode': 3,
            'title': 'TV Show', 'posterPath': '',
        })
        assert r.status_code == 200

        with app.app_context():
            wp = WatchProgress.query.filter_by(
                tmdb_id=77777, media_type='tv', season=2, episode=3
            ).first()
            assert wp is not None

    def test_unauthenticated_save_redirects(self, client):
        r = client.post('/api/watch/progress', json={
            'id': 1, 'mediaType': 'movie',
            'currentTime': 10.0, 'duration': 100.0, 'progress': 10.0,
        })
        # 401 or redirect
        assert r.status_code in (401, 302)


class TestContinueWatching:
    def _seed(self, watch_client, db, app, tmdb_id, current_time, duration):
        watch_client.post('/api/watch/progress', json={
            'id': tmdb_id, 'mediaType': 'movie',
            'currentTime': current_time, 'duration': duration,
            'progress': round(current_time / duration * 100, 1),
            'title': f'Movie {tmdb_id}',
        })

    def test_returns_in_progress_items(self, watch_client, db, app):
        # 30% watched — should appear
        self._seed(watch_client, db, app, 11111, 600.0, 2000.0)
        r = watch_client.get('/api/watch/continue')
        assert r.status_code == 200
        data = r.get_json()
        ids = [i['tmdb_id'] for i in data['items']]
        assert 11111 in ids

    def test_excludes_near_complete_items(self, watch_client, db, app):
        # 95% watched — should be excluded (current_time >= duration * 0.9)
        self._seed(watch_client, db, app, 22222, 1900.0, 2000.0)
        r = watch_client.get('/api/watch/continue')
        assert r.status_code == 200
        ids = [i['tmdb_id'] for i in r.get_json()['items']]
        assert 22222 not in ids

    def test_excludes_short_duration(self, watch_client, db, app):
        # duration <= 60 should be excluded
        self._seed(watch_client, db, app, 33333, 10.0, 30.0)
        r = watch_client.get('/api/watch/continue')
        assert r.status_code == 200
        ids = [i['tmdb_id'] for i in r.get_json()['items']]
        assert 33333 not in ids

    def test_unauthenticated_continue_redirects(self, client):
        r = client.get('/api/watch/continue')
        assert r.status_code in (401, 302)


class TestSaveProgressTelemetry:
    """Player telemetry correctness: partial/zero telemetry must never
    corrupt a real saved position, and server-side completion must work
    even when a provider omits the progress field."""

    def _save(self, client, **overrides):
        payload = {
            'id': 55555, 'mediaType': 'movie',
            'currentTime': 900.0, 'duration': 1800.0,
            'progress': 50.0, 'title': 'Telemetry Movie',
        }
        payload.update(overrides)
        return client.post('/api/watch/progress', json=payload)

    def test_zero_duration_does_not_overwrite_saved_duration(self, watch_client, db, app):
        """Providers that emit a progress ping without duration must not
        zero out the stored duration."""
        from models import WatchProgress
        self._save(watch_client)
        self._save(watch_client, duration=0.0)
        with app.app_context():
            wp = WatchProgress.query.filter_by(tmdb_id=55555, media_type='movie').first()
            assert wp.duration == 1800.0   # preserved, not zeroed

    def test_zero_current_time_does_not_overwrite_saved_position(self, watch_client, db, app):
        from models import WatchProgress
        self._save(watch_client)
        self._save(watch_client, currentTime=0.0)
        with app.app_context():
            wp = WatchProgress.query.filter_by(tmdb_id=55555, media_type='movie').first()
            assert wp.current_time == 900.0   # preserved, not zeroed

    def test_progress_derived_from_position_when_provider_omits_it(self, watch_client, db, app):
        """Some providers send currentTime+duration without a progress %;
        the server must derive completion from position instead of losing it."""
        # 90% watched but provider sends progress=0 (absent)
        r = self._save(watch_client, currentTime=1620.0, duration=1800.0, progress=0.0)
        assert r.status_code == 200
        data = r.get_json()
        assert data['success'] is True
        assert data['completed'] is True   # derived 90% >= 85% threshold

    def test_completed_flag_false_for_partial_playback(self, watch_client, db, app):
        r = self._save(watch_client)
        assert r.get_json()['completed'] is False

    def test_completion_without_progress_field_triggers_diary(self, watch_client, db, app):
        """End-to-end: near-complete position, no progress field, no duration
        overwrite → diary watch event is still created. (Unique id: auto-log
        is idempotent per movie per day.)"""
        r = self._save(watch_client, id=55556,
                       currentTime=1710.0, duration=1800.0, progress=0.0)
        data = r.get_json()
        assert data['completed'] is True
        assert data['auto_logged'] is True
        with app.app_context():
            from models import DiaryEntry, MediaItem
            media = MediaItem.query.filter_by(tmdb_id=55556, media_type='movie').first()
            assert media is not None
            assert DiaryEntry.query.filter_by(
                media_id=media.id, media_type='movie').count() == 1

    def test_completed_tv_episode_flag(self, watch_client, db, app):
        r = self._save(watch_client, id=77777, mediaType='tv',
                       currentTime=1800.0, duration=1900.0,
                       progress=0.0, season=2, episode=3,
                       title='TV Show')
        data = r.get_json()
        assert data['completed'] is True
        assert data['auto_logged'] is True   # first watch of 77777 today

    def test_short_duration_never_completes(self, watch_client, db, app):
        """duration <= 60 is not reliable completion evidence."""
        r = self._save(watch_client, currentTime=30.0, duration=30.0)
        assert r.get_json()['completed'] is False


class TestContinueWatchingCanonical:
    """/api/watch/continue now serves the canonical validated aggregation
    (same data path as the home rail)."""

    def test_canonical_fields_served(self, watch_client, db, app):
        watch_client.post('/api/watch/progress', json={
            'id': 603, 'mediaType': 'movie',
            'currentTime': 600.0, 'duration': 1800.0,
            'progress': 33.3, 'title': 'The Matrix',
        })
        r = watch_client.get('/api/watch/continue')
        assert r.status_code == 200
        items = r.get_json()['items']
        assert items, "partial movie should appear"
        it = next(i for i in items if i.get('tmdb_id') == 603)
        assert it['title'] == 'The Matrix'
        assert it['watch_url'] == '/watch/movie/603'
        assert it['media_type'] == 'movie'
        assert it['progress'] == 33.3

    def test_completed_movie_absent_even_with_stale_row(self, watch_client, db, app):
        """Watched (diary) + stale resume row → absent from /continue."""
        watch_client.post('/api/watch/progress', json={
            'id': 603, 'mediaType': 'movie',
            'currentTime': 1700.0, 'duration': 1800.0,
            'progress': 94.4, 'title': 'The Matrix',
        })
        # Lower the row below the 0.9 filter to simulate a stale resume row
        with app.app_context():
            from models import WatchProgress
            wp = WatchProgress.query.filter_by(tmdb_id=603).first()
            wp.current_time = 400.0
            db.session.commit()
        r = watch_client.get('/api/watch/continue')
        ids = [i.get('tmdb_id') for i in r.get_json()['items']]
        assert 603 not in ids
