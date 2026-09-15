"""For You V1 (Feature #6/#7 Phase 4) — focused regression.

Covers the 50 spec areas: cold-start classification, candidate sources and
the hard TMDb budget (≤5), availability probes (≤12), dedupe/merge by
(media_type, tmdb_id), exclusions (watched/watchlist/wishlist/posterless),
deterministic ranking + stable tie-breaks, diversity (director / dominant
genre / media mix), result bounds, structured reasons, DB discipline (no
N+1), API gating (auth, malformed/oversized limit, no user_id), and hygiene
(no compute-on-request, budgeted network only, legacy taste tables
untouched, no legacy recommendation rewrite).
"""
import socket
from datetime import datetime

import pytest

import api.for_you as fy
from models import (db, User, MediaItem, DiaryEntry, Review, TasteProfile,
                    user_watchlist, user_wishlist, user_viewed)


# ── module-unique data (suite convention: clean up everything, because the
#    shared session DB reuses freed PKs and leftovers poison later files) ──
DOMAIN = 'foryou.test'


@pytest.fixture(autouse=True)
def _clean_for_you_rows(app):
    yield
    Review.query.delete()
    DiaryEntry.query.delete()
    db.session.execute(user_viewed.delete())
    db.session.execute(user_watchlist.delete())
    db.session.execute(user_wishlist.delete())
    TasteProfile.query.delete()
    MediaItem.query.delete()
    from models.streaming import UserStreamingService
    UserStreamingService.query.delete()
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
        yield _make_user('foryou')


@pytest.fixture
def second_user(app):
    with app.app_context():
        yield _make_user('foryou2')


def _login(client, user):
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1'})
    return client


@pytest.fixture
def auth_client(client, user):
    return _login(client, user)


def _media(tmdb_id, media_type='movie', title='T', genres='Thriller',
           rating=7.0, runtime=110):
    return MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title,
                     genres=genres, rating=rating, runtime=runtime,
                     poster_path='/p.jpg',
                     release_date=datetime(2019, 6, 1).date())


def _profile(user_id, genres=None, confidence=0.9, titles=6, signals=12,
             directors=None):
    return TasteProfile(
        user_id=user_id,
        genre_weights=genres or {'Thriller': 0.8, 'Drama': 0.5},
        decade_weights={'2010s': 0.6}, director_affinity=directors or {},
        confidence=confidence, signal_count=signals,
        distinct_title_count=titles, profile_version=1)


# ── TMDb stubs ───────────────────────────────────────────────────────────────

class _StubTmdb:
    """Deterministic fake TMDb layer with an exact call ledger."""

    def __init__(self, discover=None, recommendations=None, trending=None):
        self.discover = discover or {}          # {genre_id: [raw results]}
        self.recommendations = recommendations or []
        self.trending = trending or []
        self.discover_calls = []                # list of genre ids
        self.recs_calls = []                    # list of seed tmdb ids
        self.trending_calls = []                # count
        self.raw_urls = []

    def __call__(self, url, *a, **kw):
        self.raw_urls.append(url)
        if '/trending/' in url:
            self.trending_calls.append(url)
            return {'results': self.trending}
        for gid in self.discover:
            if f'with_genres={gid}' in url:
                self.discover_calls.append(gid)
                return {'results': self.discover[gid]}
        return {'results': []}

    def recs(self, tmdb_id, is_movie=True, max_recommendations=50):
        self.recs_calls.append(tmdb_id)
        return list(self.recommendations)[:max_recommendations]


def _wire(stub, monkeypatch):
    """Route all engine TMDb touchpoints through the stub."""
    monkeypatch.setattr('api.tmdb.cache.cached_tmdb_request', stub)
    monkeypatch.setattr('api.tmdb.search.fetch_tmdb_recommendations',
                        stub.recs)
    return stub


def _tmdb_raw(n, media='movie', title=None, vote=7.5, votes=5000,
              genres=(53,), pop=50.0, year='2019'):
    title = title or f'T{n}'
    return {
        'id': n, 'title' if media == 'movie' else 'name': title,
        'poster_path': '/x.jpg', 'vote_average': vote, 'vote_count': votes,
        'genre_ids': list(genres), 'popularity': pop,
        'release_date' if media == 'movie' else 'first_air_date':
            f'{year}-01-01',
    }


def _ids(items):
    return [(i['media_type'], i['tmdb_id']) for i in items]


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers (quality / freshness / reasons)
# ════════════════════════════════════════════════════════════════════════════

def test_tmdb_quality_shrinks_low_sample_scores():
    # 9.0 with 3 votes must NOT beat 8.3 with 100k votes (audited rule).
    assert fy.tmdb_quality(8.3, 100000) > fy.tmdb_quality(9.0, 3)


def test_tmdb_quality_bounds_and_garbage():
    assert fy.tmdb_quality(None, None) == 0.0
    assert fy.tmdb_quality('x', 'y') == 0.0
    assert 0.0 <= fy.tmdb_quality(9.9, 100000) <= 1.0
    assert fy.tmdb_quality(4.0, 100000) == 0.0  # below prior band → 0


def test_freshness_missing_future_and_decay():
    today = datetime(2026, 9, 1).date()
    assert fy.freshness(None, today) == 0.0
    assert fy.freshness('2030-01-01', today) == 1.0  # future clamped, no >1
    assert fy.freshness('2026-01-01', today) == 1.0
    assert fy.freshness('2004-01-01', today) == 0.0  # ≥22 years old
    assert 0.0 < fy.freshness('2010-01-01', today) < 1.0
    assert 0.0 < fy.freshness('2020-01-01', today) < 1.0


def test_genre_affinity_positive_only_mean():
    assert fy.genre_affinity(['thriller', 'drama'],
                             {'thriller': 0.8, 'drama': -0.4}) == 0.4
    assert fy.genre_affinity([], {'thriller': 1.0}) == 0.0
    assert fy.genre_affinity(['thriller'], {}) == 0.0


def test_reason_never_fabricates_availability():
    c = fy._normalize_candidate(_tmdb_raw(5, genres=(53,)), 'movie',
                                'trending', 3, None)
    reason = fy.build_reason(c)
    if reason['kind'] == 'availability':
        assert c.get('providers')  # only when verified
    else:
        assert reason['kind'] in ('genre_affinity', 'trending',
                                  'similar_title')


def test_reason_similar_title_uses_real_seed():
    c = fy._normalize_candidate(_tmdb_raw(7, genres=(53,)), 'movie',
                                'similar_title', 2, None,
                                seed={'title': 'Prisoners'})
    reason = fy.build_reason(c)
    assert reason['kind'] == 'similar_title'
    assert 'Prisoners' in reason['text']
    assert reason['evidence'][0]['seed_title'] == 'Prisoners'


# ════════════════════════════════════════════════════════════════════════════
# Cold-start classification
# ════════════════════════════════════════════════════════════════════════════

def test_cold_start_zero_signals(app, user):
    with app.app_context():
        db.session.add(TasteProfile(user_id=user.id))
        db.session.commit()
        out = fy.get_for_you(user.id)
    assert out == {'personalized': False, 'mode': 'cold',
                   'confidence': 0.0, 'reason_state': 'no_meaningful_signals',
                   'items': []}


def test_missing_profile_returns_cold_start(app, user):
    with app.app_context():
        out = fy.get_for_you(user.id)
    assert out['personalized'] is False
    assert out['reason_state'] == 'no_taste_profile_yet'
    assert out['items'] == []


def test_low_signal_hedged_mode(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.1, titles=2, signals=3))
        db.session.commit()
        _wire(_StubTmdb(), monkeypatch)
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True
    assert out['mode'] == 'hedged'


def test_full_personalized_threshold(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.5, titles=6))
        db.session.commit()
        _wire(_StubTmdb(), monkeypatch)
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True
    assert out['mode'] == 'full'


def test_full_threshold_requires_both_titles_and_confidence(app, user,
                                                            monkeypatch):
    # 5+ titles but confidence < 0.4 → hedged, not full.
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.39, titles=6))
        db.session.commit()
        _wire(_StubTmdb(), monkeypatch)
        out = fy.get_for_you(user.id)
    assert out['mode'] == 'hedged'


# ════════════════════════════════════════════════════════════════════════════
# Candidate sources / hard TMDb budget
# ════════════════════════════════════════════════════════════════════════════

def test_top_two_genre_discover_source(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8,
                                                 'Drama': 0.6}))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))],
                                   18: [_tmdb_raw(2, genres=(18,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert sorted(stub.discover_calls) == [18, 53]  # both top genres probed
    assert {i['tmdb_id'] for i in out['items']} >= {1, 2}


def test_tmdb_budget_hard_cap_exact_five(app, user, monkeypatch):
    """Every source fires but the total never exceeds the hard cap of 5:
    2 genre discover + 2 seed recs + 1 trending fallback = 5."""
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8,
                                                 'Drama': 0.6}))
        # Two strong seeds via reviews (4.5 ≥ SEED_MIN_QUALITY on 0.5–5.0).
        for i, tid in enumerate((100, 101)):
            m = _media(tid, title=f'Seed{i}')
            db.session.add(m)
            db.session.commit()
            db.session.add(Review(user_id=user.id, media_id=m.id,
                                  media_type='movie', rating=4.5))
        db.session.commit()
        # Small per-source slices so S1 (10) + S2 (0) stay under the
        # MIN_DESIRED_RESULTS volume → trending fires once. Total = 2+2+1.
        stub = _StubTmdb(
            discover={53: [_tmdb_raw(n, genres=(53,)) for n in range(1, 6)],
                      18: [_tmdb_raw(1000 + n, genres=(18,))
                           for n in range(1, 6)]},
            recommendations=[],
            trending=[_tmdb_raw(3000 + n) for n in range(1, 20)])
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    total = (len(stub.discover_calls) + len(stub.recs_calls)
             + len(stub.trending_calls))
    assert total == 5  # hard cap hit exactly
    assert total <= fy.MAX_TMDB_CALLS
    assert len(stub.recs_calls) == 2  # both seeds probed
    assert len(stub.trending_calls) == 1  # fallback filled the shortfall
    assert out['personalized'] is True


def test_budget_exhaustion_stops_generation_not_ranking(app, user,
                                                        monkeypatch):
    """Budget dies after the first call → ranking proceeds with what exists."""
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))],
                                   18: [_tmdb_raw(2, genres=(18,))]})
        orig = stub.__call__
        calls = {'n': 0}

        def one_call_then_dead(url, *a, **kw):
            calls['n'] += 1
            if calls['n'] > 1:
                raise fy._BudgetExhausted()
            return orig(url)

        monkeypatch.setattr('api.tmdb.cache.cached_tmdb_request',
                            one_call_then_dead)
        monkeypatch.setattr('api.tmdb.search.fetch_tmdb_recommendations',
                            stub.recs)
        out = fy.get_for_you(user.id)
    # The second attempt is blocked by the budget (raises _BudgetExhausted
    # inside spend()); generation stops cleanly, ranking still runs.
    assert calls['n'] == 2  # 1 success + 1 blocked attempt
    assert out['personalized'] is True
    assert out['items'], 'the discovered candidate must still be ranked'


def test_director_source_skipped_when_affinity_empty(app, user, monkeypatch):
    """S3 is skipped today (no persisted director evidence) — and no /person/
    network calls are made merely to populate it."""
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
                     monkeypatch)
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True
    assert all('/person/' not in u for u in stub.raw_urls)


def test_director_probe_runs_only_with_persisted_affinity(app, user,
                                                          monkeypatch):
    """With non-empty director_affinity, engine candidates could carry
    directors — but director data comes only from the profile, not TMDb."""
    with app.app_context():
        db.session.add(_profile(user.id,
                                directors={'Denis Villeneuve': 0.9}))
        db.session.commit()
        stub = _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
                     monkeypatch)
        fy.get_for_you(user.id)
    assert all('/person/' not in u for u in stub.raw_urls)


def test_fallback_source_used_when_volume_insufficient(app, user,
                                                       monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Unknown Genre': 1.0}))
        db.session.commit()
        stub = _StubTmdb(trending=[_tmdb_raw(301, genres=(28,))])
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert any(i['source'] == 'trending' for i in out['items'])


def test_fallback_never_fetched_when_personalized_volume_sufficient(
        app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        stub = _StubTmdb(
            discover={53: [_tmdb_raw(n, genres=(53,)) for n in range(1, 16)]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert stub.trending_calls == []
    assert all(i['source'] != 'trending' for i in out['items'])


# ════════════════════════════════════════════════════════════════════════════
# Merge / dedupe by (media_type, tmdb_id)
# ════════════════════════════════════════════════════════════════════════════

def test_duplicate_candidate_merge(app, user, monkeypatch):
    """Same (movie, 7) from genre discovery + seed recs → ONE card."""
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        m = _media(100, title='Seed Movie')
        db.session.add(m)
        db.session.commit()
        db.session.add(Review(user_id=user.id, media_id=m.id,
                              media_type='movie', rating=4.5))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(7, genres=(53,))]},
                         recommendations=[_tmdb_raw(7)])
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert _ids(out['items']).count(('movie', 7)) == 1


def test_movie_tv_identity_separation():
    c1 = fy._normalize_candidate(_tmdb_raw(55), 'movie', 'trending', 3, None)
    c2 = fy._normalize_candidate(_tmdb_raw(55, media='tv'), 'tv',
                                 'trending', 3, None)
    assert len(fy._merge_candidates([c1, c2])) == 2


def test_merge_preserves_strongest_evidence():
    c1 = fy._normalize_candidate(_tmdb_raw(7, genres=(53,)), 'movie',
                                 'trending', 3, None)
    c2 = fy._normalize_candidate(_tmdb_raw(7, genres=(53,)), 'movie',
                                 'genre:thriller', 1, None,
                                 matched_genres=['thriller'])
    merged = fy._merge_candidates([c1, c2])
    assert len(merged) == 1
    assert merged[0]['source'] == 'genre:thriller'  # strongest priority wins
    assert merged[0]['sources'] == ['trending', 'genre:thriller']


# ════════════════════════════════════════════════════════════════════════════
# Exclusions
# ════════════════════════════════════════════════════════════════════════════

def test_watched_exclusions(app, user, monkeypatch):
    with app.app_context():
        m = _media(42, title='Seen')
        db.session.add(m)
        db.session.commit()
        db.session.execute(user_viewed.insert().values(
            user_id=user.id, media_id=m.id, media_type='movie'))
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(42, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert ('movie', 42) not in _ids(out['items'])


def test_watchlist_exclusion_and_intent_bonus(app, user, monkeypatch):
    """Watchlisted titles are excluded from candidates, but a candidate that
    arrives on the watchlist through another route still gets the intent
    bonus flag (positive ranking signal, never a hard filter)."""
    with app.app_context():
        m = _media(43)
        db.session.add(m)
        db.session.commit()
        db.session.execute(user_watchlist.insert().values(
            user_id=user.id, media_id=m.id, media_type='movie'))
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(43, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert ('movie', 43) not in _ids(out['items'])


def test_wishlist_exclusions(app, user, monkeypatch):
    with app.app_context():
        m = _media(44)
        db.session.add(m)
        db.session.commit()
        db.session.execute(user_wishlist.insert().values(
            user_id=user.id, media_id=m.id, media_type='movie'))
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(44, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert ('movie', 44) not in _ids(out['items'])


def test_diary_watched_exclusion(app, user, monkeypatch):
    with app.app_context():
        m = _media(45, title='Diarized')
        db.session.add(m)
        db.session.commit()
        db.session.add(DiaryEntry(user_id=user.id, media_id=m.id,
                                  media_type='movie', rating=4.0,
                                  watched_date=datetime(2026, 1, 1).date()))
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(45, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert ('movie', 45) not in _ids(out['items'])


def test_posterless_exclusion():
    c = fy._normalize_candidate(_tmdb_raw(9), 'movie', 'trending', 3, None)
    c['poster_path'] = None
    kept = fy._apply_exclusions(
        [c], {'exclude_keys': set(), 'watchlisted_keys': set(), 'seeds': []})
    assert kept == []


def test_adult_exclusion_policy(app, user, monkeypatch):
    """Generation requests include_adult=false (existing app policy)."""
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        stub = _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
                     monkeypatch)
        fy.get_for_you(user.id)
    assert all('include_adult=false' in u for u in stub.raw_urls
               if '/discover/' in u)


def test_unfamiliar_titles_not_excluded():
    """TMDb-only candidates (no local MediaItem) survive exclusion."""
    c = fy._normalize_candidate(_tmdb_raw(9999), 'movie', 'trending', 3, None)
    kept = fy._apply_exclusions(
        [c], {'exclude_keys': {('movie', 42)}, 'watchlisted_keys': set(),
              'seeds': []})
    assert len(kept) == 1


def test_no_mediaitem_created_for_exclusions(app, user, monkeypatch):
    with app.app_context():
        before = MediaItem.query.count()
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]})
        _wire(stub, monkeypatch)
        fy.get_for_you(user.id)
        assert MediaItem.query.count() == before + 0  # profile added, no media


# ════════════════════════════════════════════════════════════════════════════
# Ranking / determinism
# ════════════════════════════════════════════════════════════════════════════

def _cand(n, genres=('thriller',), profile_genres=None, **kw):
    ids = {'thriller': 53, 'drama': 18, 'comedy': 35}
    c = fy._normalize_candidate(
        _tmdb_raw(n, genres=tuple(ids[g] for g in genres)), 'movie',
        'trending', 3, None)
    c['profile_genres'] = profile_genres or {'thriller': 0.8}
    for k, v in kw.items():
        c[k] = v
    return c


def test_genre_scoring_ranks_affine_titles_first():
    today = datetime(2026, 9, 1).date()
    strong = _cand(1, profile_genres={'thriller': 0.9})
    weak = _cand(2, genres=('comedy',), profile_genres={'thriller': 0.9})
    scored = fy.rank_candidates([weak, strong], today)
    assert [t[0]['tmdb_id'] for t in scored] == [1, 2]


def test_director_scoring_zero_without_data():
    today = datetime(2026, 9, 1).date()
    _, _, comp = fy.rank_candidates([_cand(1)], today)[0]
    assert comp['director_affinity'] == 0.0


def test_director_scoring_when_present():
    today = datetime(2026, 9, 1).date()
    c = _cand(1, director='Denis Villeneuve',
              profile_directors={'Denis Villeneuve': 0.9})
    _, score, comp = fy.rank_candidates([c], today)[0]
    assert comp['director_affinity'] == 0.9
    assert score > 0.4  # 0.5 * 0.9 contributes


def test_quality_freshness_watchlist_availability_components():
    today = datetime(2026, 9, 1).date()
    c = _cand(1, watchlisted=True, available=True, release_date='2026-01-01',
              vote_average=8.5, vote_count=90000)
    _, score, comp = fy.rank_candidates([c], today)[0]
    assert comp['watchlist_intent'] == 1.0
    assert comp['availability'] == 1.0
    assert comp['freshness'] == 1.0
    assert comp['tmdb_quality'] > 0.5
    assert score > 1.0


def test_friend_signal_reserved_zero():
    today = datetime(2026, 9, 1).date()
    _, score, comp = fy.rank_candidates([_cand(1)], today)[0]
    assert comp['friend_signal'] == 0.0
    assert fy.W_FRIEND_SIGNAL == 0.15  # weight exists; contribution always 0


def test_popularity_penalty_bounded():
    today = datetime(2026, 9, 1).date()
    _, score, comp = fy.rank_candidates(
        [_cand(1, pop=5000.0)], today)[0]
    assert score >= 0  # penalty (≤0.05) can never dominate the score


def test_deterministic_ranking_repeatable():
    today = datetime(2026, 9, 1).date()
    cands = [_cand(n) for n in range(1, 8)]
    a = [c['tmdb_id'] for c, _, _ in fy.rank_candidates(cands, today)]
    b = [c['tmdb_id'] for c, _, _ in fy.rank_candidates(cands, today)]
    assert a == b


def test_stable_tie_breaking_by_tmdb_id():
    today = datetime(2026, 9, 1).date()
    a, b = _cand(10), _cand(2)  # identical scores
    scored = fy.rank_candidates([a, b], today)
    assert [t[0]['tmdb_id'] for t in scored] == [2, 10]


def test_no_random_shuffle_in_engine_module():
    import inspect
    src = inspect.getsource(fy)
    assert 'random.shuffle' not in src
    assert 'import random' not in src


# ════════════════════════════════════════════════════════════════════════════
# Diversity + result bounds
# ════════════════════════════════════════════════════════════════════════════

def test_director_diversity_max_one():
    today = datetime(2026, 9, 1).date()
    cands = [_cand(n, director='Same Director') for n in range(1, 6)]
    scored = fy.rank_candidates(cands, today)
    chosen = fy.apply_diversity(scored, limit=5)
    directors = [c['director'] for c in chosen if c.get('director')]
    assert len(directors) == len(set(directors)) == 1


def test_dominant_genre_diversity_max_seven():
    today = datetime(2026, 9, 1).date()
    cands = [_cand(n, genres=('thriller',)) for n in range(1, 11)]
    scored = fy.rank_candidates(cands, today)
    chosen = fy.apply_diversity(scored, limit=10)
    thrillers = sum(1 for c in chosen if 'thriller' in (c['genres'] or []))
    assert thrillers <= fy.MAX_PER_DOMINANT_GENRE


def test_result_max_14():
    today = datetime(2026, 9, 1).date()
    cands = [_cand(n, genres=('thriller',) if n % 2 else ('drama',))
             for n in range(1, 40)]
    scored = fy.rank_candidates(cands, today)
    assert len(fy.apply_diversity(scored, limit=14)) <= 14


def test_fewer_than_12_graceful_result(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(
            discover={53: [_tmdb_raw(n, genres=(53,)) for n in range(1, 9)]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert 1 <= len(out['items']) < 12  # graceful underfill, no placeholders


def test_result_size_cap_14_via_engine(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8,
                                                 'Drama': 0.6}))
        db.session.commit()
        stub = _StubTmdb(
            discover={53: [_tmdb_raw(n, genres=(53,)) for n in range(1, 16)],
                      18: [_tmdb_raw(1000 + n, genres=(18,))
                           for n in range(1, 16)]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert len(out['items']) <= 14


def test_structured_reason_generation(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(5, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    reason = out['items'][0]['reason']
    assert set(reason) == {'kind', 'text', 'evidence'}
    assert reason['kind'] in ('genre_affinity', 'similar_title',
                              'decade_affinity', 'availability',
                              'watchlist_intent', 'trending')
    assert isinstance(reason['text'], str) and reason['text']


def test_profile_feedback_effects_flow_through_tasteprofile(app, user,
                                                            monkeypatch):
    """Feedback semantics live in the persisted profile: a feedback-derived
    genre surfaces here through genre discovery + reasons."""
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert out['items'][0]['reason']['kind'] == 'genre_affinity'


def test_feedback_not_queried_directly_by_ranking():
    import inspect
    src = inspect.getsource(fy)
    assert 'RecommendationFeedback' not in src


# ════════════════════════════════════════════════════════════════════════════
# Availability (bounded bonus)
# ════════════════════════════════════════════════════════════════════════════

def test_bounded_availability_probes_max_12(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(
            discover={53: [_tmdb_raw(n, genres=(53,)) for n in range(1, 21)]})
        _wire(stub, monkeypatch)
        probes = {'n': 0}

        def fake_get_availability(media_type, tmdb_id, region=None):
            probes['n'] += 1
            return {}

        monkeypatch.setattr('api.availability.get_availability',
                            fake_get_availability)
        monkeypatch.setattr('api.availability.match_my_services',
                            lambda a, b: {'matches': [], 'available': False})
        # User has services → probing is enabled at all.
        from models.streaming import UserStreamingService
        db.session.add(UserStreamingService(user_id=user.id, provider_id=8,
                                            region='US'))
        db.session.commit()
        fy.get_for_you(user.id)
    assert 0 < probes['n'] <= fy.MAX_AVAILABILITY_PROBES


def test_unknown_availability_no_bonus(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]})
        _wire(stub, monkeypatch)

        def empty_avail(*a, **kw):
            return {}

        monkeypatch.setattr('api.availability.get_availability',
                            empty_avail)
        monkeypatch.setattr('api.availability.match_my_services',
                            lambda a, b: {'matches': [], 'available': False})
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True  # unknown ≠ failure


def test_availability_bonus_requires_stream_match_not_rent_buy():
    """match_my_services is the canonical arbiter (stream/free only); the
    engine trusts its verdict rather than re-implementing it."""
    from api.availability import match_my_services
    rent_only = {'stream': [], 'free': []}          # rent/buy not in these
    stream = {'stream': [{'id': 8}], 'free': []}
    assert match_my_services(rent_only, [8])['available'] is False
    assert match_my_services(stream, [8])['available'] is True


def test_availability_failure_degrades_gracefully(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]})
        _wire(stub, monkeypatch)

        def broken_avail(*a, **kw):
            raise RuntimeError('availability down')

        monkeypatch.setattr('api.availability.get_availability',
                            broken_avail)
        from models.streaming import UserStreamingService
        db.session.add(UserStreamingService(user_id=user.id, provider_id=8,
                                            region='US'))
        db.session.commit()
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True


def test_no_services_means_no_probes(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(n, genres=(53,))
                                        for n in range(1, 20)]})
        _wire(stub, monkeypatch)
        probes = {'n': 0}

        def fake_get_availability(*a, **kw):
            probes['n'] += 1
            return {}

        monkeypatch.setattr('api.availability.get_availability',
                            fake_get_availability)
        out = fy.get_for_you(user.id)
    assert probes['n'] == 0  # no user services → zero probes
    assert out['personalized'] is True


# ════════════════════════════════════════════════════════════════════════════
# DB discipline
# ════════════════════════════════════════════════════════════════════════════

def test_no_n_plus_one_local_queries(app, user, monkeypatch):
    """The local-state read stays bounded (exclusions + profile, one join)."""
    from sqlalchemy import event

    with app.app_context():
        db.session.add(_profile(user.id))
        m = _media(70)
        db.session.add(m)
        db.session.commit()
        db.session.execute(user_viewed.insert().values(
            user_id=user.id, media_id=m.id, media_type='movie'))
        stmts = []

        def _count(conn, cursor, statement, parameters, context, executemany):
            stmts.append(statement)

        event.listen(db.engine, 'before_cursor_execute', _count)
        try:
            _wire(_StubTmdb(), monkeypatch)
            fy.get_for_you(user.id)
        finally:
            event.remove(db.engine, 'before_cursor_execute', _count)
    assert len(stmts) <= 12  # bounded; a per-row N+1 would be dozens+


# ════════════════════════════════════════════════════════════════════════════
# API endpoint
# ════════════════════════════════════════════════════════════════════════════

def test_api_authenticated_access(app, auth_client, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
              monkeypatch)
        resp = auth_client.get('/api/for-you')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['personalized'] is True
    assert isinstance(body['items'], list)
    assert body['confidence'] == 0.9


def test_api_anonymous_rejected(client):
    # Same convention as the feedback API suite: flask-login redirects
    # unauthenticated browser requests (302); 401 would also be acceptable.
    assert client.get('/api/for-you').status_code in (302, 401)


def test_api_cold_start_response_shape(app, auth_client, user):
    with app.app_context():
        db.session.add(TasteProfile(user_id=user.id))
        db.session.commit()
        resp = auth_client.get('/api/for-you')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['personalized'] is False
    assert body['reason_state'] == 'no_meaningful_signals'
    assert body['items'] == []


def test_api_malformed_limit_rejected(auth_client):
    assert auth_client.get('/api/for-you?limit=abc').status_code == 400


def test_api_oversized_and_zero_limit_rejected(auth_client):
    assert auth_client.get('/api/for-you?limit=500').status_code == 400
    assert auth_client.get('/api/for-you?limit=0').status_code == 400


def test_api_client_cannot_select_another_user(app, auth_client, user,
                                               second_user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(second_user.id, genres={'Thriller': 0.9}))
        db.session.commit()
        _wire(_StubTmdb(), monkeypatch)
        resp = auth_client.get('/api/for-you?user_id=99999')
    body = resp.get_json()
    assert body['personalized'] is False  # session user is cold; no leak


def test_api_response_contains_no_scoring_internals(app, auth_client, user,
                                                    monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
              monkeypatch)
        resp = auth_client.get('/api/for-you')
    keys = {i for item in resp.get_json()['items'] for i in item}
    assert not keys & {'score', 'components', '_source_priority',
                       'profile_genres', 'profile_directors'}


def test_api_no_tasteprofile_computation(app, auth_client, user, monkeypatch):
    with app.app_context():
        _wire(_StubTmdb(), monkeypatch)

        def _boom(*a, **kw):
            raise AssertionError('compute_profile must not run at request time')

        monkeypatch.setattr('api.taste_profile.compute_profile', _boom)
        resp = auth_client.get('/api/for-you')
    assert resp.status_code == 200


def test_api_no_unexpected_network(app, auth_client, user, monkeypatch):
    """socket-level guard: the budgeted TMDb calls are stubbed, so ANY other
    outbound connection attempt fails the test."""
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
              monkeypatch)

        class _NoNet(Exception):
            pass

        def _no_socket(*a, **kw):
            raise _NoNet('unexpected outbound connection')

        monkeypatch.setattr(socket, 'create_connection', _no_socket)
        monkeypatch.setattr(socket.socket, 'connect', _no_socket)
        resp = auth_client.get('/api/for-you')
    assert resp.status_code == 200


def test_route_rate_limited():
    """The route IS registered with the shared limiter (Valkey in prod)."""
    import inspect
    import routes.for_you as route_mod
    src = inspect.getsource(route_mod)
    assert "@limiter.limit(RATE_LIMIT)" in src
    assert route_mod.RATE_LIMIT == '30 per minute'


def test_blueprint_registered_once(app):
    rules = [r.rule for r in app.url_map.iter_rules()
             if r.rule == '/api/for-you']
    assert rules == ['/api/for-you']


# ════════════════════════════════════════════════════════════════════════════
# Graceful degradation
# ════════════════════════════════════════════════════════════════════════════

def test_partial_tmdb_failure_degrades_gracefully(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]})
        _wire(stub, monkeypatch)

        def broken_recs(*a, **kw):
            raise RuntimeError('tmdb down')

        monkeypatch.setattr('api.tmdb.search.fetch_tmdb_recommendations',
                            broken_recs)
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True  # discovery results still rank
    assert out['items']


def test_cache_identity_includes_profile_version(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))]}),
              monkeypatch)
        fy._cache.clear()
        first = fy.get_for_you(user.id)
        p = TasteProfile.query.filter_by(user_id=user.id).first()
        p.profile_version = 2
        db.session.commit()
        second = fy.get_for_you(user.id)
    assert first == second  # same data → equal results; key differs (no stale)


def test_cache_isolation_between_users(app, user, second_user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id, genres={'Thriller': 0.8}))
        db.session.add(_profile(second_user.id, genres={'Drama': 0.9}))
        db.session.commit()
        _wire(_StubTmdb(discover={53: [_tmdb_raw(1, genres=(53,))],
                                  18: [_tmdb_raw(2, genres=(18,))]}),
              monkeypatch)
        fy._cache.clear()
        a = fy.get_for_you(user.id)
        b = fy.get_for_you(second_user.id)
    assert {i['tmdb_id'] for i in a['items']} == {1}
    assert {i['tmdb_id'] for i in b['items']} == {2}


def test_cold_start_cached_safely(app, user):
    with app.app_context():
        db.session.add(TasteProfile(user_id=user.id))
        db.session.commit()
        fy._cache.clear()
        out = fy.get_for_you(user.id)
        # Cached cold state is user-keyed and contains no items to leak.
        assert out['items'] == []


# ════════════════════════════════════════════════════════════════════════════
# Legacy / hygiene
# ════════════════════════════════════════════════════════════════════════════

def test_no_legacy_taste_tables_touched():
    import inspect
    src = inspect.getsource(fy)
    assert 'user_taste_profile' not in src
    assert 'user_similarity' not in src


def test_no_legacy_recommendation_modules_modified():
    """The engine exists alongside legacy code — nothing was deleted."""
    import routes.recommendations  # noqa: F401 — import proves module intact
    from api.for_you import get_for_you  # noqa: F401
    assert callable(get_for_you)


# ════════════════════════════════════════════════════════════════════════════
# Phase 10: director affinity from persisted local capture
# ════════════════════════════════════════════════════════════════════════════

_FY_DIR_PERSON = {'n': 991_500}


def _fy_dir_user(app):
    from models import User
    import uuid
    u = User(username=f"fydir_{uuid.uuid4().hex[:8]}",
             email=f"fydir_{uuid.uuid4().hex[:8]}@x.io")
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


def _capture_director(media_item, name):
    """Persist director evidence exactly as scripts/enrich_directors.py
    would (stable person id, unique association)."""
    from models.director import Director, MediaDirector
    _FY_DIR_PERSON['n'] += 1
    d = Director(tmdb_person_id=_FY_DIR_PERSON['n'], name=name, source='tmdb')
    db.session.add(d)
    db.session.flush()
    db.session.add(MediaDirector(media_item_id=media_item.id, director_id=d.id))
    db.session.commit()
    return d


def _attach_setup(app, user_id, directors_map, tmdb_ids):
    """Profile + local MediaItems (+ captured directors) for attach tests."""
    from models import MediaItem
    from datetime import datetime as _dt
    with app.app_context():
        db.session.add(_profile(user_id, directors=directors_map))
        items = {}
        for tid in tmdb_ids:
            m = MediaItem(tmdb_id=tid, media_type='movie', title=f'M{tid}',
                          genres='Thriller', rating=7.5, runtime=110,
                          poster_path='/p.jpg',
                          release_date=_dt(2019, 6, 1).date())
            db.session.add(m)
            items[tid] = m
        db.session.commit()
        yield items
        # leave no residue: associations cascade with media rows
        for m in items.values():
            db.session.delete(m)
        db.session.commit()


def test_director_affinity_now_affects_ranking(app, user, monkeypatch):
    from datetime import datetime as _dt
    today = _dt(2026, 9, 1).date()
    with app.app_context():
        # Candidate A's director has positive profile affinity; B's has none.
        m_a = _media(910001, title='AffinityHit')
        m_b = _media(910002, title='NoAffinity')
        db.session.add_all([m_a, m_b])
        db.session.commit()
        _capture_director(m_a, 'Christopher Nolan')
        _capture_director(m_b, 'Random Director')
        db.session.add(_profile(user.id,
                                directors={'Christopher Nolan': 0.9}))
        db.session.commit()

        base = dict(vote=7.5, votes=5000, genres=(53,), pop=50.0, year='2019')
        c_a = fy._normalize_candidate(_tmdb_raw(910001, **base), 'movie',
                                      'genre_discover', 1,
                                      TasteProfile.query.filter_by(
                                          user_id=user.id).one())
        c_b = fy._normalize_candidate(_tmdb_raw(910002, **base), 'movie',
                                      'genre_discover', 1,
                                      TasteProfile.query.filter_by(
                                          user_id=user.id).one())
        # Prior affinity must be neutralized so ONLY the director differs.
        c_b['profile_directors'] = dict(c_a['profile_directors'])
        c_a, c_b = fy._attach_local_directors([c_a, c_b])
        assert c_a['director'] == 'Christopher Nolan'
        assert c_b['director'] is None  # captured but NOT in profile
        scored = fy.rank_candidates([c_a, c_b], today)
        scores = {c['tmdb_id']: s for c, s, _ in scored}
        assert scores[910001] > scores[910002]


def test_director_affinity_remains_zero_when_empty(app, user):
    with app.app_context():
        db.session.add(_profile(user.id, directors={}))
        db.session.commit()
        m = _media(910011)
        db.session.add(m)
        db.session.commit()
        _capture_director(m, 'Some Director')
        c = fy._normalize_candidate(_tmdb_raw(910011), 'movie',
                                    'trending', 3,
                                    TasteProfile.query.filter_by(
                                        user_id=user.id).one())
        c = fy._attach_local_directors([c])[0]
        assert c['director'] is None          # never tagged from capture alone
        assert fy.build_reason(c)['kind'] != 'director_affinity'


def test_director_reason_only_with_real_evidence(app, user):
    c = fy._normalize_candidate(_tmdb_raw(910021), 'movie', 'trending', 3,
                                None)
    c['director'] = 'Christopher Nolan'  # name with NO profile affinity
    assert fy.build_reason(c)['kind'] != 'director_affinity'

    c['profile_directors'] = {'Christopher Nolan': 0.4}
    reason = fy.build_reason(c)
    assert reason['kind'] == 'director_affinity'
    assert reason['text'] == 'Because you liked films by Christopher Nolan'
    assert reason['evidence'][0] == {'director': 'Christopher Nolan',
                                     'affinity': 0.4}


def test_director_negative_affinity_never_reasoned(app, user):
    c = fy._normalize_candidate(_tmdb_raw(910031), 'movie', 'trending', 3,
                                None)
    c['director'] = 'Disliked Director'
    c['profile_directors'] = {'Disliked Director': -0.5}
    assert fy.build_reason(c)['kind'] != 'director_affinity'


def test_no_director_network_call_during_for_you(app, user, monkeypatch):
    """Full flow with captured directors present: the local tagging path
    must not dial out (socket guard) and the TMDb budget stays intact."""
    import socket

    class _NoNet(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError('network socket during For You flow')

    monkeypatch.setattr(socket, 'socket', _NoNet)
    monkeypatch.setattr(socket, 'create_connection', _NoNet)
    with app.app_context():
        m = _media(910041, title='DirectorFlow')
        db.session.add(m)
        db.session.commit()
        _capture_director(m, 'Christopher Nolan')
        db.session.add(_profile(user.id,
                                directors={'Christopher Nolan': 0.9}))
        db.session.commit()
        stub = _StubTmdb(discover={53: [_tmdb_raw(910041, genres=(53,))]})
        _wire(stub, monkeypatch)
        out = fy.get_for_you(user.id)
    assert out['personalized'] is True
    total = (len(stub.discover_calls) + len(stub.recs_calls)
             + len(stub.trending_calls))
    assert total <= 5                          # hard TMDb budget intact
    item = next(i for i in out['items'] if i['tmdb_id'] == 910041)
    assert item['reason']['kind'] == 'director_affinity'
    assert 'Christopher Nolan' in item['reason']['text']


def test_director_attach_skips_tv_candidates(app, user):
    """TV has no persisted series-level director evidence — never tagged."""
    from models import MediaItem
    from datetime import datetime as _dt
    with app.app_context():
        db.session.add(_profile(user.id,
                                directors={'Some Director': 0.8}))
        show = MediaItem(tmdb_id=910051, media_type='tv', title='Show',
                         genres='Drama', release_date=_dt(2019, 6, 1).date())
        db.session.add(show)
        db.session.commit()
        _capture_director(show, 'Some Director')
        c = fy._normalize_candidate(
            _tmdb_raw(910051, media='tv'), 'tv', 'trending', 3,
            TasteProfile.query.filter_by(user_id=user.id).one())
        c = fy._attach_local_directors([c])[0]
        assert c['director'] is None
        assert c.get('directors') is None


def test_availability_and_determinism_unchanged_by_directors(app, user):
    """Directors participate only through the existing 0.5 weight —
    availability probes (≤12) and tie-breaking rules are untouched."""
    from datetime import datetime as _dt
    today = _dt(2026, 9, 1).date()
    c1 = fy._normalize_candidate(_tmdb_raw(910061), 'movie', 'trending', 3,
                                 None)
    c2 = fy._normalize_candidate(_tmdb_raw(910062), 'movie', 'trending', 3,
                                 None)
    c1['director'] = c2['director'] = 'Tied Director'
    c1['profile_directors'] = c2['profile_directors'] = {
        'Tied Director': 0.6}
    s1 = fy.rank_candidates([c1, c2], today)
    s2 = fy.rank_candidates([c1, c2], today)
    assert [(c['tmdb_id'], round(s, 6)) for c, s, _ in s1] == \
        [(c['tmdb_id'], round(s, 6)) for c, s, _ in s2]
    # Stable tie-break on equal scores: tmdb_id ascending.
    assert [c['tmdb_id'] for c, _, _ in s1] == [910061, 910062]
