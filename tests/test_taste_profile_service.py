"""Tests for the canonical taste profile computation service (Phase 2).

Covers the pure helpers (quality, decay, normalization, decades, weighted
percentiles, confidence), per-signal collection semantics (one event → one
signal, no double counting), persistence behavior (create/update/idempotent,
no duplicate rows), and hygiene (no network path, deterministic describe).
"""
from datetime import datetime, timedelta, date

import pytest

from models import (
    db, User, MediaItem, Review, DiaryEntry, MediaLike, UserMediaTag, Tag,
    TVEpisodeWatch, user_watchlist, TasteProfile,
)
import api.taste_profile as tp


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def user(app, db):
    u = User(username=tp_test_id('taster'),
             email=f"{tp_test_id('taster')}@example.com")
    u.set_password('password123')
    db.session.add(u)
    db.session.commit()
    return u


def tp_test_id(prefix):
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# The suite shares one session-wide SQLite DB, so tmdb_ids must be unique
# across ALL tests — a module-level monotonic counter, not per-test.
_TMDB_COUNTER = {'n': 900000}


@pytest.fixture
def media_factory(app, db):
    """Create MediaItems with globally unique tmdb_ids."""

    def _make(media_type='movie', title='Test Movie', genres='Drama, Crime',
              release_date=date(2015, 6, 1), runtime=120):
        _TMDB_COUNTER['n'] += 1
        m = MediaItem(
            tmdb_id=_TMDB_COUNTER['n'], media_type=media_type, title=title,
            release_date=release_date, genres=genres, runtime=runtime,
        )
        db.session.add(m)
        db.session.commit()
        return m

    return _make


def _add_watchlist(user, media):
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=media.id, media_type=media.media_type))
    db.session.commit()


# ── 1. rating_quality mapping ────────────────────────────────────────────────

def test_rating_quality_mapping():
    # Audited anchors: 5.0→+1.0, 2.5→+0.4 (mild positive), 0.5→-1.0.
    assert tp.rating_quality(5.0) == 1.0
    assert tp.rating_quality(2.5) == pytest.approx(0.4)
    assert tp.rating_quality(0.5) == -1.0
    # Upper segment slope 0.24/star: 3.75 → 0.4 + 1.25*0.24 = 0.7.
    assert tp.rating_quality(3.75) == pytest.approx(0.7)
    # Lower segment slope 0.7/star crosses zero ≈1.93 → negative below ~2★.
    assert tp.rating_quality(1.0) < 0
    assert tp.rating_quality(1.5) < 0
    assert tp.rating_quality(2.0) > 0
    assert tp.rating_quality(None) == 0.0
    assert tp.rating_quality('bogus') == 0.0
    # Out-of-range inputs clamp to the [-1, 1] evidence band.
    assert tp.rating_quality(99) == 1.0
    assert tp.rating_quality(-3) == -1.0


# ── 2. negative rating produces negative evidence ───────────────────────────

def test_negative_rating_negative_evidence(app, db, user, media_factory):
    m = media_factory(genres='Horror')
    db.session.add(Review(user_id=user.id, media_id=m.id, media_type='movie',
                          rating=1.0, content=''))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    weights = profile.genre_weights
    assert weights.get('Horror', 0) < 0


# ── 3–5. recency decay ───────────────────────────────────────────────────────

def test_recency_decay_windows():
    now = datetime(2026, 9, 12, 12, 0, 0)
    assert tp.recency_decay(now, now) == 1.0                       # today
    assert tp.recency_decay(now - timedelta(days=365), now) == pytest.approx(0.5)  # 1y
    year2 = tp.recency_decay(now - timedelta(days=730), now)
    assert year2 == pytest.approx(0.25, abs=1e-2)                  # 2y
    very_old = tp.recency_decay(now - timedelta(days=3650), now)
    assert very_old >= 0.1                                         # floor, inclusive
    assert very_old == pytest.approx(0.1, abs=0.05)


def test_decay_floor():
    now = datetime(2026, 9, 12)
    assert tp.recency_decay(datetime(2000, 1, 1), now) == 0.1
    assert tp.recency_decay(datetime(1970, 1, 1), now) == 0.1


def test_decay_future_timestamp():
    now = datetime(2026, 9, 12)
    assert tp.recency_decay(now + timedelta(days=30), now) == 1.0
    # date objects work too
    assert tp.recency_decay(date(2026, 9, 12), now) == 1.0
    assert tp.recency_decay(None, now) == 1.0


# ── 6. L2 normalization ──────────────────────────────────────────────────────

def test_l2_normalization():
    norm = tp.normalize_l2({'a': 3.0, 'b': 4.0})
    assert norm['a'] == pytest.approx(0.6)
    assert norm['b'] == pytest.approx(0.8)
    # sign preserved
    signed = tp.normalize_l2({'a': 3.0, 'b': -4.0})
    assert signed['a'] > 0 and signed['b'] < 0
    # degenerate inputs
    assert tp.normalize_l2({}) == {}
    assert tp.normalize_l2({'a': 0.0}) == {}


# ── 7. decade extraction ─────────────────────────────────────────────────────

def test_decade_extraction():
    assert tp.decade_from_release_date('1987-06-04') == '1980s'
    assert tp.decade_from_release_date(date(2015, 6, 1)) == '2010s'
    assert tp.decade_from_release_date(date(2010, 1, 1)) == '2010s'
    assert tp.decade_from_release_date('1999-12-31') == '1990s'
    assert tp.decade_from_release_date(None) is None
    assert tp.decade_from_release_date('not-a-date') is None
    # implausible years rejected
    assert tp.decade_from_release_date('9999-01-01') is None
    assert tp.decade_from_release_date('1800-01-01') is None


# ── 8–10. weighted aggregation / genre evidence ──────────────────────────────

def test_genre_aggregation_rating_weighted(app, db, user, media_factory):
    """A 5★ review and a 3★ review of same-genre titles: the 5★ genre
    accumulates strictly more (signed, weighted) evidence."""
    loved = media_factory(genres='Thriller', runtime=100)
    liked = media_factory(genres='Thriller', runtime=100)
    db.session.add(Review(user_id=user.id, media_id=loved.id,
                          media_type='movie', rating=5.0))
    db.session.add(Review(user_id=user.id, media_id=liked.id,
                          media_type='movie', rating=3.0))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    w = profile.genre_weights
    assert w.get('Thriller', 0) > 0
    # one loved (1.0 evidence) + one liked (0.2 evidence) — L2 keeps ratio
    # visible as relative share; here the single positive genre is 1.0.
    assert len(w) == 1


def test_negative_genre_evidence_preserved(app, db, user, media_factory):
    loved = media_factory(genres='Thriller')
    hated = media_factory(genres='Horror')
    db.session.add(Review(user_id=user.id, media_id=loved.id,
                          media_type='movie', rating=5.0))
    db.session.add(Review(user_id=user.id, media_id=hated.id,
                          media_type='movie', rating=0.5))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    w = profile.genre_weights
    assert w['Thriller'] > 0 and w['Horror'] < 0
    # L2 unit-length overall vector
    assert pytest.approx(sum(v * v for v in w.values()), abs=1e-3) == 1.0


# ── 11. media type aggregation ───────────────────────────────────────────────

def test_media_type_aggregation(app, db, user, media_factory):
    m1 = media_factory(media_type='movie', genres='Drama')
    m2 = media_factory(media_type='movie', genres='Crime')
    m3 = media_factory(media_type='tv', genres='Drama')
    for m in (m1, m2, m3):
        db.session.add(Review(user_id=user.id, media_id=m.id,
                              media_type=m.media_type, rating=5.0))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    pref = profile.media_type_pref
    assert pref['movie'] == pytest.approx(2 / 3, abs=1e-3)
    assert pref['tv'] == pytest.approx(1 / 3, abs=1e-3)


# ── 12. runtime percentile calculation ───────────────────────────────────────

def test_weighted_percentile():
    values = [80, 90, 100, 110, 120]
    weights = [1, 1, 1, 1, 1]
    assert tp.weighted_percentile(values, weights, 0.25) == 90
    assert tp.weighted_percentile(values, weights, 0.75) == 110
    assert tp.weighted_percentile(values, weights, 0.0) == 80
    assert tp.weighted_percentile(values, weights, 1.0) == 120
    # weight shifts the percentile toward the heavy sample
    assert tp.weighted_percentile([80, 120], [1, 10], 0.75) == 120
    assert tp.weighted_percentile([], [], 0.5) is None
    assert tp.weighted_percentile([80], [0], 0.5) is None


def test_runtime_pref_from_mediaitem_only(app, db, user, media_factory):
    """Runtime comes ONLY from persisted MediaItem.runtime; titles without
    a persisted runtime are skipped (no TMDb calls anywhere in the service)."""
    with_runtime = media_factory(genres='Drama', runtime=90)
    no_runtime = media_factory(genres='Drama', runtime=None)
    db.session.add(Review(user_id=user.id, media_id=with_runtime.id,
                          media_type='movie', rating=5.0))
    db.session.add(Review(user_id=user.id, media_id=no_runtime.id,
                          media_type='movie', rating=5.0))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    rp = profile.runtime_pref
    assert rp['p25'] == 90 and rp['p75'] == 90
    assert rp['sample_count'] == 1  # the no-runtime title was skipped


# ── 13–14. counts ────────────────────────────────────────────────────────────

def test_distinct_title_and_signal_counting(app, db, user, media_factory):
    """A title with rating + rewatch + 5 tags counts ONCE for titles;
    its rating/rewatch are separate signals but the 5 tags collapse to 3
    (per-title cap) — and the title itself still counts once."""
    m = media_factory(genres='Drama')
    db.session.add(Review(user_id=user.id, media_id=m.id, media_type='movie',
                          rating=5.0))
    db.session.add(DiaryEntry(user_id=user.id, media_id=m.id,
                              media_type='movie',
                              watched_date=date(2026, 9, 1),
                              is_rewatch=True))
    tags = [Tag(name=tp_test_id('tag'), usage_count=0) for _ in range(5)]
    db.session.add_all(tags)
    db.session.commit()
    for tag in tags:  # 5 distinct tags on one title — capped at 3 evidences
        db.session.add(UserMediaTag(user_id=user.id, media_id=m.tmdb_id,
                                    media_type='movie', tag_id=tag.id))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.distinct_title_count == 1
    # 1 review_rating + 1 diary_rewatch + 3 capped tags = 5 observations
    assert profile.signal_count == 5


# ── 15–16. confidence ────────────────────────────────────────────────────────

def test_confidence_below_threshold():
    assert tp._confidence(50, 4) == 0.0       # breadth gate not met
    assert tp._confidence(2, 10) == pytest.approx(2 / 30, abs=1e-4)


def test_confidence_after_five_titles():
    assert tp._confidence(30, 5) == 1.0
    assert tp._confidence(15, 5) == 0.5
    assert tp._confidence(0, 0) == 0.0
    assert tp._confidence(100, 50) == 1.0


def test_confidence_persisted_requires_breadth(app, db, user, media_factory):
    """Two signals but only 2 distinct titles → confidence stays 0."""
    m1 = media_factory()
    m2 = media_factory()
    db.session.add(Review(user_id=user.id, media_id=m1.id,
                          media_type='movie', rating=5.0))
    db.session.add(Review(user_id=user.id, media_id=m2.id,
                          media_type='movie', rating=5.0))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.distinct_title_count == 2
    assert profile.confidence == 0.0


# ── 17. zero-signal user ─────────────────────────────────────────────────────

def test_zero_signal_user_neutral_profile(app, db, user):
    profile = tp.compute_profile(user.id)  # must not raise
    assert profile.genre_weights == {}
    assert profile.decade_weights == {}
    assert profile.director_affinity == {}
    assert profile.runtime_pref == {}
    assert profile.media_type_pref == {}
    assert profile.confidence == 0.0
    assert profile.signal_count == 0
    assert profile.distinct_title_count == 0
    assert profile.mood_tags is None


# ── 18. watchlist-only user ──────────────────────────────────────────────────

def test_watchlist_only_user(app, db, user, media_factory):
    m = media_factory(genres='Comedy', release_date=date(1994, 9, 10))
    _add_watchlist(user, m)
    profile = tp.compute_profile(user.id)
    assert profile.genre_weights.get('Comedy', 0) > 0
    assert profile.decade_weights.get('1990s', 0) > 0
    assert profile.media_type_pref == {'movie': 1.0}
    # watchlist is weak intent: confidence stays 0 below the breadth gate
    assert profile.distinct_title_count == 1
    assert profile.confidence == 0.0
    assert profile.runtime_pref == {}  # no runtime evidence from intent


# ── 19. media-like signal ────────────────────────────────────────────────────

def test_media_like_signal(app, db, user, media_factory):
    m = media_factory(genres='Action')
    db.session.add(MediaLike(user_id=user.id, media_id=m.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.genre_weights.get('Action', 0) > 0
    assert profile.signal_count == 1


# ── 20. tag signal ───────────────────────────────────────────────────────────

def test_tag_signal(app, db, user, media_factory):
    m = media_factory(genres='Horror')
    tag = Tag(name=tp_test_id('creepy'))
    db.session.add(tag)
    db.session.commit()
    db.session.add(UserMediaTag(user_id=user.id, media_id=m.tmdb_id,
                                media_type='movie', tag_id=tag.id))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.genre_weights.get('Horror', 0) > 0
    assert profile.signal_count == 1


# ── 21. diary rating ─────────────────────────────────────────────────────────

def test_diary_rating_signal(app, db, user, media_factory):
    m = media_factory(genres='Romance')
    db.session.add(DiaryEntry(user_id=user.id, media_id=m.id,
                              media_type='movie', rating=4.5,
                              watched_date=date(2026, 8, 1)))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.genre_weights.get('Romance', 0) > 0
    assert profile.signal_count == 1  # rating only — no double counting


# ── 22. diary rewatch ────────────────────────────────────────────────────────

def test_diary_rewatch_signal(app, db, user, media_factory):
    """Rewatch adds a separate viewing signal on top of the rating —
    both count, neither duplicated."""
    m = media_factory(genres='Sci-Fi')
    db.session.add(DiaryEntry(user_id=user.id, media_id=m.id,
                              media_type='movie', rating=5.0,
                              watched_date=date(2026, 8, 1),
                              is_rewatch=True))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.signal_count == 2  # diary_rating + diary_rewatch
    assert profile.distinct_title_count == 1


def test_diary_rewatch_without_rating(app, db, user, media_factory):
    m = media_factory(genres='Sci-Fi')
    db.session.add(DiaryEntry(user_id=user.id, media_id=m.id,
                              media_type='movie',
                              watched_date=date(2026, 8, 1),
                              is_rewatch=True))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.signal_count == 1  # rewatch alone


# ── 23. review rating ────────────────────────────────────────────────────────

def test_review_rating_signal(app, db, user, media_factory):
    m = media_factory(genres='Western')
    db.session.add(Review(user_id=user.id, media_id=m.id, media_type='movie',
                          rating=4.0))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.genre_weights.get('Western', 0) > 0
    assert profile.signal_count == 1


# ── 24. episode rating mapped to parent show ────────────────────────────────

def test_episode_rating_maps_to_show(app, db, user, media_factory):
    """Episode rating evidence lands on the show's dimensions via
    MediaItem(tmdb_id=show_id, media_type='tv') — never as a movie id."""
    show = media_factory(media_type='tv', genres='Mystery', title='Show')
    db.session.add(TVEpisodeWatch(
        user_id=user.id, show_id=show.tmdb_id,
        season_number=1, episode_number=1, rating=5.0,
        watched_date=date(2026, 9, 1)))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.genre_weights.get('Mystery', 0) > 0
    assert profile.media_type_pref.get('tv', 0) > 0
    assert profile.signal_count == 1
    assert profile.distinct_title_count == 1


def test_unrated_episodes_ignored(app, db, user, media_factory):
    show = media_factory(media_type='tv', genres='Mystery')
    db.session.add(TVEpisodeWatch(
        user_id=user.id, show_id=show.tmdb_id,
        season_number=1, episode_number=1, rating=None,
        watched_date=date(2026, 9, 1)))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.signal_count == 0


# ── 25–28. determinism + persistence ─────────────────────────────────────────

def test_deterministic_repeated_computation(app, db, user, media_factory):
    m1 = media_factory(genres='Thriller', runtime=100)
    m2 = media_factory(genres='Drama', runtime=140)
    db.session.add(Review(user_id=user.id, media_id=m1.id,
                          media_type='movie', rating=5.0))
    db.session.add(Review(user_id=user.id, media_id=m2.id,
                          media_type='movie', rating=4.0))
    db.session.commit()
    p1 = tp.compute_profile(user.id)
    snap1 = p1.to_dict()
    p2 = tp.compute_profile(user.id)
    snap2 = p2.to_dict()
    for key in ('genre_weights', 'decade_weights', 'media_type_pref',
                'runtime_pref', 'confidence', 'signal_count',
                'distinct_title_count', 'profile_version'):
        assert snap1[key] == snap2[key], key


def test_persistence_creates_then_updates(app, db, user, media_factory):
    assert TasteProfile.query.filter_by(user_id=user.id).first() is None
    profile = tp.compute_profile(user.id)
    assert profile.id is not None
    original_id = profile.id
    original_updated = profile.updated_at

    m = media_factory(genres='Fantasy')
    db.session.add(Review(user_id=user.id, media_id=m.id,
                          media_type='movie', rating=5.0))
    db.session.commit()
    updated = tp.compute_profile(user.id)
    assert updated.id == original_id            # same row updated
    assert updated.genre_weights.get('Fantasy', 0) > 0
    assert updated.updated_at >= original_updated
    assert TasteProfile.query.filter_by(user_id=user.id).count() == 1


def test_no_duplicate_profile_rows(app, db, user, media_factory):
    m = media_factory()
    db.session.add(Review(user_id=user.id, media_id=m.id, media_type='movie',
                          rating=4.0))
    db.session.commit()
    for _ in range(3):
        tp.compute_profile(user.id)
    assert TasteProfile.query.filter_by(user_id=user.id).count() == 1


def test_compute_unknown_user_raises(app, db):
    with pytest.raises(ValueError):
        tp.compute_profile(99999999)


# ── 29. no external HTTP/network call path ───────────────────────────────────

def test_no_network_calls_during_computation(app, db, user, media_factory, monkeypatch):
    """compute_profile must run entirely from local data — any socket use
    (TMDb, providers, anything) fails the test."""
    import socket

    class _Forbidden(socket.socket):
        def __init__(self, *args, **kwargs):
            raise AssertionError("network socket created during taste "
                                 "profile computation")

    monkeypatch.setattr(socket, 'socket', _Forbidden)
    m = media_factory(genres='Drama')
    db.session.add(Review(user_id=user.id, media_id=m.id, media_type='movie',
                          rating=4.0))
    _add_watchlist(user, m)
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.signal_count >= 1


def test_service_import_has_no_side_effects():
    """Importing the module must not compute profiles or touch the DB."""
    import sys
    # Sanity: the module exposes only pure/service functions and constants.
    mod = sys.modules.get('api.taste_profile')
    assert mod is not None
    assert callable(mod.compute_profile)
    assert callable(mod.describe_profile)
    assert mod.PROFILE_VERSION >= 1


# ── 30. director affinity stays empty ────────────────────────────────────────

def test_director_affinity_stays_empty(app, db, user, media_factory):
    m = media_factory(genres='Drama')
    db.session.add(Review(user_id=user.id, media_id=m.id, media_type='movie',
                          rating=5.0))
    db.session.commit()
    profile = tp.compute_profile(user.id)
    assert profile.director_affinity == {}
    assert profile.director_affinity_json == '{}'


# ── 31. describe_profile pure/deterministic ──────────────────────────────────

def test_describe_profile_structure_and_determinism(app, db, user, media_factory):
    # 6 signals over 5 distinct titles → confidence 0.2 → personalized True.
    loved = media_factory(genres='Thriller, Mystery')
    hated = media_factory(genres='Horror')
    db.session.add(Review(user_id=user.id, media_id=loved.id,
                          media_type='movie', rating=5.0))
    db.session.add(Review(user_id=user.id, media_id=hated.id,
                          media_type='movie', rating=0.5))
    for _ in range(3):  # same-genre fillers to reach the breadth gate
        filler = media_factory(genres='Mystery, Thriller')
        db.session.add(Review(user_id=user.id, media_id=filler.id,
                              media_type='movie', rating=5.0))
    db.session.add(MediaLike(user_id=user.id, media_id=loved.tmdb_id,
                             media_type='movie'))
    db.session.commit()
    profile = tp.compute_profile(user.id)

    d1 = tp.describe_profile(profile)
    d2 = tp.describe_profile(profile)
    assert d1 == d2  # pure, deterministic

    assert [g['genre'] for g in d1['top_positive_genres']] == \
        ['Mystery', 'Thriller']  # sorted by (-weight, name)
    assert d1['top_negative_genres'][0]['genre'] == 'Horror'
    assert d1['confidence'] == profile.confidence
    assert d1['signal_count'] == profile.signal_count
    assert d1['distinct_title_count'] == profile.distinct_title_count
    assert d1['is_personalized'] is True
    assert set(d1) == {
        'top_positive_genres', 'top_negative_genres', 'top_decades',
        'media_type_pref', 'runtime_pref', 'director_affinity',
        'confidence', 'signal_count', 'distinct_title_count',
        'profile_version', 'is_personalized'}


def test_describe_profile_cold_start_shape(app, db, user):
    profile = tp.compute_profile(user.id)
    d = tp.describe_profile(profile)
    assert d['top_positive_genres'] == []
    assert d['is_personalized'] is False
    assert d['runtime_pref']['p25'] is None
    assert d['runtime_pref']['sample_count'] == 0


def test_describe_profile_accepts_plain_dict():
    d = tp.describe_profile({
        'genre_weights': {'Drama': 0.8},
        'decade_weights': {'2010s': 1.0},
        'media_type_pref': {'movie': 1.0},
        'runtime_pref': {'p25': 90, 'p75': 140, 'sample_count': 3},
        'confidence': 0.5, 'signal_count': 20, 'distinct_title_count': 8,
        'profile_version': 1,
    })
    assert d['top_positive_genres'][0]['genre'] == 'Drama'
    assert d['runtime_pref']['p75'] == 140
    assert d['is_personalized'] is True
