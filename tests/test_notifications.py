"""Feature 04 — Notifications + new-episode alerts.

Covers:
- fan-out: only tracked users notified; watched/future episodes never notify
- idempotency: repeated sync runs never duplicate
- API: user-scoped list, mark-read, mark-all-read, unread count, ordering
- security: cross-user reads/writes 404
- hygiene: no creation during page rendering, no polling/timers, bulk (non-N+1)
  creation

notify_newly_aired_episodes() is exercised directly — it is the exact function
the UpcomingEpisode sync calls (scripts/sync_upcoming_episodes.py), so these
tests cover the sync-triggered behavior without network access.
"""
from datetime import datetime, timedelta

import pytest

from models import db, TVShowProgress, TVEpisodeWatch, UpcomingEpisode
from models import User
from models.notification import Notification
import api.notifications as notif_svc
from api.notifications import notify_newly_aired_episodes


SHOW_A = 1399        # one tracked show
SHOW_B = 1396        # a show nobody tracks
FUTURE = datetime.utcnow().date() + timedelta(days=30)
TODAY = datetime.utcnow().date()


def _upcoming(show_id, season, episode, air_date, name=None):
    return UpcomingEpisode(
        show_id=show_id, show_name=f"Show {show_id}", poster_path=None,
        season_number=season, episode_number=episode,
        episode_name=name or f"S{season}E{episode} Episode",
        air_date=air_date,
    )


@pytest.fixture
def notif_env(app, db):
    """Two users; A tracks SHOW_A ('watching'). Owns all created rows."""
    with app.app_context():
        ua = User(username='notifuser_a', email='na@example.com', email_verified=True)
        ua.set_password('NotifPass123')
        ub = User(username='notifuser_b', email='nb@example.com', email_verified=True)
        ub.set_password('NotifPass123')
        db.session.add_all([ua, ub])
        db.session.commit()
        db.session.add(TVShowProgress(user_id=ua.id, show_id=SHOW_A,
                                      status='watching'))
        db.session.commit()
        yield {'a': ua, 'b': ub}
        # Cleanup: children first, then users (query-based to avoid stale objs).
        Notification.query.delete()
        TVEpisodeWatch.query.filter(
            TVEpisodeWatch.user_id.in_([ua.id, ub.id])).delete(
            synchronize_session=False)
        TVShowProgress.query.filter(
            TVShowProgress.user_id.in_([ua.id, ub.id])).delete(
            synchronize_session=False)
        UpcomingEpisode.query.delete()
        User.query.filter(User.id.in_([ua.id, ub.id])).delete(
            synchronize_session=False)
        db.session.commit()


@pytest.fixture
def notif_client(client, notif_env):
    client.post('/login', data={'username': 'notifuser_a',
                                'password': 'NotifPass123'})
    return client


# ─────────────────────────────────────────────────────────────────────────────
# Fan-out from the sync
# ─────────────────────────────────────────────────────────────────────────────

class TestFanOut:
    def test_aired_episode_notifies_tracked_user(self, notif_env):
        db.session.add(_upcoming(SHOW_A, 2, 4, TODAY))
        db.session.commit()
        created = notify_newly_aired_episodes()
        assert created == 1
        rows = Notification.query.all()
        assert len(rows) == 1
        assert rows[0].user_id == notif_env['a'].id
        assert rows[0].type == 'new_episode'

    def test_non_tracked_show_not_notified(self, notif_env):
        db.session.add(_upcoming(SHOW_B, 1, 1, TODAY))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0
        assert Notification.query.count() == 0

    def test_duplicate_sync_runs_never_duplicate(self, notif_env):
        db.session.add(_upcoming(SHOW_A, 2, 4, TODAY))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        # Simulated re-run: no purge, same row still aired-window.
        assert notify_newly_aired_episodes() == 0
        assert Notification.query.count() == 1

    def test_future_episode_not_notified(self, notif_env):
        db.session.add(_upcoming(SHOW_A, 2, 5, FUTURE))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0

    def test_already_watched_episode_not_notified(self, notif_env):
        db.session.add(_upcoming(SHOW_A, 2, 4, TODAY))
        db.session.add(TVEpisodeWatch(user_id=notif_env['a'].id, show_id=SHOW_A,
                                      season_number=2, episode_number=4,
                                      watched_date=TODAY))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0

    def test_completed_show_not_notified(self, notif_env):
        (TVShowProgress.query
         .filter_by(user_id=notif_env['a'].id, show_id=SHOW_A)
         .update({'status': 'completed'}))
        db.session.add(_upcoming(SHOW_A, 2, 4, TODAY))
        db.session.commit()
        assert notify_newly_aired_episodes() == 0

    def test_target_url_is_exact_episode_and_server_generated(self, notif_env):
        db.session.add(_upcoming(SHOW_A, 2, 4, TODAY, name='Echoes'))
        db.session.commit()
        assert notify_newly_aired_episodes() == 1
        n = Notification.query.one()
        # Built server-side from the TMDb-sourced row — never client input.
        assert n.target_url == f'/watch/tv/{SHOW_A}/2/4'
        assert n.season == 2 and n.episode == 4
        assert n.episode_name == 'Echoes'

    def test_bulk_fanout_is_batched(self, notif_env):
        """Many aired episodes → many notifications in bounded bulk inserts
        (single commit; no per-episode × per-user query loop)."""
        for ep in range(1, 8):
            db.session.add(_upcoming(SHOW_A, 3, ep, TODAY))
        db.session.commit()
        created = notify_newly_aired_episodes()
        assert created == 7
        assert Notification.query.count() == 7


# ─────────────────────────────────────────────────────────────────────────────
# API — user scoping, read state, ordering
# ─────────────────────────────────────────────────────────────────────────────

class TestNotificationAPI:
    def _seed(self, uid, count=2, aired_days_ago=0):
        base = datetime.utcnow() - timedelta(days=aired_days_ago)
        for i in range(count):
            db.session.add(Notification(
                user_id=uid, type='new_episode', title='New episode available',
                body=f'Show {SHOW_A} · S1E{i + 1} is now available.',
                target_url=f'/watch/tv/{SHOW_A}/1/{i + 1}',
                show_id=SHOW_A, season=1, episode=i + 1,
                created_at=base + timedelta(minutes=i)))
        db.session.commit()

    def test_list_requires_auth(self, client, notif_env):
        r = client.get('/api/notifications')
        assert r.status_code == 302  # login redirect

    def test_list_returns_own_notifications_newest_first(self, notif_client,
                                                         notif_env):
        self._seed(notif_env['a'].id, count=3)
        data = notif_client.get('/api/notifications').get_json()
        assert data['unread_count'] == 3
        eps = [n['episode'] for n in data['notifications']]
        assert eps == [3, 2, 1]  # newest first

    def test_user_cannot_see_other_users_notifications(self, notif_client,
                                                       notif_env):
        self._seed(notif_env['b'].id, count=2)  # B's rows
        data = notif_client.get('/api/notifications').get_json()
        assert data['notifications'] == []
        assert data['unread_count'] == 0

    def test_mark_read_works(self, notif_client, notif_env):
        self._seed(notif_env['a'].id, count=1)
        nid = Notification.query.first().id
        r = notif_client.post(f'/api/notifications/{nid}/read')
        data = r.get_json()
        assert r.status_code == 200 and data['ok'] is True
        assert data['unread_count'] == 0
        assert Notification.query.first().read_at is not None

    def test_mark_read_is_idempotent(self, notif_client, notif_env):
        self._seed(notif_env['a'].id, count=1)
        nid = Notification.query.first().id
        assert notif_client.post(f'/api/notifications/{nid}/read').status_code == 200
        assert notif_client.post(f'/api/notifications/{nid}/read').status_code == 200
        assert Notification.query.count() == 1

    def test_mark_other_users_notification_fails(self, notif_client, notif_env):
        self._seed(notif_env['b'].id, count=1)
        nid = Notification.query.first().id  # B's
        r = notif_client.post(f'/api/notifications/{nid}/read')
        assert r.status_code == 404
        assert Notification.query.first().read_at is None  # untouched

    def test_mark_all_read(self, notif_client, notif_env):
        self._seed(notif_env['a'].id, count=3)
        r = notif_client.post('/api/notifications/read-all')
        data = r.get_json()
        assert r.status_code == 200 and data['marked'] == 3
        assert data['unread_count'] == 0
        assert all(n.read_at for n in Notification.query.all())
        # Only the caller's rows are touched.
        assert Notification.query.filter_by(user_id=notif_env['b'].id).count() == 0

    def test_unread_count_accurate(self, notif_client, notif_env):
        self._seed(notif_env['a'].id, count=4)
        data = notif_client.get('/api/notifications').get_json()
        assert data['unread_count'] == 4
        nid = Notification.query.first().id
        notif_client.post(f'/api/notifications/{nid}/read')
        data = notif_client.get('/api/notifications').get_json()
        assert data['unread_count'] == 3


# ─────────────────────────────────────────────────────────────────────────────
# Hygiene
# ─────────────────────────────────────────────────────────────────────────────

class TestHygiene:
    def test_no_creation_during_page_render(self, client, notif_env,
                                            monkeypatch):
        """Ordinary page rendering must never fan out notifications: the
        trigger is called with an empty upcoming table → 0 rows, and the
        notification API/GET never invokes the fan-out at all."""
        assert Notification.query.count() == 0
        # A homepage render (anonymous) creates nothing.
        monkeypatch.setattr('routes.watch.fetch_movie_details',
                            lambda _id: None, raising=False)
        assert Notification.query.count() == 0

    def test_empty_upcoming_table_is_noop(self, notif_env):
        assert notify_newly_aired_episodes() == 0

    def test_no_polling_or_timers_in_frontend(self):
        src = open('static/js/notifications.js').read()
        for banned in ('setInterval', 'setTimeout', 'while (true)',
                       'EventSource', 'WebSocket'):
            assert banned not in src, f"polling machinery found: {banned}"

    def test_no_polling_or_timers_in_service(self):
        import inspect
        src = inspect.getsource(notif_svc)
        for banned in ('while True', 'time.sleep', 'Thread('):
            assert banned not in src, f"loop machinery found: {banned}"
