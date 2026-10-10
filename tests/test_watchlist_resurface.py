"""Tests for Feature F10 — watchlist resurfacing (api/watchlist_resurface.py).

Covered here:
  * the pure helpers (staleness, ordering, conservative date labelling);
  * the reachable behaviour the module adds (neglected titles resurface,
    recent ones do not, blocking feedback and canonical watched state win);
  * user isolation and query bounding.

Integration with the For You response (``resurfaced`` key, ``items`` contract
untouched, cold-start still returns the key) lives in test_for_you.py /
test_for_you_cold_start_homepage.py.
"""
import pathlib
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event

import api.for_you as fy
import api.watchlist_resurface as wr
from models import (
    DiaryEntry, MediaItem, MovieReleaseDate, RecommendationFeedback,
    TasteProfile, TVShowProgress, UpcomingEpisode, user_viewed,
)
from models.base import db
from models.recommendation_feedback import SURFACES
from models.user import User
from models.associations import user_watchlist

# Every staleness assertion is anchored to a fixed TODAY so the suite never
# depends on the wall clock.
TODAY = date(2026, 3, 1)
DOMAIN = 'resurface.test'


# ── suite convention: module-unique rows only, cleaned up afterwards ────────
@pytest.fixture(autouse=True)
def _clean_resurface_rows(app):
    # The For You response cache is module-level and process-wide. SQLite
    # REUSES primary keys after a delete, so two different users in this file
    # can end up with the same user_id — and therefore the same cache key
    # (user_id, region, limit, mode, profile_version, updated_at). Without this
    # clear, a cold-start result cached for one user is replayed to the next,
    # surfacing the previous user's watchlist titles.
    #
    # Not reachable in production: User.id is INTEGER PRIMARY KEY, i.e. a
    # Postgres sequence, which never reuses a value after a delete.
    fy._cache.clear()
    yield
    DiaryEntry.query.delete()
    db.session.execute(user_viewed.delete())
    db.session.execute(user_watchlist.delete())
    RecommendationFeedback.query.delete()
    UpcomingEpisode.query.delete()
    MovieReleaseDate.query.delete()
    # Must go before the User delete: tv_show_progress.user_id is a NOT NULL
    # FK and would otherwise leak into later files (whose teardown then fails
    # with "NOT NULL constraint failed: tv_show_progress.user_id").
    TVShowProgress.query.delete()
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
        yield _make_user('resurface')


def _days_ago(n):
    """A date_added anchored to TODAY, not to the wall clock."""
    return datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=n)


# ══════════════════════════════════════════════════════════════════════════
# Pure helpers
# ══════════════════════════════════════════════════════════════════════════

def test_neglect_days_counts_whole_days():
    added = (TODAY - timedelta(days=40)).isoformat() and \
        datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=40)
    assert wr.neglect_days(added, TODAY) == 40


def test_neglect_days_accepts_datetime_today_and_string_date():
    added = datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=10)
    assert wr.neglect_days(added, TODAY) == 10
    assert wr.neglect_days(added.date(), TODAY) == 10
    iso = (TODAY - timedelta(days=7)).isoformat()
    assert wr.neglect_days(iso, TODAY) == 7


def test_neglect_days_never_negative_for_future_dated_row():
    """A clock-skewed future date must not read as hugely stale."""
    future = datetime(TODAY.year, TODAY.month, TODAY.day) + timedelta(days=5)
    assert wr.neglect_days(future, TODAY) == 0


def test_neglect_days_none_for_unusable_input():
    assert wr.neglect_days(None, TODAY) is None
    assert wr.neglect_days('not-a-date', TODAY) is None
    assert wr.neglect_days(TODAY, None) is None


def test_is_stale_threshold_is_inclusive():
    assert wr.is_stale(TODAY - timedelta(days=wr.STALE_AFTER_DAYS), TODAY)
    assert not wr.is_stale(TODAY - timedelta(days=wr.STALE_AFTER_DAYS - 1),
                           TODAY)
    # NULL date_added is never "infinitely stale".
    assert not wr.is_stale(None, TODAY)


def test_priority_rank_orders_importance_and_tolerates_junk():
    assert wr.priority_rank('high') < wr.priority_rank('medium')
    assert wr.priority_rank('medium') < wr.priority_rank('low')
    assert wr.priority_rank('LOW') == wr.priority_rank('low')
    # Unknown / NULL sort last, never first and never raise.
    assert wr.priority_rank('urgent') > wr.priority_rank('low')
    assert wr.priority_rank(None) == wr.PRIORITY_UNKNOWN_RANK
    assert wr.priority_rank(3) == wr.PRIORITY_UNKNOWN_RANK


def test_priority_label_never_invents_a_level():
    assert wr.priority_label('high') == 'high'
    assert wr.priority_label(' High ') == 'high'
    assert wr.priority_label('urgent') is None
    assert wr.priority_label(None) is None


def test_resurface_sort_key_is_priority_then_neglect_then_identity():
    """Not a plain oldest-first list, and totally ordered (deterministic)."""
    def e(rank, days, tmdb, mt='movie'):
        return {'priority_rank': rank, 'neglect_days': days,
                'tmdb_id': tmdb, 'media_type': mt}
    entries = [
        e(1, 10, 5), e(0, 90, 4), e(0, 30, 6), e(1, 10, 3),
    ]
    ordered = [x['tmdb_id'] for x in sorted(entries, key=wr.resurface_sort_key)]
    # high+90 before high+30; then medium band, tie broken by tmdb_id.
    assert ordered == [4, 6, 3, 5]
    # Total order ⇒ stable across runs regardless of input order.
    assert [x['tmdb_id'] for x in
            sorted(list(reversed(entries)), key=wr.resurface_sort_key)] == ordered


def test_release_label_only_known_actionable_types():
    assert wr.release_label(3) == 'theatrical'
    assert wr.release_label('4') == 'digital'
    # Premiere / physical / tv / junk are unknown, never guessed at.
    for bad in (1, 2, 5, 6, 0, '', None, 'abc', 99):
        assert wr.release_label(bad) in (None, 'unknown')


def test_movie_release_context_past_date_says_released_not_streaming():
    """The central honesty guard: a past release date is NOT availability."""
    past = TODAY - timedelta(days=200)
    ctx = wr.movie_release_context(3, past, TODAY)
    assert ctx['upcoming'] is False
    text = ctx['text'].lower()
    assert 'released' in text
    for banned in ('stream', 'watch now', 'available', 'free'):
        assert banned not in text


def test_movie_release_context_future_theatrical_and_digital():
    future = TODAY + timedelta(days=30)
    theatrical = wr.movie_release_context(3, future, TODAY)
    digital = wr.movie_release_context(4, future, TODAY)
    assert theatrical['upcoming'] is True
    assert 'theaters' in theatrical['text'].lower()
    assert 'digital' in digital['text'].lower()


def test_movie_release_context_none_for_unusable_input():
    assert wr.movie_release_context(None, TODAY, TODAY) is None
    assert wr.movie_release_context(3, None, TODAY) is None
    assert wr.movie_release_context(99, TODAY, TODAY) is None


def test_episode_release_context_names_date_without_availability():
    ctx = wr.episode_release_context(TODAY + timedelta(days=2), TODAY)
    assert ctx['upcoming'] is True
    text = ctx['text'].lower()
    assert 'new episode' in text
    for banned in ('stream', 'watch now', 'available on', 'netflix'):
        assert banned not in text
    past = wr.episode_release_context(TODAY - timedelta(days=1), TODAY)
    assert past['upcoming'] is False
    assert wr.episode_release_context(None, TODAY) is None


def test_resurface_reason_states_only_what_is_known():
    text = wr.resurface_reason(40, 'high')['text']
    assert '40 days ago' in text
    assert 'high' in text
    one = wr.resurface_reason(1, None)['text']
    assert 'a day ago' in one
    months = wr.resurface_reason(200, 'low')['text']
    assert 'months ago' in months
    # Unrecognised priority ⇒ no invented label.
    assert 'priority' not in wr.resurface_reason(40, 'urgent')['text']


# ══════════════════════════════════════════════════════════════════════════
# Behaviour — needs the DB
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def saved_movie(app):
    with app.app_context():
        item = MediaItem(
            tmdb_id=4242, media_type='movie', title='Resurface Me',
            poster_path='/p.jpg', release_date=date(2019, 5, 1))
        db.session.add(item)
        db.session.commit()
        yield item.id
        db.session.rollback()


def _watch(user_id, media_id, media_type, days, priority=None):
    db.session.execute(user_watchlist.insert().values(
        user_id=user_id, media_id=media_id, media_type=media_type,
        date_added=_days_ago(days), priority=priority or 'medium'))
    db.session.commit()


def _feedback(user_id, tmdb_id, event, media_type='movie'):
    """RecommendationFeedback row. `source` is NOT NULL alongside surface."""
    db.session.add(RecommendationFeedback(
        user_id=user_id, media_id=tmdb_id, media_type=media_type,
        surface=SURFACES[0], source='test_resurface', event=event))
    db.session.commit()


def test_stale_title_resurfaces(app, user, saved_movie):
    _watch(user.id, saved_movie, 'movie', 45)
    cards = wr.resurface_cards(user.id, today=TODAY)
    assert len(cards) == 1
    assert cards[0]['tmdb_id'] == 4242
    assert cards[0]['source'] == 'watchlist_resurface'
    assert 'Saved 45 days ago' in cards[0]['reason']['text']


def test_recent_title_does_not_resurface(app, user, saved_movie):
    _watch(user.id, saved_movie, 'movie', 3)
    assert wr.resurface_cards(user.id, today=TODAY) == []


def test_missing_poster_or_tmdb_row_is_skipped_not_created(app, user, saved_movie):
    """A title whose MediaItem is gone simply never appears."""
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=999999, media_type='movie',
        date_added=_days_ago(60), priority='high'))
    db.session.commit()
    assert wr.resurface_cards(user.id, today=TODAY) == []


def test_watched_title_never_resurfaces(app, user, saved_movie):
    _watch(user.id, saved_movie, 'movie', 45)
    cards = wr.resurface_cards(
        user.id, watched_keys={('movie', 4242)}, today=TODAY)
    assert cards == []


def test_not_interested_feedback_suppresses_resurface(app, user, saved_movie):
    """The first consumer of the blocking feedback vocabulary."""
    _watch(user.id, saved_movie, 'movie', 45)
    _feedback(user.id, 4242, 'not_interested')
    assert wr.resurface_cards(user.id, today=TODAY) == []


def test_already_watched_feedback_suppresses_resurface(app, user, saved_movie):
    _watch(user.id, saved_movie, 'movie', 45)
    _feedback(user.id, 4242, 'already_watched')
    assert wr.resurface_cards(user.id, today=TODAY) == []


def test_interest_events_do_not_suppress(app, user, saved_movie):
    """Being shown or clicking a title is interest, not a dismissal."""
    _watch(user.id, saved_movie, 'movie', 45)
    for ev in ('impression', 'click', 'saved'):
        _feedback(user.id, 4242, ev)
    assert len(wr.resurface_cards(user.id, today=TODAY)) == 1


def test_user_isolation_other_users_watchlist_invisible(app, user, saved_movie):
    other = _make_user('resurface_other')
    _watch(other.id, saved_movie, 'movie', 60)
    assert wr.resurface_cards(user.id, today=TODAY) == []
    assert len(wr.resurface_cards(other.id, today=TODAY)) == 1


def test_limit_is_clamped_to_max(app, user):
    """Never more than RESURFACE_MAX, whatever the caller asks for."""
    for i in range(12):
        item = MediaItem(tmdb_id=7000 + i, media_type='movie',
                         title='Bulk %d' % i, poster_path='/p.jpg')
        db.session.add(item)
        db.session.commit()
        _watch(user.id, item.id, 'movie', 40)
    out = wr.resurface_cards(user.id, today=TODAY, limit=14)
    assert len(out) == wr.RESURFACE_MAX
    assert wr.resurface_cards(user.id, today=TODAY, limit=0) == []


def test_queries_are_bounded_and_not_per_title(app, user):
    """Query count must not scale with watchlist size (no N+1)."""
    for i in range(10):
        item = MediaItem(tmdb_id=8000 + i, media_type='movie',
                         title='Q %d' % i, poster_path='/p.jpg')
        db.session.add(item)
        db.session.commit()
        _watch(user.id, item.id, 'movie', 40)

    counter = {'n': 0}

    def _count(conn, cursor, statement, params, context, executemany):
        counter['n'] += 1

    event.listen(db.engine, 'before_cursor_execute', _count)
    try:
        wr.resurface_cards(user.id, today=TODAY)
    finally:
        event.remove(db.engine, 'before_cursor_execute', _count)
    # watchlist + feedback + release context. Not 10+.
    assert counter['n'] <= 5


def test_movie_release_context_used_when_cached(app, user, saved_movie):
    _watch(user.id, saved_movie, 'movie', 45)
    db.session.add(MovieReleaseDate(
        tmdb_id=4242, region='US', release_type=3,
        release_date=TODAY + timedelta(days=45)))
    db.session.commit()
    cards = wr.resurface_cards(user.id, today=TODAY)
    assert cards[0]['release']['label'] == 'theatrical'
    assert 'theaters' in cards[0]['release']['text'].lower()


def test_release_context_is_region_scoped(app, user, saved_movie):
    _watch(user.id, saved_movie, 'movie', 45)
    db.session.add(MovieReleaseDate(
        tmdb_id=4242, region='GB', release_type=3,
        release_date=TODAY + timedelta(days=45)))
    db.session.commit()
    # US user must not inherit a GB release date.
    us = wr.resurface_cards(user.id, region='US', today=TODAY)
    gb = wr.resurface_cards(user.id, region='GB', today=TODAY)
    assert 'release' not in us[0]
    assert 'release' in gb[0]


def test_tv_uses_episode_air_date_not_availability(app, user):
    show = MediaItem(tmdb_id=5555, media_type='tv', title='Old Show',
                     poster_path='/s.jpg')
    db.session.add(show)
    db.session.commit()
    _watch(user.id, show.id, 'tv', 90)
    db.session.add(UpcomingEpisode(
        show_id=5555, show_name='Old Show', season_number=3,
        episode_number=1,
        air_date=TODAY + timedelta(days=7)))
    db.session.commit()
    cards = wr.resurface_cards(user.id, today=TODAY)
    assert cards[0]['media_type'] == 'tv'
    assert cards[0]['release']['kind'] == 'episode_air_date'
    text = cards[0]['release']['text'].lower()
    assert 'new episode' in text
    for banned in ('stream', 'watch now', 'available on'):
        assert banned not in text


def test_past_tv_episode_is_not_used_as_upcoming_context(app, user):
    show = MediaItem(tmdb_id=5556, media_type='tv', title='Done Show',
                     poster_path='/s.jpg')
    db.session.add(show)
    db.session.commit()
    _watch(user.id, show.id, 'tv', 90)
    db.session.add(UpcomingEpisode(
        show_id=5556, show_name='Done Show', season_number=1,
        episode_number=1,
        air_date=TODAY - timedelta(days=30)))
    db.session.commit()
    assert 'release' not in wr.resurface_cards(user.id, today=TODAY)[0]


def test_priority_orders_resurface_output(app, user):
    made = {}
    for tmdb, priority in ((9001, 'low'), (9002, 'high')):
        item = MediaItem(tmdb_id=tmdb, media_type='movie',
                         title='P %d' % tmdb, poster_path='/p.jpg')
        db.session.add(item)
        db.session.commit()
        made[tmdb] = item.id
    # Both stale; the high-priority one was saved *more recently* and must
    # still win, proving priority outranks age.
    _watch(user.id, made[9001], 'movie', 200, priority='low')
    _watch(user.id, made[9002], 'movie', 30, priority='high')
    cards = wr.resurface_cards(user.id, today=TODAY)
    assert [c['tmdb_id'] for c in cards] == [9002, 9001]


def test_today_defaults_to_utc_now(app, user, saved_movie):
    """No explicit today ⇒ still uses real neglect, not a crash."""
    _watch(user.id, saved_movie, 'movie', 45)
    assert len(wr.resurface_cards(user.id)) == 1


def test_none_user_id_returns_empty(app):
    assert wr.resurface_cards(None, today=TODAY) == []


# ══════════════════════════════════════════════════════════════════════════
# For You integration
# ══════════════════════════════════════════════════════════════════════════

def _full_profile(user_id):
    db.session.add(TasteProfile(
        user_id=user_id, genre_weights={'Drama': 0.8, 'Thriller': 0.5},
        decade_weights={'2010s': 0.6}, director_affinity={},
        confidence=0.9, signal_count=12, distinct_title_count=6,
        profile_version=1))
    db.session.commit()


def test_get_for_you_includes_resurfaced(app, user, saved_movie, monkeypatch):
    _full_profile(user.id)
    _watch(user.id, saved_movie, 'movie', 45)
    monkeypatch.setattr(fy, '_generate_candidates', lambda *a, **k: [])
    monkeypatch.setattr(fy, '_TmdbBudget',
                        lambda: type('B', (), {'can_afford': lambda s: False})())
    out = fy.get_for_you(user.id)
    assert 'resurfaced' in out
    assert out['resurfaced'][0]['tmdb_id'] == 4242


def test_resurfaced_failure_degrades_without_breaking_rail(
        app, user, saved_movie, monkeypatch):
    """A resurfacing error must never take down the For You rail."""
    _full_profile(user.id)
    _watch(user.id, saved_movie, 'movie', 45)

    def boom(*a, **k):
        raise RuntimeError('resurface exploded')

    monkeypatch.setattr('api.watchlist_resurface.resurface_cards', boom)
    out = fy.get_for_you(user.id)
    assert out['resurfaced'] == []
    assert isinstance(out['items'], list)


def test_cold_start_still_returns_resurfaced_key(app, user, saved_movie):
    """Key always present so the response shape never varies."""
    _watch(user.id, saved_movie, 'movie', 45)
    out = fy.get_for_you(user.id)
    assert out['personalized'] is False
    assert out['items'] == []
    assert len(out['resurfaced']) == 1


def test_user_without_watchlist_gets_no_resurfacings(app, user):
    _full_profile(user.id)
    out = fy.get_for_you(user.id)
    assert out['resurfaced'] == []


# ══════════════════════════════════════════════════════════════════════════
# Watched-state safety (Feature F10 correction)
#
# The original implementation derived its watched set as
# ``exclude_keys - watchlisted_keys``. That difference is NOT a watched-state
# signal: ``exclude_keys`` also contains watchlist members, whose exclusion is
# only duplicate suppression. Subtracting the watchlist therefore erased every
# title that is BOTH watched AND still listed — the exact population this rail
# exists to surface. These tests pin the corrected behaviour.
# ══════════════════════════════════════════════════════════════════════════

def _log_movie(user_id, media_id, watched=TODAY):
    """DiaryEntry — the CANONICAL movie watch record (CLAUDE.md)."""
    db.session.add(DiaryEntry(
        user_id=user_id, media_id=media_id, media_type='movie',
        watched_date=watched, rating=4.0))
    db.session.commit()


def _tv_show(app, tmdb_id, title):
    show = MediaItem.query.filter_by(tmdb_id=tmdb_id).first()
    if show is None:
        show = MediaItem(tmdb_id=tmdb_id, media_type='tv', title=title,
                         poster_path='/s.jpg')
        db.session.add(show)
        db.session.commit()
    return show.id


def _progress(user_id, show_id, status, watched=0, total=0):
    """Upsert — tv_show_progress is UNIQUE(user_id, show_id)."""
    p = TVShowProgress.query.filter_by(
        user_id=user_id, show_id=show_id).first()
    if p is None:
        p = TVShowProgress(user_id=user_id, show_id=show_id)
        db.session.add(p)
    p.status = status
    p.watched_episodes = watched
    p.total_episodes = total
    db.session.commit()
    return p


# 1. A fully watched movie still in the watchlist is excluded.
def test_fully_watched_movie_still_watchedlist_is_excluded(
        app, user, saved_movie):
    """Regression: the old set-difference let this resurface."""
    _watch(user.id, saved_movie, 'movie', 45)
    _log_movie(user.id, saved_movie)
    # End-to-end through get_for_you so the REAL key derivation is exercised.
    _full_profile(user.id)
    out = fy.get_for_you(user.id)
    assert all(c['tmdb_id'] != 4242 for c in out['resurfaced'])
    # And with the key derived the way get_for_you derives it:
    local = fy._load_local_state(user.id)
    assert ('movie', 4242) in local['watched_keys']
    cards = wr.resurface_cards(user.id, watched_keys=local['watched_keys'],
                               today=TODAY)
    assert cards == []


# 2. A fully watched TV show still in the watchlist is excluded.
def test_completed_tv_show_still_watchedlist_is_excluded(app, user):
    show = _tv_show(app, 5001, 'Finished Show')
    _watch(user.id, show, 'tv', 90)
    _progress(user.id, 5001, 'completed', watched=10, total=10)
    cards = wr.resurface_cards(user.id, today=TODAY)
    assert cards == []


def test_completed_show_excluded_even_with_zero_renewal(app, user):
    """Completion beats the staleness rule in both directions."""
    show = _tv_show(app, 5002, 'Just Finished')
    _watch(user.id, show, 'tv', 40)
    _progress(user.id, 5002, 'completed', watched=8, total=8)
    assert wr.resurface_cards(user.id, today=TODAY) == []


# 3. A partially watched TV show remains eligible.
def test_partially_watched_tv_show_stays_eligible(app, user):
    show = _tv_show(app, 5003, 'Half Watched')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5003, 'watching', watched=3, total=10)
    cards = wr.resurface_cards(user.id, today=TODAY)
    assert [c['tmdb_id'] for c in cards] == [5003]


# 4. A TV show with no watched episodes remains eligible.
def test_unwatched_tv_show_stays_eligible(app, user):
    show = _tv_show(app, 5004, 'Not Started')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5004, 'watching', watched=0, total=10)
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [5004]


def test_tv_show_with_no_progress_row_stays_eligible(app, user):
    """No progress record at all ⇒ no completion claim invented."""
    show = _tv_show(app, 5005, 'Never Tracked')
    _watch(user.id, show, 'tv', 60)
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [5005]


def test_plan_to_watch_show_stays_eligible(app, user):
    show = _tv_show(app, 5006, 'Planned')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5006, 'plan_to_watch', watched=0, total=10)
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [5006]


# 5. Missing / ambiguous progress must not cause a false completion claim.
def test_zero_denominator_does_not_imply_completion(app, user):
    """`watched >= total` is 0 >= 0 here — the classic false-completion trap.

    This is exactly why the gate reads the SEALED ``status`` instead of
    recomputing completion locally.
    """
    show = _tv_show(app, 5007, 'No Aired Data')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5007, 'watching', watched=0, total=0)
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [5007]


def test_null_status_does_not_imply_completion(app, user):
    show = _tv_show(app, 5008, 'Odd Status')
    _watch(user.id, show, 'tv', 60)
    db.session.add(TVShowProgress(user_id=user.id, show_id=5008,
                                  status=None, watched_episodes=12,
                                  total_episodes=12))
    db.session.commit()
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [5008]


def test_unknown_status_value_is_not_treated_as_completed(app, user):
    show = _tv_show(app, 5009, 'Weird Status')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5009, 'watched_through', watched=12, total=12)
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [5009]


# 6. not_interested / already_watched remain authoritative.
def test_feedback_still_wins_over_completion_gate_logic(app, user):
    """Feedback excludes regardless of any TV progress state."""
    show = _tv_show(app, 5010, 'Dismissed Show')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5010, 'watching', watched=1, total=10)
    _feedback(user.id, 5010, 'not_interested', media_type='tv')
    assert wr.resurface_cards(user.id, today=TODAY) == []


def test_already_watched_feedback_still_excludes(app, user):
    show = _tv_show(app, 5011, 'Seen Show')
    _watch(user.id, show, 'tv', 60)
    _feedback(user.id, 5011, 'already_watched', media_type='tv')
    assert wr.resurface_cards(user.id, today=TODAY) == []


def test_completed_progress_cannot_re_enable_a_dismissed_title(app, user):
    show = _tv_show(app, 5012, 'Dismissed And Completed')
    _watch(user.id, show, 'tv', 60)
    _progress(user.id, 5012, 'watching', watched=2, total=10)
    _feedback(user.id, 5012, 'not_interested', media_type='tv')
    # Even if the show were later sealed as completed, it stays excluded.
    _progress(user.id, 5012, 'completed', watched=10, total=10)
    assert wr.resurface_cards(user.id, today=TODAY) == []


# 7. Duplicate suppression in the main rail must not block resurfacing.
def test_watchlist_only_exclusion_still_allows_resurfacing(app, user):
    """A title excluded from `items` purely as a duplicate may resurface.

    `exclude_keys` contains watchlist members so the main rail cannot suggest
    a saved title back as new. That suppression must NOT reach this rail.
    """
    show = _tv_show(app, 5013, 'Saved Only')
    _watch(user.id, show, 'tv', 60)
    local = fy._load_local_state(user.id)
    # It IS suppressed from the main rail...
    assert ('tv', 5013) in local['exclude_keys']
    assert ('tv', 5013) in local['watchlisted_keys']
    # ...but that is duplicate suppression, not watched state.
    assert ('tv', 5013) not in local['watched_keys']
    cards = wr.resurface_cards(user.id, watched_keys=local['watched_keys'],
                               today=TODAY)
    assert [c['tmdb_id'] for c in cards] == [5013]


# 8. Watchlist membership must not override real exclusions.
def test_watchlist_membership_does_not_override_watched_state(app, user):
    """Diary covers TV too: a logged show that is still listed stays out."""
    show = _tv_show(app, 5014, 'Watched But Listed')
    _watch(user.id, show, 'tv', 60)
    db.session.add(DiaryEntry(
        user_id=user.id, media_id=show, media_type='tv',
        watched_date=TODAY, rating=4.5))
    db.session.commit()
    local = fy._load_local_state(user.id)
    # Present in every exclusion set — including the real watched set.
    assert ('tv', 5014) in local['exclude_keys']
    assert ('tv', 5014) in local['watchlisted_keys']
    assert ('tv', 5014) in local['watched_keys']
    assert wr.resurface_cards(user.id, watched_keys=local['watched_keys'],
                              today=TODAY) == []


def test_watched_keys_is_not_derived_from_exclude_minus_watchlist(app, user):
    """Pins the corrected derivation itself, independent of any resurfacING."""
    show = _tv_show(app, 5015, 'Both Watched And Listed')
    _watch(user.id, show, 'tv', 60)
    db.session.add(DiaryEntry(
        user_id=user.id, media_id=show, media_type='tv',
        watched_date=TODAY, rating=3.5))
    db.session.commit()
    local = fy._load_local_state(user.id)
    key = ('tv', 5015)
    assert key in local['watched_keys']
    # The old derivation would have dropped it.
    assert key not in (local['exclude_keys'] - local['watchlisted_keys'])
    # And watchlist-only members must NOT leak into watched_keys.
    other = _tv_show(app, 5016, 'Saved Only 2')
    _watch(user.id, other, 'tv', 60)
    local2 = fy._load_local_state(user.id)
    assert ('tv', 5016) not in local2['watched_keys']


# 9. Query bounds still hold; no N+1.
def test_query_count_does_not_grow_with_tv_candidates(app, user):
    """TV completion lookup is ONE batched read, not one per show."""
    for i in range(8):
        show_id = 6100 + i
        item_id = _tv_show(app, show_id, f'Show {i}')
        _watch(user.id, item_id, 'tv', 40)
        _progress(user.id, show_id, 'completed' if i % 2 else 'watching',
                  watched=10, total=10)

    with statements() as rec:
        cards = wr.resurface_cards(user.id, today=TODAY)
    # Odd indices were sealed 'completed' and dropped; even ones survived.
    assert {c['tmdb_id'] for c in cards} == {6100, 6102, 6104, 6106}
    assert len(_phase_counts(rec)) == len(_phase_counts(rec))
    assert _phase_counts(rec)['tv-completion'] == 1, (
        f"one tv-completion statement expected, got {rec.captured}")


def test_completed_show_query_is_skipped_for_movies_only(app, user, saved_movie):
    """A movie-only resurfacING costs no TV-progress query."""
    _watch(user.id, saved_movie, 'movie', 45)
    seen = []

    original = wr._completed_shows

    def spy(*a, **k):
        seen.append(a)
        return original(*a, **k)

    wr._completed_shows = spy
    try:
        wr.resurface_cards(user.id, today=TODAY)
    finally:
        wr._completed_shows = original
    assert seen == []


def test_completed_show_is_scoped_to_the_owner(app, user):
    """Another user's completion must not suppress my resurfacING."""
    from models.user import User as _U
    other = _U(username='other-tv', email='other-tv@resurface.test',
               email_verified=True)
    other.set_password('TestPass1')
    db.session.add(other)
    db.session.commit()
    show = _tv_show(app, 6200, 'Shared Show')
    _watch(user.id, show, 'tv', 60)
    _progress(other.id, 6200, 'completed', watched=9, total=9)
    assert [c['tmdb_id']
            for c in wr.resurface_cards(user.id, today=TODAY)] == [6200]


# 4b. Threshold boundary is pinned exactly (STALE_AFTER_DAYS = 21 heuristic).
def test_threshold_boundary_is_exactly_inclusive(app, user):
    for tmdb, days in ((7100, wr.STALE_AFTER_DAYS - 1), (7101, wr.STALE_AFTER_DAYS)):
        item = MediaItem(tmdb_id=tmdb, media_type='movie',
                         title=f'B {tmdb}', poster_path='/p.jpg')
        db.session.add(item)
        db.session.commit()
        _watch(user.id, item.id, 'movie', days)
    surfaced = {c['tmdb_id'] for c in wr.resurface_cards(user.id, today=TODAY)}
    assert surfaced == {7101}, "exactly STALE_AFTER_DAYS is stale, one less is not"


def test_stale_after_days_is_a_documented_tunable_heuristic():
    """Guard against the 21 being mistaken for a validated finding."""
    assert wr.STALE_AFTER_DAYS == 21
    src = pathlib.Path(wr.__file__).read_text()
    assert 'PRODUCT HEURISTIC' in src
    assert 'NOT EVIDENCE-BACKED' in src


def test_excluded_titles_do_not_consume_slots(app, user):
    """The rail must still fill up to RESURFACE_MAX after filtering.

    Regression: exclusion used to run after the limit trim, so a dismissed or
    completed title burned one of the six slots and the rail rendered short
    while further eligible titles sat further down the sorted list.
    """
    total = wr.RESURFACE_MAX + 4
    for i in range(total):
        tmdb = 8200 + i
        item = MediaItem(tmdb_id=tmdb, media_type='movie',
                         title=f'Fill {i}', poster_path='/p.jpg')
        db.session.add(item)
        db.session.commit()
        _watch(user.id, item.id, 'movie', 40)
    # Dismiss the top-ranked two (lowest tmdb_id sorts first).
    _feedback(user.id, 8200, 'not_interested')
    _feedback(user.id, 8201, 'already_watched')

    cards = wr.resurface_cards(user.id, today=TODAY)
    ids = {c['tmdb_id'] for c in cards}
    assert len(cards) == wr.RESURFACE_MAX, "rail rendered short"
    assert 8200 not in ids and 8201 not in ids
    # The freed slots went to the next eligible titles.
    assert {8202, 8203, 8204, 8205}.issubset(ids)

# ══════════════════════════════════════════════════════════════════════════
# Executed-SQL instrumentation
#
# Uses the repository's established pattern (same before_cursor_execute +
# normalised-SQL capture as tests/test_tv_canonical_invariants.py and the other
# budget tests) so these numbers are STATEMENTS EXECUTED BY SQLALCHEMY — not
# titles processed, not API calls, not Python iterations.
#
# Statements are additionally bucketed by phase, so a future N+1 inside one
# specific phase is caught rather than hidden inside an aggregate total.
# ══════════════════════════════════════════════════════════════════════════


class statements:
    """Record every SQL statement executed inside the block."""

    def __enter__(self):
        self.captured = []
        self._fn = (lambda conn, cursor, statement, *a, **k:
                    self.captured.append(" ".join(statement.split())))
        event.listen(db.engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *exc):
        event.remove(db.engine, "before_cursor_execute", self._fn)
        return False

    def selects(self):
        return [s for s in self.captured
                if s.lstrip().upper().startswith("SELECT")]


def _phase(sql):
    """Bucket a statement by the phase that issues it."""
    low = sql.lower()
    if "user_watchlist" in low:
        return "watchlist-selection"
    if "media_item" in low:
        return "media-metadata"
    if "recommendation_feedback" in low:
        return "feedback-filter"
    if "tv_show_progress" in low:
        return "tv-completion"
    if "movie_release_date" in low or "upcoming_episode" in low:
        return "release-context"
    if "diary_entry" in low or "user_viewed" in low or "review" in low:
        return "watched-state"
    if "taste_profile" in low:
        return "profile"
    return "other"


def _phase_counts(rec):
    out = {}
    for sql in rec.captured:
        key = _phase(sql)
        out[key] = out.get(key, 0) + 1
    return out


def _bulk_seed(user_id, n_movies, n_tv, tmdb_base,
               movie_priority='medium', tv_priority='medium'):
    """Seed n_movies + n_tv stale watchlist titles with few round-trips."""
    # Defensive: a previously failed statement leaves the session unusable.
    db.session.rollback()
    # Anchored to TODAY, not the wall clock: resurface_cards is called with
    # today=TODAY below, so a utcnow-based date_added would land in the future
    # and every title would read as 0 days stale.
    now = datetime(TODAY.year, TODAY.month, TODAY.day) - timedelta(days=40)
    media_rows, prog_rows = [], []
    for i in range(n_movies):
        tmdb = tmdb_base + i
        media_rows.append({"tmdb_id": tmdb, "media_type": "movie",
                           "title": f"M{tmdb}", "poster_path": "/p.jpg"})
    for i in range(n_tv):
        tmdb = tmdb_base + 100000 + i
        media_rows.append({"tmdb_id": tmdb, "media_type": "tv",
                           "title": f"T{tmdb}", "poster_path": "/p.jpg"})
        prog_rows.append({"user_id": user_id, "show_id": tmdb,
                          "status": "watching", "watched_episodes": 1,
                          "total_episodes": 10})
    db.session.execute(MediaItem.__table__.insert(), media_rows)
    db.session.commit()
    ids = {(r.media_type, r.tmdb_id): r.id for r in
           MediaItem.query.filter(
               MediaItem.tmdb_id.in_([m["tmdb_id"] for m in media_rows]))
           .with_entities(MediaItem.id, MediaItem.media_type,
                          MediaItem.tmdb_id).all()}
    db.session.execute(user_watchlist.insert(), [
        {"user_id": user_id, "media_id": ids[(m["media_type"], m["tmdb_id"])],
         "media_type": m["media_type"], "date_added": now,
         "priority": (movie_priority if m["media_type"] == "movie"
                      else tv_priority)} for m in media_rows])
    if prog_rows:
        db.session.execute(TVShowProgress.__table__.insert(), prog_rows)
    db.session.commit()


def test_statement_count_is_flat_across_candidate_counts(app, user):
    """1, 20 and the maximum supported scan must issue the SAME statements.

    The real assertion is EQUALITY across the three sizes, not an upper bound:
    an upper bound can be satisfied by a slow N+1 that has not yet crossed it.
    """
    counts = {}
    for label, base, n_movies, n_tv in (
            ("1", 50000, 1, 0), ("20", 51000, 20, 0),
            ("cap", 52000, 0, wr.WATCHLIST_SCAN_CAP)):
        other = _make_user(f'resize_q{label}')
        _bulk_seed(other.id, n_movies, n_tv, base)
        with statements() as rec:
            wr.resurface_cards(other.id, today=TODAY)
        counts[label] = len(rec.captured)
    assert counts['1'] == counts['20'], counts
    # The cap batch contains only TV, so it additionally pays the single
    # tv-completion statement — exactly one, never one per show.
    assert counts['cap'] == counts['1'] + 1, counts


def test_each_phase_issues_at_most_one_statement(app, user):
    """Guards against an N+1 hiding inside a single phase.

    TV rows are seeded at 'high' priority so they win the six-card trim.
    Without that the movies (lower tmdb_id) fill every slot, show_ids comes
    out empty, and the TV release-context phase is never actually executed —
    which would make this assertion vacuous.
    """
    _bulk_seed(user.id, 30, 30, 60000,
               movie_priority='low', tv_priority='high')
    with statements() as rec:
        cards = wr.resurface_cards(user.id, today=TODAY)
    assert len(cards) == wr.RESURFACE_MAX
    assert {c['media_type'] for c in cards} == {'tv'}, (
        "expected the six returned cards to be TV so every phase is exercised")
    phases = _phase_counts(rec)
    for phase in ('watchlist-selection', 'feedback-filter', 'tv-completion',
                  'release-context'):
        assert phases.get(phase, 0) <= 1, (
            f"{phase} issued {phases.get(phase)} statements for 60 titles: "
            f"{rec.captured}")
    assert sum(phases.values()) <= 6, phases


def test_movie_only_batch_issues_no_tv_completion_statement(
        app, user, saved_movie):
    """Documented intent: the TV completion read is skipped for movie-only."""
    _bulk_seed(user.id, 40, 0, 70000)
    _watch(user.id, saved_movie, 'movie', 45)
    with statements() as rec:
        wr.resurface_cards(user.id, today=TODAY)
    assert 'tv-completion' not in _phase_counts(rec), rec.captured


def test_watchlist_read_respects_the_scan_cap(app, user):
    """The single watchlist SELECT must never read past the cap."""
    _bulk_seed(user.id, wr.WATCHLIST_SCAN_CAP + 50, 0, 80000)
    with statements() as rec:
        wr.resurface_cards(user.id, today=TODAY)
    watchlist_sql = [s for s in rec.captured
                     if 'user_watchlist' in s.lower()]
    assert len(watchlist_sql) == 1, watchlist_sql
    assert 'LIMIT' in watchlist_sql[0].upper(), (
        "watchlist selection is not capped: " + watchlist_sql[0])


def test_full_for_you_path_statement_count_is_flat(app, user, monkeypatch):
    """The complete caller path stays flat as the watchlist grows."""
    _full_profile(user.id)
    monkeypatch.setattr(fy, '_generate_candidates', lambda *a, **k: [])
    counts = {}
    for label, base, n_movies, n_tv in (("1", 90000, 1, 0),
                                        ("20", 91000, 20, 0)):
        other = _make_user(f'resize_full{label}')
        _bulk_seed(other.id, n_movies, n_tv, base)
        _full_profile(other.id)
        fy._cache.clear()
        with statements() as rec:
            fy.get_for_you(other.id)
        counts[label] = len(rec.captured)
    assert counts['1'] == counts['20'], counts


# ══════════════════════════════════════════════════════════════════════════
# Cold / hedged path — the audit blocker
#
# The cold/hedged branch of get_for_you() returns BEFORE _load_local_state()
# runs, so it has no watched-state set to hand the resurfacING engine. It
# substituted an empty set, which meant a watchlisted movie carrying a
# canonical (unrated) DiaryEntry resurfaced as something the user still needed
# to watch. These tests drive the REAL get_for_you() branch — no mocking of
# _resurface(), and the watched set is never passed in by hand.
# ══════════════════════════════════════════════════════════════════════════

def _cold_user(username):
    """A user with no usable TasteProfile — takes the cold/hedged branch."""
    u = _make_user(username)
    return u


def test_cold_start_excludes_watched_movie_still_on_watchlist(
        app, user, saved_movie):
    """END-TO-END regression for the audit blocker.

    Reproduces the exact failure: the movie is on the watchlist AND has a
    canonical DiaryEntry (so it is watched), but the entry is unrated so it
    contributes no taste signal and the user is legitimately cold-start.
    """
    cold = _cold_user('coldwatched')
    _watch(cold.id, saved_movie, 'movie', 45)
    # Unrated → counted as watched, contributes nothing to taste signals.
    db.session.add(DiaryEntry(
        user_id=cold.id, media_id=saved_movie, media_type='movie',
        watched_date=TODAY, rating=None))
    db.session.commit()

    out = fy.get_for_you(cold.id)

    # Really on the cold/hedged branch (otherwise this proves nothing).
    assert out['mode'] in ('cold', 'hedged'), out['mode']
    assert out['personalized'] is False
    assert out['items'] == []

    tmdb_ids = [c['tmdb_id'] for c in out['resurfaced']]
    assert 4242 not in tmdb_ids, (
        f"watched-but-listed movie resurfaced for a cold user: {tmdb_ids}")

    # The watched key really is derivable — the fix must supply it.
    assert ('movie', 4242) in fy._load_watched_keys(cold.id)


def test_cold_start_still_resurfaces_an_unwatched_title(app, user, saved_movie):
    """The counterpart: cold users must NOT lose the feature entirely."""
    cold = _cold_user('coldclean')
    _watch(cold.id, saved_movie, 'movie', 45)
    out = fy.get_for_you(cold.id)
    assert out['personalized'] is False
    assert [c['tmdb_id'] for c in out['resurfaced']] == [4242]


def test_cold_start_excludes_completed_show_and_dismissed_title(app, user):
    """TV completion + feedback stay authoritative on the cold path."""
    cold = _cold_user('coldtv')
    done = _tv_show(app, 5300, 'Completed Cold Show')
    _watch(cold.id, done, 'tv', 60)
    _progress(cold.id, 5300, 'completed', watched=9, total=9)

    part = _tv_show(app, 5301, 'Partial Cold Show')
    _watch(cold.id, part, 'tv', 60)
    _progress(cold.id, 5301, 'watching', watched=2, total=10)

    dismissed = MediaItem.query.filter_by(tmdb_id=5302).first()
    if dismissed is None:
        dismissed = MediaItem(tmdb_id=5302, media_type='movie',
                              title='Dismissed Cold', poster_path='/p.jpg')
        db.session.add(dismissed)
        db.session.commit()
    _watch(cold.id, dismissed.id, 'movie', 60)
    _feedback(cold.id, 5302, 'not_interested')

    out = fy.get_for_you(cold.id)
    ids = {c['tmdb_id'] for c in out['resurfaced']}
    assert 5300 not in ids, "completed show resurfaced on the cold path"
    assert 5302 not in ids, "dismissed title resurfaced on the cold path"
    assert 5301 in ids, "partially watched show must remain eligible"


def test_load_watched_keys_agrees_between_cold_and_full_paths(app, user,
                                                              saved_movie):
    """Both paths must derive watched keys identically, by construction."""
    _watch(user.id, saved_movie, 'movie', 45)
    _log_movie(user.id, saved_movie)
    show = _tv_show(app, 5400, 'Shared State Show')
    _watch(user.id, show, 'tv', 45)
    db.session.add(DiaryEntry(
        user_id=user.id, media_id=show, media_type='tv',
        watched_date=TODAY, rating=None))
    db.session.commit()

    # Cold path helper (no profile loaded).
    cold_keys = fy._load_watched_keys(user.id)
    # Full path, via the engine's own local state.
    full_keys = fy._load_local_state(user.id)['watched_keys']
    assert cold_keys == full_keys, (cold_keys, full_keys)
    assert ('movie', 4242) in cold_keys
    assert ('tv', 5400) in cold_keys


def test_load_watched_keys_is_user_scoped(app, user, saved_movie):
    """Another user's watched state must never enter my watched keys."""
    _watch(user.id, saved_movie, 'movie', 45)
    _log_movie(user.id, saved_movie)
    mine = fy._load_watched_keys(user.id)
    other = _make_user('wkeys_other')
    assert fy._load_watched_keys(other.id) == set()
    assert mine == {('movie', 4242)}


def test_load_watched_keys_empty_user_is_empty(app, user):
    assert fy._load_watched_keys(user.id) == set()


def test_cold_start_empty_watchlist_is_empty(app, user):
    """Empty watchlist → empty resurfacing, section removed by the JS."""
    cold = _cold_user('coldempty')
    out = fy.get_for_you(cold.id)
    assert out['resurfaced'] == []
    assert out['items'] == []
    assert out['personalized'] is False
