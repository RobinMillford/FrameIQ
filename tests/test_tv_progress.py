"""Feature 02 — Best-in-class TV Progress + Continue Watching.

Covers the paused status state machine, first-class next-episode resolution,
the unfinished-shows shelf, and TV continue-watching rail entries, plus
regression coverage for existing episode/season/status behavior.

TMDb calls are mocked — no external services are required.
"""
from datetime import date, datetime, timedelta

import pytest

from models import (
    ContinueWatchingItem, MediaItem, TVEpisodeWatch, TVShowProgress,
    UpcomingEpisode, WatchProgress, db,
)
import api.continue_watching as cw
from routes.tv_tracking import _compute_next_episode_cached


@pytest.fixture(autouse=True)
def _clean_tv_rows(db, sample_user):
    """Clean TV rows before sample_user teardown (depends on it for ordering)."""
    cw._memo.clear()
    yield
    TVEpisodeWatch.query.delete()
    TVShowProgress.query.delete()
    UpcomingEpisode.query.delete()
    WatchProgress.query.delete()
    ContinueWatchingItem.query.delete()
    db.session.commit()
    cw._memo.clear()


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
    """Intent-based: the show appears on the rail at the exact started
    episode; finishing it promotes the next valid episode."""
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    cw.start_item(sample_user.id, 'tv', SHOW_ID, season=1, episode=2)
    entries = _unfinished_entries(sample_user)
    assert len(entries) == 1
    e = entries[0]
    assert e['media_type'] == 'tv'
    assert e['watch_url'] == f'/watch/tv/{SHOW_ID}/1/2'
    assert 'S1E2' in e['label']


def test_continue_watching_finish_promotes_next(auth_client, sample_user,
                                                monkeypatch):
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    cw.start_item(sample_user.id, 'tv', SHOW_ID, season=1, episode=2)

    cw.finish_tv_episode(sample_user.id, SHOW_ID, 1, 2)

    entries = _unfinished_entries(sample_user)
    assert len(entries) == 1
    e = entries[0]
    assert e['watch_url'] == f'/watch/tv/{SHOW_ID}/1/3'
    assert 'S1E3' in e['label']


def test_continue_watching_exact_episode_preserved(auth_client, sample_user,
                                                   monkeypatch):
    """The exact started season/episode is preserved — never S1E1, never a
    substituted episode, no playback percentage in the label."""
    _mock_show(monkeypatch)
    _track(sample_user, watched=2)
    _watch(sample_user, 1, 1)
    _watch(sample_user, 1, 2)
    cw.start_item(sample_user.id, 'tv', SHOW_ID, season=1, episode=2)
    e = _unfinished_entries(sample_user)[0]
    assert e['watch_url'] == f'/watch/tv/{SHOW_ID}/1/2'
    assert 'S1E2' in e['label']
    assert '%' not in e['label']


def test_continue_watching_requires_start(auth_client, sample_user,
                                          monkeypatch):
    """A tracked-but-never-started show is NOT on the rail: Continue
    Watching means 'started but not finished', not 'tracked'"""
    _mock_show(monkeypatch)
    _track(sample_user, watched=1)
    _watch(sample_user, 1, 1)
    assert _unfinished_entries(sample_user) == []


def test_continue_watching_paused_show_still_appears(auth_client, sample_user,
                                                     monkeypatch):
    """A paused show the user actually started still appears at its exact
    episode — pausing must not hide or reset progress."""
    _mock_show(monkeypatch)
    _track(sample_user, status='paused', watched=1)
    _watch(sample_user, 1, 1)
    cw.start_item(sample_user.id, 'tv', SHOW_ID, season=1, episode=2)
    e = _unfinished_entries(sample_user)[0]
    assert e['watch_url'] == f'/watch/tv/{SHOW_ID}/1/2'


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


# ── Unfinished shows: canonical metadata hydration ────────────────────────────
# Data ownership: TVShowProgress = tracking state, MediaItem = local metadata
# cache, UpcomingEpisode = air state, TMDb = fallback when local cache missing.

def _track_show(user, show_id, status='watching', watched=2, total=7):
    p = TVShowProgress(user_id=user.id, show_id=show_id, status=status,
                       watched_episodes=watched, total_episodes=total)
    db.session.add(p)
    db.session.commit()
    return p


def _unfinished_payload(client):
    r = client.get('/api/tv/unfinished-shows')
    assert r.status_code == 200
    return r.get_json()['shows']


def test_metadata_from_mediaitem_without_upcoming_row(auth_client, sample_user):
    """Show 2: tracked show with a MediaItem but NO UpcomingEpisode row still
    gets title/poster (the old UpcomingEpisode-only path rendered
    'Unknown show' here)."""
    _track_show(sample_user, 2316)
    db.session.add(MediaItem(tmdb_id=2316, media_type='tv',
                             title='The Office', poster_path='/office.jpg'))
    db.session.commit()

    shows = _unfinished_payload(auth_client)
    card = next(s for s in shows if s['show_id'] == 2316)
    assert card['name'] == 'The Office'
    assert card['poster_path'] == '/office.jpg'


def test_metadata_falls_back_to_cached_tmdb_and_persists(
        auth_client, sample_user, monkeypatch):
    """Show 3+4: no MediaItem row → cached-TMDb fallback resolves title/poster
    and persists a MediaItem so a second request needs no TMDb call."""
    _track_show(sample_user, 17287)
    calls = []

    def fake_show(show_id, **kw):
        calls.append(show_id)
        return {
            'id': show_id,
            'name': 'Party Down',
            'poster_path': 'https://image.tmdb.org/t/p/w500/party.jpg',
            'seasons': SEASONS,
        }

    monkeypatch.setattr(
        'routes.tv_tracking.fetch_tv_show_details', fake_show)

    shows = _unfinished_payload(auth_client)
    card = shows[0]
    assert card['name'] == 'Party Down'
    # Full TMDb URL normalized to the raw path MediaItem stores.
    assert card['poster_path'] == '/party.jpg'

    # Persisted to the local cache, no duplicate rows.
    m = MediaItem.query.filter_by(tmdb_id=17287, media_type='tv').one()
    assert m.title == 'Party Down'
    assert m.poster_path == '/party.jpg'

    # Second request: metadata served from the persisted MediaItem — the
    # hydration path makes zero TMDb calls (any remaining call is the
    # preserved next-episode resolution, not metadata).
    db.session.expire_all()
    calls.clear()
    shows2 = _unfinished_payload(auth_client)
    assert shows2[0]['name'] == 'Party Down'
    m2 = MediaItem.query.filter_by(tmdb_id=17287, media_type='tv').one()
    assert m2.title == 'Party Down'
    assert MediaItem.query.filter_by(tmdb_id=17287).count() == 1


def test_metadata_tmdb_failure_degrades_card_only(
        auth_client, sample_user, monkeypatch):
    """Show 5+6: TMDb fallback failure keeps the API 200 and the shelf intact;
    that card alone degrades (name None → frontend generic fallback).
    Raw TMDb IDs are never rendered as titles."""
    _track_show(sample_user, 1)
    db.session.add(MediaItem(tmdb_id=2, media_type='tv',
                             title='Cached Show', poster_path='/c.jpg'))
    _track_show(sample_user, 2)
    db.session.commit()

    def boom(show_id, **kw):
        raise LookupError(f'TV show {show_id} was not found')

    monkeypatch.setattr(
        'routes.tv_tracking.fetch_tv_show_details', boom)

    r = auth_client.get('/api/tv/unfinished-shows')
    assert r.status_code == 200
    shows = r.get_json()['shows']
    assert len(shows) == 2
    broken = next(s for s in shows if s['show_id'] == 1)
    cached = next(s for s in shows if s['show_id'] == 2)
    assert broken['name'] is None  # frontend shows generic fallback
    assert cached['name'] == 'Cached Show'
    # No MediaItem row fabricated from a failed lookup.
    assert MediaItem.query.filter_by(tmdb_id=1, media_type='tv').count() == 0


def test_mediaitem_of_other_type_ignored(
        auth_client, sample_user, monkeypatch):
    """tmdb_id identity includes media_type: a movie MediaItem must not
    satisfy TV metadata lookup."""
    _track_show(sample_user, 555)
    db.session.add(MediaItem(tmdb_id=555, media_type='movie',
                             title='A Movie', poster_path='/m.jpg'))
    db.session.commit()

    monkeypatch.setattr(
        'routes.tv_tracking.fetch_tv_show_details',
        lambda sid, **kw: {'id': sid, 'name': 'Real Show',
                           'poster_path': '/real.jpg', 'seasons': SEASONS})

    shows = _unfinished_payload(auth_client)
    card = shows[0]
    assert card['name'] == 'Real Show'
    # The movie row is untouched — no duplicate, no overwrite.
    m = MediaItem.query.filter_by(tmdb_id=555, media_type='movie').one()
    assert m.title == 'A Movie'


def test_multiple_shows_single_response_hydrated(
        auth_client, sample_user, monkeypatch):
    """Shows 8+7: multiple unfinished shows return through ONE response with
    canonical titles; mixed local-cache/fallback sources; no raw-ID titles."""
    _track_show(sample_user, 100)
    _track_show(sample_user, 200)
    _track_show(sample_user, 300)
    db.session.add(MediaItem(tmdb_id=100, media_type='tv',
                             title='Local Show', poster_path='/local.jpg'))
    db.session.commit()

    def fake_show(show_id, **kw):
        return {'id': show_id, 'name': f'TMDb Show {show_id}',
                'poster_path': f'/{show_id}.jpg', 'seasons': SEASONS}

    monkeypatch.setattr(
        'routes.tv_tracking.fetch_tv_show_details', fake_show)

    shows = _unfinished_payload(auth_client)
    assert {s['show_id'] for s in shows} == {100, 200, 300}
    by_id = {s['show_id']: s for s in shows}
    assert by_id[100]['name'] == 'Local Show'
    assert by_id[200]['name'] == 'TMDb Show 200'
    assert by_id[300]['name'] == 'TMDb Show 300'
    assert all(s['name'] for s in shows)  # never a raw numeric ID title


def test_progress_and_next_episode_unchanged_by_metadata_fix(
        auth_client, sample_user, monkeypatch):
    """Shows 9-14: metadata hydration does not disturb progress values,
    next-episode validation (no naive E+1), unaired exclusion, watch_url,
    paused/watching semantics, or the poster onerror fallback contract."""
    _mock_show(monkeypatch)  # S1: 4 eps, S2: 3 eps — S1E5 does NOT exist
    MediaItem.query.filter_by(tmdb_id=SHOW_ID, media_type='tv').delete()
    _track_show(sample_user, SHOW_ID, status='paused', watched=4, total=7)
    _watch(sample_user, 1, 1)
    _watch(sample_user, 1, 2)
    _watch(sample_user, 1, 3)
    _watch(sample_user, 1, 4)
    db.session.add(MediaItem(tmdb_id=SHOW_ID, media_type='tv',
                             title='Test Show', poster_path='/test.jpg'))
    db.session.commit()

    s = _unfinished_payload(auth_client)[0]
    assert s['status'] == 'paused'
    assert s['watched_episodes'] == 4
    assert s['total_episodes'] == 7
    assert s['progress_percent'] == pytest.approx(4 / 7 * 100, abs=0.1)
    assert s['last_episode'] == {'season': 1, 'episode': 4}
    # Crosses into S2E1 — naive E+1 would emit the nonexistent S1E5.
    assert s['next_episode'] == {'season': 2, 'episode': 1}
    assert s['watch_url'] == f'/watch/tv/{SHOW_ID}/2/1'
    assert s['name'] == 'Test Show'
    assert s['poster_path'] == '/test.jpg'
