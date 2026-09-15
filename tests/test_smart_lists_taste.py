"""Smart Lists 'matches_my_taste' filter (Feature #6, Phase 12).

Covers the pure taste-match helpers (component scores, weighted
combination, threshold boundaries), the canonical cold-start gate, the
opt-in engine integration (eligibility only — never reordering),
zero-work when the filter is off, and hygiene: no network, no
RecommendationFeedback reads, no For You invocation, no recomputation,
existing result schema unchanged.
"""
import socket
from datetime import datetime

import pytest

from models import db, MediaItem, user_watchlist
from models.smart_lists import SmartList
from models.taste_profile import TasteProfile
from api import smart_lists as sl
from api.taste_profile import (
    TASTE_MATCH_THRESHOLD, TASTE_MATCH_WEIGHTS,
    genre_match_score, director_match_score, decade_match_score,
    media_type_match_score, runtime_match_score,
    taste_match_score, taste_match_inputs, taste_profile_eligible,
)


# ── helpers ──────────────────────────────────────────────────────────────────

_TMDB = {'n': 860000}


def _uid(prefix):
    import uuid
    return f'{prefix}_{uuid.uuid4().hex[:8]}'


@pytest.fixture
def user(app, db):
    from models import User
    u = User(username=_uid('taste'), email=f'{_uid("taste")}@x.io')
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def media_factory(app, db):
    def _make(media_type='movie', title='Dune', genres='Thriller, Drama',
              runtime=120, year=2021, rating=7.5, add_to_watchlist=True):
        _TMDB['n'] += 1
        m = MediaItem(
            tmdb_id=_TMDB['n'], media_type=media_type, title=title,
            genres=genres, runtime=runtime, rating=rating,
            release_date=datetime(year, 6, 15).date(),
            poster_path=f'/{_TMDB["n"]}.jpg')
        db.session.add(m)
        db.session.flush()
        if add_to_watchlist:
            db.session.execute(user_watchlist.insert().values(
                user_id=user_id_holder[0], media_id=m.id,
                media_type=media_type, priority='medium'))
        db.session.commit()
        return m
    return _make


user_id_holder = [None]


@pytest.fixture(autouse=True)
def _bind_user(user):
    user_id_holder[0] = user.id


def _eligible_profile(user_id, genre_weights=None, decade_weights=None,
                      directors=None, media_type_pref=None, runtime_pref=None):
    p = TasteProfile(user_id=user_id)
    p.genre_weights = genre_weights or {}
    p.decade_weights = decade_weights or {}
    p.director_affinity = directors or {}
    p.media_type_pref = media_type_pref or {}
    p.runtime_pref = runtime_pref or {}
    p.confidence = 0.7
    p.signal_count = 12
    p.distinct_title_count = 6
    p.profile_version = 1
    db.session.add(p)
    db.session.commit()
    return p


def _ineligible_profile(user_id, confidence=0.2, titles=2):
    p = _eligible_profile(user_id)
    p.confidence = confidence
    p.distinct_title_count = titles
    db.session.commit()
    return p


# ════════════════════════════════════════════════════════════════════════════
# Pure component helpers (spec §24: 6–18, §25)
# ════════════════════════════════════════════════════════════════════════════

def test_genre_match_positive_and_duplicate_handling():
    # Duplicates (any spelling) must not double-count — mean over distinct.
    assert genre_match_score(
        {'Thriller': 0.8}, ['Thriller', 'thriller', 'THRILLER ']) == 0.8
    assert genre_match_score({'Thriller': 0.8}, ['Thriller']) == 0.8


def test_genre_match_multi_genre_mean_not_sum():
    # Mean, not sum: two genres never outscore one equally-good genre.
    two = genre_match_score({'A': 0.6, 'B': 0.4}, ['A', 'B'])
    assert two == 0.5
    one = genre_match_score({'A': 0.6}, ['A'])
    assert one == 0.6


def test_genre_match_negative_evidence_respected():
    # A negative profile weight flows straight through the mean.
    assert genre_match_score({'Horror': -0.7}, ['Horror']) == -0.7


def test_genre_match_empty_sides():
    assert genre_match_score({}, ['Thriller']) == 0.0
    assert genre_match_score({'Thriller': 0.9}, []) == 0.0
    assert genre_match_score({'Thriller': 0.9}, None) == 0.0


def test_director_match_best_positive_only():
    assert director_match_score(
        {'A': 0.3, 'B': 0.5}, ['A', 'B']) == 0.5
    # Negative affinity must never count as a match.
    assert director_match_score({'C': -0.9}, ['C']) == 0.0


def test_director_match_empty_profile():
    assert director_match_score({}, ['Villeneuve']) == 0.0
    assert director_match_score({'Villeneuve': 0.5}, []) == 0.0


def test_decade_match_canonical_label():
    assert decade_match_score({'2010s': 0.3}, '2010s') == 0.3
    assert decade_match_score({'2010s': 0.3}, '1990s') == 0.0
    assert decade_match_score({'2010s': 0.3}, None) == 0.0


def test_media_type_match_prefers_movie_but_not_gate():
    pref = {'movie': 0.7, 'tv': 0.3}
    assert media_type_match_score(pref, 'movie') == 0.7
    # The other type scores its (lower) share — never excluded.
    assert media_type_match_score(pref, 'tv') == 0.3
    assert media_type_match_score({'movie': 1.0}, 'tv') == 0.0


def test_runtime_match_inside_boundary_outside_missing():
    pref = {'p25': 90, 'p75': 140}
    assert runtime_match_score(pref, 100) == 1.0      # inside
    assert runtime_match_score(pref, 90) == 1.0       # boundary (inclusive)
    assert runtime_match_score(pref, 140) == 1.0
    assert 0.0 < runtime_match_score(pref, 155) < 1.0  # near boundary
    assert runtime_match_score(pref, 155) == 0.4      # 15 of 25 out
    assert runtime_match_score(pref, 152.5) == 0.5    # halfway through taper
    assert runtime_match_score(pref, 165) == 0.0      # full taper distance
    assert runtime_match_score(pref, 40) == 0.0       # far outside
    assert runtime_match_score(pref, None) == 0.0     # missing runtime
    assert runtime_match_score({}, 100) == 0.0        # missing interval
    assert runtime_match_score({'p25': 100, 'p75': 100}, 100) == 0.0  # degenerate


def test_threshold_and_weights_constants():
    assert TASTE_MATCH_THRESHOLD == 0.45
    assert TASTE_MATCH_WEIGHTS == {
        'genre': 0.50, 'director': 0.20, 'decade': 0.10,
        'media_type': 0.10, 'runtime': 0.10}


def test_score_weighted_combination():
    inputs = taste_match_inputs({
        'genre_weights': {'Thriller': 1.0},
        'director_affinity': {'Villeneuve': 1.0},
        'decade_weights': {'2020s': 1.0},
        'media_type_pref': {'movie': 1.0},
        'runtime_pref': {'p25': 90, 'p75': 140},
        'confidence': 0.8, 'distinct_title_count': 7})
    candidate = {
        'genres': ['Thriller'], 'directors': ['Villeneuve'],
        'decade': '2020s', 'media_type': 'movie', 'runtime': 110}
    # All components maxed: 0.5 + 0.2 + 0.1 + 0.1 + 0.1 = 1.0
    assert taste_match_score(inputs, candidate) == 1.0


def test_score_missing_dimensions_are_zero_not_negative():
    # Only genre evidence → 0.50 * genre; nothing else fabricates value.
    inputs = taste_match_inputs({
        'genre_weights': {'Thriller': 0.9}})
    candidate = {'genres': ['Thriller']}
    assert taste_match_score(inputs, candidate) == 0.45


def test_score_deterministic_repeated():
    inputs = taste_match_inputs({'genre_weights': {'Drama': 0.6}})
    candidate = {'genres': ['Drama'], 'media_type': 'movie', 'runtime': 100}
    scores = {taste_match_score(inputs, candidate) for _ in range(25)}
    assert len(scores) == 1


# ════════════════════════════════════════════════════════════════════════════
# Threshold boundaries (spec §24: 19–21)
# ════════════════════════════════════════════════════════════════════════════

def test_threshold_boundaries_just_below_exact_above():
    just_below = taste_match_inputs(
        {'genre_weights': {'G': 0.449 / 0.50 - 1e-9}})
    candidate = {'genres': ['G']}
    score = taste_match_score(just_below, candidate)
    assert score < TASTE_MATCH_THRESHOLD

    exact = taste_match_inputs({'genre_weights': {'G': 0.9}})
    # 0.50 * 0.9 = 0.45 → exactly at threshold → match (>= semantics)
    assert taste_match_score(exact, {'genres': ['G']}) == TASTE_MATCH_THRESHOLD

    above = taste_match_inputs({'genre_weights': {'G': 1.0}})
    assert taste_match_score(above, {'genres': ['G']}) > TASTE_MATCH_THRESHOLD


# ════════════════════════════════════════════════════════════════════════════
# Cold-start gate (spec §24: 3–5)
# ════════════════════════════════════════════════════════════════════════════

def test_eligibility_gate_requires_titles_and_confidence():
    assert not taste_profile_eligible(
        {'distinct_title_count': 4, 'confidence': 0.9})
    assert not taste_profile_eligible(
        {'distinct_title_count': 5, 'confidence': 0.39})
    assert taste_profile_eligible(
        {'distinct_title_count': 5, 'confidence': 0.4})


def test_engine_cold_start_no_profile_returns_empty(user, media_factory):
    media_factory()  # watchlist item exists but no TasteProfile at all
    lst = sl.__dict__  # noqa: F841 — module import sanity
    from models.smart_lists import SmartList
    smart = SmartList(user_id=user.id, name='T', scope='watchlist',
                      sort='date_added')
    smart.filters = {'matches_my_taste': True}
    db.session.add(smart)
    db.session.commit()
    result = sl.evaluate_smart_list(smart)
    assert result['items'] == []
    assert result['total'] == 0


def test_engine_cold_start_low_confidence_returns_empty(
        user, media_factory, db):
    media_factory()
    _ineligible_profile(user.id, confidence=0.3, titles=6)
    from models.smart_lists import SmartList
    smart = SmartList(user_id=user.id, name='T', scope='watchlist',
                      sort='date_added')
    smart.filters = {'matches_my_taste': True}
    db.session.add(smart)
    db.session.commit()
    result = sl.evaluate_smart_list(smart)
    assert result['total'] == 0


def test_engine_cold_start_few_distinct_titles_returns_empty(
        user, media_factory, db):
    media_factory()
    _ineligible_profile(user.id, confidence=0.9, titles=4)
    from models.smart_lists import SmartList
    smart = SmartList(user_id=user.id, name='T', scope='watchlist',
                      sort='date_added')
    smart.filters = {'matches_my_taste': True}
    db.session.add(smart)
    db.session.commit()
    result = sl.evaluate_smart_list(smart)
    assert result['total'] == 0


# ════════════════════════════════════════════════════════════════════════════
# Engine integration (spec §24: 1, 19, 26, 27, 30, 31)
# ════════════════════════════════════════════════════════════════════════════

def _make_list(user, filters, scope='watchlist', sort='date_added'):
    from models.smart_lists import SmartList
    smart = SmartList(user_id=user.id, name='T', scope=scope, sort=sort)
    smart.filters = filters
    db.session.add(smart)
    db.session.commit()
    return smart


def test_engine_filter_true_matches_eligible_titles(user, media_factory, db):
    thriller = media_factory(title='Thriller Hit', genres='Thriller')
    drama = media_factory(title='Drama Item', genres='Drama')
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0},
                      media_type_pref={'movie': 1.0})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    ids = {i['id'] for i in result['items']}
    assert thriller.tmdb_id in ids
    assert drama.tmdb_id not in ids


def test_engine_combines_with_existing_filters_anded(
        user, media_factory, db):
    media_factory(title='Old Thriller', genres='Thriller', year=2001)
    new_thriller = media_factory(title='New Thriller', genres='Thriller',
                                 year=2021)
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})
    result = sl.evaluate_smart_list(_make_list(user, {
        'matches_my_taste': True, 'year': 2021}))
    ids = {i['id'] for i in result['items']}
    assert ids == {new_thriller.tmdb_id}


def test_engine_preserves_existing_sort(user, media_factory, db):
    low = media_factory(title='Low Rated', genres='Thriller', rating=5.0)
    high = media_factory(title='High Rated', genres='Thriller', rating=9.0)
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})
    result = sl.evaluate_smart_list(
        _make_list(user, {'matches_my_taste': True}, sort='rating'))
    order = [i['id'] for i in result['items']]
    assert order == [high.tmdb_id, low.tmdb_id]


def test_engine_multiple_candidates_and_movie_tv_separation(
        user, media_factory, db):
    m1 = media_factory(title='M1', genres='Thriller')
    m2 = media_factory(title='M2', genres='Thriller')
    show = media_factory(title='Show', genres='Thriller', media_type='tv')
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0},
                      media_type_pref={'movie': 1.0, 'tv': 0.0})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    ids = {i['id'] for i in result['items']}
    assert {m1.tmdb_id, m2.tmdb_id} <= ids
    # TV is scored lower by media-type share but NOT hard-gated; with 1.0
    # genre evidence it still clears the threshold.
    assert show.tmdb_id in ids


def test_engine_negative_genre_keeps_conservative(
        user, media_factory, db):
    # Horror-negative profile: a pure-horror title must not match.
    media_factory(title='Scary', genres='Horror')
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0,
                                              'Horror': -0.8})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    assert result['total'] == 0


def test_engine_director_evidence_contributes(user, media_factory, db):
    m = media_factory(title='Directed', genres='Documentary')
    from models.director import Director, MediaDirector
    d = Director(tmdb_person_id=777001, name='Fav Director', source='tmdb')
    db.session.add(d)
    db.session.flush()
    db.session.add(MediaDirector(media_item_id=m.id, director_id=d.id))
    # Weak/no genre evidence; director affinity alone must carry the match:
    # 0.20 * 1.0 = 0.2 … plus media-type 0.10 * 1.0 → 0.30 < 0.45. So add
    # modest genre evidence to cross the threshold realistically.
    _eligible_profile(user.id,
                      genre_weights={'Documentary': 0.6},
                      directors={'Fav Director': 1.0},
                      media_type_pref={'movie': 1.0})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    ids = {i['id'] for i in result['items']}
    assert ids == {m.tmdb_id}


def test_engine_runtime_evidence_contributes(user, media_factory, db):
    m = media_factory(title='Snappy', genres='Unknown', runtime=95)
    _eligible_profile(user.id,
                      genre_weights={'Unknown': 0.6},
                      runtime_pref={'p25': 80, 'p75': 110},
                      media_type_pref={'movie': 1.0})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    assert {i['id'] for i in result['items']} == {m.tmdb_id}


def test_engine_decade_evidence_contributes(user, media_factory, db):
    m = media_factory(title='Retro', genres='Unknown', year=1994)
    _eligible_profile(user.id,
                      genre_weights={'Unknown': 0.6},
                      decade_weights={'1990s': 1.0},
                      media_type_pref={'movie': 1.0})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    assert {i['id'] for i in result['items']} == {m.tmdb_id}


def test_engine_missing_metadata_scores_zero_components(
        user, media_factory, db):
    # No runtime → runtime component 0; release date None → decade 0.
    _TMDB['n'] += 1
    m = MediaItem(tmdb_id=_TMDB['n'], media_type='movie', title='Bare',
                  genres='Thriller', runtime=None, rating=7.0,
                  release_date=None, poster_path='x.jpg')
    db.session.add(m)
    db.session.flush()
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=m.id, media_type='movie',
        priority='medium'))
    db.session.commit()
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0},
                      runtime_pref={'p25': 80, 'p75': 110},
                      decade_weights={'2020s': 1.0})
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    # genre 0.5 + media_type 0.1 = 0.60 >= 0.45 → still matches
    assert {i['id'] for i in result['items']} == {m.tmdb_id}


# ════════════════════════════════════════════════════════════════════════════
# Validation (spec §24: 2, 21)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('bad', [1, 'true', 0, None, 1.0, {'a': 1}])
def test_filter_rejects_non_boolean(bad):
    with pytest.raises(ValueError):
        sl.validate_config('watchlist', {'matches_my_taste': bad}, 'rating')


def test_filter_accepts_false_and_true():
    # Canonical table keeps known keys: False is preserved (engine treats
    # any falsy value as off) and True round-trips.
    assert sl.validate_config(
        'watchlist', {'matches_my_taste': False}, 'rating') == {
        'matches_my_taste': False}
    assert sl.validate_config(
        'watchlist', {'matches_my_taste': True}, 'rating') == {
        'matches_my_taste': True}


def test_unknown_filters_still_rejected():
    with pytest.raises(ValueError):
        sl.validate_config('watchlist', {'matches_my_directors': True},
                           'rating')


# ════════════════════════════════════════════════════════════════════════════
# Filter-off behavior / profile-load count (spec §24: 25, 28, 29; §18, §23)
# ════════════════════════════════════════════════════════════════════════════

def test_filter_off_never_touches_taste_profile(user, media_factory, db,
                                                monkeypatch):
    media_factory()
    import api.taste_profile as tp

    def _boom(*a, **kw):
        raise AssertionError('TasteProfile work must not happen')

    monkeypatch.setattr(tp, 'get_profile', _boom)
    # Filter absent → no profile work at all:
    result = sl.evaluate_smart_list(_make_list(user, {}))
    assert result['total'] == 1
    # Filter explicitly false → still no profile work:
    result = sl.evaluate_smart_list(
        _make_list(user, {'matches_my_taste': False}))
    assert result['total'] == 1


def test_profile_loaded_exactly_once_when_enabled(user, media_factory, db,
                                                  monkeypatch):
    media_factory(genres='Thriller')
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0},
                      media_type_pref={'movie': 1.0})
    import api.taste_profile as tp
    loads = []
    real_get = tp.get_profile

    def counting_get(user_id, create=False):
        loads.append(user_id)
        return real_get(user_id, create=create)

    monkeypatch.setattr(tp, 'get_profile', counting_get)
    # Two single-genre watchlist items → several candidates, still ONE load.
    media_factory(title='Second', genres='Thriller')
    media_factory(title='Third', genres='Thriller')
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    assert result['total'] == 3
    assert len(loads) == 1  # one profile load per evaluation, not per title


def test_filter_off_matches_baseline_behavior(user, media_factory, db):
    a = media_factory(title='A', genres='Action')
    b = media_factory(title='B', genres='Drama')
    without = sl.evaluate_smart_list(_make_list(user, {}))
    with_false = sl.evaluate_smart_list(
        _make_list(user, {'matches_my_taste': False}))
    assert ({i['id'] for i in without['items']}
            == {a.tmdb_id, b.tmdb_id}
            == {i['id'] for i in with_false['items']})


# ════════════════════════════════════════════════════════════════════════════
# Hygiene (spec §24: 22–24, 32–36; §13)
# ════════════════════════════════════════════════════════════════════════════

def test_no_network_during_evaluation(user, media_factory, db, monkeypatch):
    media_factory(genres='Thriller')
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})

    class _Guard(socket.socket):
        def __init__(self, *a, **kw):
            raise AssertionError('network access during smart list taste eval')

    monkeypatch.setattr(socket, 'socket', _Guard)
    result = sl.evaluate_smart_list(_make_list(
        user, {'matches_my_taste': True}))
    assert result['total'] == 1


def test_no_recommendation_feedback_query(user, media_factory, db,
                                          monkeypatch):
    media_factory()
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})
    import models.recommendation_feedback as rf
    monkeypatch.setattr(
        rf.RecommendationFeedback, 'query', property(
            lambda self: (_ for _ in ()).throw(
                AssertionError('feedback queried'))))
    sl.evaluate_smart_list(_make_list(user, {'matches_my_taste': True}))


def test_no_for_you_engine_invocation(user, media_factory, db, monkeypatch):
    media_factory()
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})
    import api.for_you as fy
    monkeypatch.setattr(
        fy, 'get_for_you', lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError('For You engine invoked')))
    sl.evaluate_smart_list(_make_list(user, {'matches_my_taste': True}))


def test_no_synchronous_recomputation(user, media_factory, db, monkeypatch):
    media_factory()
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})
    import api.taste_profile as tp
    monkeypatch.setattr(
        tp, 'compute_profile', lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError('synchronous recomputation')))
    sl.evaluate_smart_list(_make_list(user, {'matches_my_taste': True}))


def test_no_legacy_taste_counters_or_tables(user, media_factory, db):
    src = open('api/smart_lists.py').read()
    taste_src = open('api/taste_profile.py').read()
    for banned in ('user_taste_profile', 'user_similarity', '_user_top_genre'):
        assert banned not in src
        assert banned not in taste_src


def test_result_schema_unchanged(user, media_factory, db):
    media_factory(title='Schema', genres='Thriller')
    _eligible_profile(user.id, genre_weights={'Thriller': 1.0})
    base = sl.evaluate_smart_list(_make_list(user, {}))
    filtered = sl.evaluate_smart_list(
        _make_list(user, {'matches_my_taste': True}))
    assert base['items'] and filtered['items']
    assert set(base['items'][0]) == set(filtered['items'][0])
    assert set(filtered) == {'items', 'total', 'page', 'pages'}
    # No taste internals leaked into cards:
    flat = repr(filtered['items'][0]).lower()
    for banned in ('score', 'taste', 'confidence', 'weight'):
        assert banned not in flat


def test_scores_never_exposed_in_rule_summary(user, db):
    smart = _make_list(user, {'matches_my_taste': True})
    labels = sl.rule_summary(smart)
    assert 'Matches My Taste' in labels
    assert not any('score' in label.lower() for label in labels)


# ════════════════════════════════════════════════════════════════════════════
# API/UI serialization (spec §24: 37–38)
# ════════════════════════════════════════════════════════════════════════════

def test_roundtrip_via_model_json(user, db):
    smart = _make_list(user, {'matches_my_taste': True})
    fetched = SmartList.query.get(smart.id)
    assert fetched.filters['matches_my_taste'] is True
    data = fetched.to_dict()
    assert data['filters']['matches_my_taste'] is True


def test_ui_template_and_js_wire_the_checkbox():
    tpl = open('templates/smart_list_detail.html').read()
    js = open('static/js/smart-list-detail.js').read()
    assert 'sl-matches-taste' in tpl and 'sl-matches-taste' in js
    assert 'Matches my taste' in tpl
    # JS only sends true when checked; never fabricates values:
    assert 'nextFilters.matches_my_taste = true' in js
    assert "matches_my_taste === true" in js
    # No sliders/scoring UI:
    assert 'taste-score' not in tpl and 'slider' not in tpl.lower()
