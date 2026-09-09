"""Feature 02 — Best-in-class TV Progress + Continue Watching.

Covers the paused status state machine, first-class next-episode resolution,
the unfinished-shows shelf, and TV continue-watching rail entries, plus
regression coverage for existing episode/season/status behavior.

TMDb calls are mocked — no external services are required.
"""
from datetime import date, datetime, timedelta

import pytest

from models import (
    TVEpisodeWatch, TVShowProgress, UpcomingEpisode, WatchProgress, db,
)
from routes.tv_tracking import _compute_next_episode_cached


@pytest.fixture(autouse=True)
def _clean_tv_rows(db, sample_user):
    """Clean TV rows before sample_user teardown (depends on it for ordering)."""
    yield
    TVEpisodeWatch.query.delete()
    TVShowProgress.query.delete()
    UpcomingEpisode.query.delete()
    WatchProgress.query.delete()
    db.session.commit()


SHOW_ID = 1399  # arbitrary TMDB show id

SEASONS = [
    {'season_number': 1, 'episode_count': 4, 'name': 'Season 1'},
    {'season_number': 2, 'episode_count': 3, 'name': 'Season 2'},
]


def _mock_show(monkeypatch, seasons=SEASONS, status='Ended'):
    """Mock TMDb show details at every call site (module bindings AND the
    call-time imports used by the canonical continue-watching builder)."""
    payload = {
        'id': None, 'name': 'Test Show', 'status': status,
        'number_of_seasons': len(seasons), 'number_of_episodes':
            sum(s['episode_count'] for s in seasons),
        'seasons': seasons,
    }

    def fake_show(show_id, **kw):
        d = dict(payload)
        d['id'] = show_id
        return d

    monkeypatch.setattr(
        'routes.tv_tracking.fetch_tv_show_details', fake_show)
    monkeypatch.setattr(
        'api.tmdb_client.fetch_tv_show_details', fake_show)


def _track(user, status='watching', watched=0, total=7):
    p = TVShowProgress(user_id=user.id, show_id=SHOW_ID, status=status,
                       watched_episodes=watched, total_episodes=total,
                       total_seasons=2)
    db.session.add(p)
    db.session.commit()
    return p


def _watch(user, season, episode, **kw):
    db.session.add(TVEpisodeWatch(user_id=user.id, show_id=SHOW_ID,
                                  season_number=season, episode_number=episode,
                                  **kw))
    db.session.commit()


# ── State machine: pause / resume ─────────────────────────────────────────────

def test_plan_to_watch_becomes_watching(auth_client, sample_user):
    _track(sample_user, status='plan_to_watch')
    r = auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                         json={'status': 'watching'})
    assert r.status_code == 200
    assert r.get_json()['progress']['status'] == 'watching'


def test_watching_becomes_paused(auth_client, sample_user):
    _track(sample_user)
    r = auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                         json={'status': 'paused'})
    assert r.status_code == 200
    assert r.get_json()['progress']['status'] == 'paused'


def test_paused_becomes_watching(auth_client, sample_user):
    _track(sample_user, status='paused')
    r = auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                         json={'status': 'watching'})
    assert r.status_code == 200
    assert r.get_json()['progress']['status'] == 'watching'


def test_pause_preserves_episode_history(auth_client, sample_user):
    _track(sample_user, watched=2)
    _watch(sample_user, 1, 1, watched_date=date(2026, 1, 1))
    _watch(sample_user, 1, 2, watched_date=date(2026, 1, 2))

    auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                     json={'status': 'paused'})

    p = TVShowProgress.query.filter_by(user_id=sample_user.id,
                                       show_id=SHOW_ID).first()
    assert p.status == 'paused'
    assert p.watched_episodes == 2
    assert TVEpisodeWatch.query.filter_by(
        user_id=sample_user.id, show_id=SHOW_ID).count() == 2
    # Resume keeps everything.
    auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                     json={'status': 'watching'})
    p = TVShowProgress.query.filter_by(user_id=sample_user.id,
                                       show_id=SHOW_ID).first()
    assert p.status == 'watching'
    assert p.watched_episodes == 2


def test_invalid_status_rejected(auth_client, sample_user):
    _track(sample_user)
    r = auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                         json={'status': 'bogus'})
    assert r.status_code == 400


def test_my_shows_status_filter_includes_paused(auth_client, sample_user):
    _track(sample_user, status='paused')
    r = auth_client.get('/api/tv/my-shows?status=paused')
    shows = r.get_json()['shows']
    assert len(shows) == 1 and shows[0]['status'] == 'paused'


# ── Next episode ──────────────────────────────────────────────────────────────

def test_next_episode_identified(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    r = auth_client.get(f'/api/tv/{SHOW_ID}/next-episode')
    body = r.get_json()
    assert body['tracked'] is True
    assert body['next_episode']['season'] == 1
    assert body['next_episode']['episode'] == 2
    assert body['next_episode']['aired'] is True


def test_next_episode_skips_watched(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=4)
    for e in range(1, 5):
        _watch(sample_user, 1, e)
    ne = _compute_next_episode_cached(sample_user.id, SHOW_ID)
    assert (ne['season'], ne['episode']) == (2, 1)


def test_future_episode_not_aired(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    db.session.add(UpcomingEpisode(
        show_id=SHOW_ID, show_name='Test Show', season_number=1,
        episode_number=2, air_date=date.today() + timedelta(days=5)))
    db.session.commit()
    r = auth_client.get(f'/api/tv/{SHOW_ID}/next-episode')
    ne = r.get_json()['next_episode']
    assert (ne['season'], ne['episode']) == (1, 2)
    assert ne['aired'] is False
    assert ne['air_date'] is not None


def test_completed_show_has_no_next_episode(auth_client, sample_user):
    _track(sample_user, status='completed', watched=7)
    r = auth_client.get(f'/api/tv/{SHOW_ID}/next-episode')
    assert r.get_json()['next_episode'] is None


def test_untracked_show_next_episode(auth_client, sample_user):
    r = auth_client.get(f'/api/tv/{SHOW_ID}/next-episode')
    assert r.get_json()['tracked'] is False


# ── Unfinished shows shelf ────────────────────────────────────────────────────

def test_unfinished_includes_watching_and_paused(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=2)
    p2 = TVShowProgress(user_id=sample_user.id, show_id=999,
                        status='paused', watched_episodes=1, total_episodes=10)
    db.session.add(p2)
    db.session.add(UpcomingEpisode(show_id=SHOW_ID, show_name='Test Show',
                                   season_number=1, episode_number=1,
                                   air_date=date(2020, 1, 1)))
    db.session.commit()
    r = auth_client.get('/api/tv/unfinished-shows')
    shows = r.get_json()['shows']
    ids = {s['show_id'] for s in shows}
    assert SHOW_ID in ids and 999 in ids
    first = next(s for s in shows if s['show_id'] == SHOW_ID)
    assert first['status'] == 'watching'
    assert first['progress_percent'] == pytest.approx(2 / 7 * 100, abs=0.1)


def test_unfinished_excludes_completed_and_dropped(auth_client, sample_user):
    _track(sample_user, status='completed', watched=7)
    db.session.add(TVShowProgress(user_id=sample_user.id, show_id=999,
                                  status='dropped', watched_episodes=1,
                                  total_episodes=10))
    db.session.commit()
    r = auth_client.get('/api/tv/unfinished-shows')
    assert r.get_json()['shows'] == []


def test_unfinished_sorted_by_last_watched(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    old = _track(sample_user, watched=1)
    old.last_watched = datetime(2026, 1, 1)
    new = TVShowProgress(user_id=sample_user.id, show_id=999, status='watching',
                         watched_episodes=1, total_episodes=10)
    new.last_watched = datetime(2026, 6, 1)
    db.session.add(new)
    db.session.commit()
    shows = auth_client.get('/api/tv/unfinished-shows').get_json()['shows']
    assert [s['show_id'] for s in shows] == [999, SHOW_ID]


def test_unfinished_next_episode_and_watch_url(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    db.session.add(UpcomingEpisode(show_id=SHOW_ID, show_name='Test Show',
                                   poster_path='/p.jpg', season_number=1,
                                   episode_number=1, air_date=date(2020, 1, 1)))
    db.session.commit()
    s = auth_client.get('/api/tv/unfinished-shows').get_json()['shows'][0]
    assert s['next_episode'] == {'season': 1, 'episode': 2}
    assert s['watch_url'] == f'/watch/tv/{SHOW_ID}/1/2'
    assert s['name'] == 'Test Show'


# ── Continue Watching rail entries ────────────────────────────────────────────

def _unfinished_entries(user):
    from routes.browse import _unfinished_tv_entries
    return _unfinished_tv_entries(user.id)


def test_continue_watching_next_action(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    entries = _unfinished_entries(sample_user)
    assert len(entries) == 1
    e = entries[0]
    assert e['media_type'] == 'tv'
    assert e['watch_url'] == f'/watch/tv/{SHOW_ID}/1/2'
    assert 'S1E2' in e['label']
    assert e['is_paused'] is False


def test_continue_watching_resume_partial_playback(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=2)
    _watch(sample_user, 1, 1)
    _watch(sample_user, 1, 2)
    db.session.add(WatchProgress(
        user_id=sample_user.id, tmdb_id=SHOW_ID, media_type='tv',
        season=1, episode=2, current_time=600, duration=1800,
        title='Test Show'))
    db.session.commit()
    e = _unfinished_entries(sample_user)[0]
    assert e['watch_url'] == f'/watch/tv/{SHOW_ID}/1/2?type=tv'
    assert 'S1E2' in e['label'] and '33%' in e['label']


def test_continue_watching_paused_flag(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, status='paused', watched=1)
    _watch(sample_user, 1, 1)
    e = _unfinished_entries(sample_user)[0]
    assert e['is_paused'] is True


# ── Authorization + regressions ───────────────────────────────────────────────

def test_user_a_cannot_modify_user_b_progress(auth_client, sample_user, db):
    other = _track(sample_user)  # sample_user owns it; auth_client is same user
    # Create a second user's progress and ensure status change hits only ours.
    from models import User
    u2 = User(username='tvother', email='tvother@example.com',
              email_verified=True)
    u2.set_password('TestPass1')
    db.session.add(u2)
    db.session.commit()
    p2 = TVShowProgress(user_id=u2.id, show_id=SHOW_ID, status='watching',
                        watched_episodes=1, total_episodes=7)
    db.session.add(p2)
    db.session.commit()

    r = auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                         json={'status': 'paused'})
    assert r.status_code == 200
    db.session.expire_all()
    assert TVShowProgress.query.filter_by(user_id=u2.id).first().status == 'watching'
    assert TVShowProgress.query.filter_by(
        user_id=sample_user.id).first().status == 'paused'
    db.session.delete(p2)
    db.session.delete(u2)
    db.session.commit()


def test_existing_episode_mark_unmark_intact(auth_client, sample_user,
                                             monkeypatch):
    _mock_show(monkeypatch)
    r = auth_client.post(f'/api/tv/{SHOW_ID}/episode/1/1/mark-watched', json={})
    assert r.status_code == 200
    assert r.get_json()['progress']['watched_episodes'] == 1

    r = auth_client.post(f'/api/tv/{SHOW_ID}/episode/1/1/unmark-watched')
    assert r.status_code == 200
    p = TVShowProgress.query.filter_by(user_id=sample_user.id,
                                       show_id=SHOW_ID).first()
    assert p.watched_episodes == 0


def test_existing_season_bulk_intact(auth_client, sample_user, monkeypatch):
    _mock_show(monkeypatch)
    monkeypatch.setattr(
        'routes.tv_tracking.cached_tmdb_request',
        lambda url, **kw: {'episodes': [
            {'episode_number': 1, 'name': 'Pilot'},
            {'episode_number': 2, 'name': 'Two'},
        ]})
    r = auth_client.post(f'/api/tv/{SHOW_ID}/season/1/mark-watched', json={})
    assert r.status_code == 200
    body = r.get_json()
    assert body['marked_episodes'] == 2
    assert body['progress']['watched_episodes'] == 2


def test_existing_status_transitions_still_work(auth_client, sample_user):
    _track(sample_user)
    for status in ('dropped', 'completed', 'watching'):
        r = auth_client.post(f'/api/tv/{SHOW_ID}/update-status',
                             json={'status': status})
        assert r.status_code == 200
    assert TVShowProgress.query.filter_by(
        user_id=sample_user.id).first().status == 'watching'


def test_existing_rows_survive_no_migration(auth_client, sample_user):
    """No schema migration needed: legacy rows keep working with 'paused'."""
    _track(sample_user, status='watching', watched=3)
    p = TVShowProgress.query.filter_by(user_id=sample_user.id).first()
    assert p.status == 'watching'  # legacy value untouched
    p.status = 'paused'
    db.session.commit()
    db.session.expire_all()
    assert TVShowProgress.query.filter_by(
        user_id=sample_user.id).first().status == 'paused'
