"""Year-in-review foundation (Feature #8, Phase 4) — focused suite.

Covers api/year_in_review.build_year_in_review() as a pure
transformation over one canonical api.statistics.get_statistics() call:

- year-only scope with canonical validation (invalid years raise
  ValueError before any DB access)
- empty year → available=False + state="empty" (no fabricated story)
- highlight rules: top genre (canonical order, omitted when absent),
  busiest month (earliest-month tie rule), ratings, rewatches,
  deterministic media split, runtime completeness ("At least X hours"
  when coverage is incomplete)
- one statistics-service call, zero additional DB queries, zero
  network, zero writes, no IDs / private identity exposed
- deterministic output: byte-identical JSON for identical statistics
"""
import json
import socket
import uuid
from datetime import date
from unittest.mock import patch

import pytest

from api import year_in_review
from api.year_in_review import build_year_in_review


# ── Helpers (statistics-suite conventions) ──────────────────────────────────

def _make_user(username):
    from models import User, db as _db
    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def user(app):
    with app.app_context():
        yield _make_user('yir' + uuid.uuid4().hex[:6])


def _media(title, runtime=None, genres=None, media_type='movie'):
    from models import MediaItem, db as _db
    m = MediaItem(tmdb_id=9_000_000 + uuid.uuid4().hex.__len__() * 0 +
                  uuid.uuid4().int % 900_000_000,
                  media_type=media_type, title=title, runtime=runtime,
                  genres=genres)
    _db.session.add(m)
    _db.session.commit()
    return m


def _diary(user, media, watched_date, rating=None, is_rewatch=False):
    from models import DiaryEntry, db as _db
    e = DiaryEntry(user_id=user.id, media_id=media.id,
                   media_type=media.media_type,
                   watched_date=watched_date, rating=rating,
                   is_rewatch=is_rewatch)
    _db.session.add(e)
    _db.session.commit()
    return e


# ── ID-leak probes (structural, not naive substring matching) ──────────────

def _walk_id_keys(node):
    """Collect every object key that names an identifier."""
    keys = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower().endswith('_id') or key.lower() == 'id':
                keys.add(key)
            keys |= _walk_id_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _walk_id_keys(item)
    return keys


def _walk_values(node):
    """Every leaf/child value in a nested JSON structure."""
    if isinstance(node, dict):
        out = []
        for child in node.values():
            out.append(child)
            out.extend(_walk_values(child))
        return out
    if isinstance(node, list):
        out = []
        for child in node:
            out.append(child)
            out.extend(_walk_values(child))
        return out
    return [node]


# ════════════════════════════════════════════════════════════════════════════
# Empty state (§5)
# ════════════════════════════════════════════════════════════════════════════

def test_empty_year_returns_unavailable_neutral_state(user, app):
    with app.app_context():
        result = build_year_in_review(user.id, 2026)
    assert result == {'year': 2026, 'available': False, 'state': 'empty'}


def test_empty_year_fabricates_no_story_text(user, app):
    with app.app_context():
        result = build_year_in_review(user.id, 2026)
    blob = json.dumps(result)
    assert 'Most watched' not in blob
    assert 'busiest' not in blob.lower()
    assert 'Average rating' not in blob


# ════════════════════════════════════════════════════════════════════════════
# Normal years — summary passthrough + structure
# ════════════════════════════════════════════════════════════════════════════

def test_one_watch_event_full_model(user, app):
    with app.app_context():
        m = _media('Solo', runtime=120, genres='Drama')
        _diary(user, m, date(2026, 5, 10), rating=4.0)
        result = build_year_in_review(user.id, 2026)
    assert result['available'] is True
    assert result['state'] == 'ready'
    assert result['year'] == 2026
    summary = result['summary']
    assert summary['total_watch_events'] == 1
    assert summary['distinct_titles'] == 1
    assert summary['movies_watched'] == 1
    assert summary['tv_watch_events'] == 0
    assert summary['total_hours_watched'] == 2.0
    assert summary['average_rating'] == 4.0
    assert summary['rating_count'] == 1
    assert summary['rewatch_count'] == 0


def test_multiple_watches_title_and_event_counts_preserved(user, app):
    with app.app_context():
        a = _media('A', runtime=100)
        b = _media('B', runtime=50)
        _diary(user, a, date(2026, 1, 5))
        _diary(user, a, date(2026, 2, 5))   # second event, same title
        _diary(user, b, date(2026, 3, 5))
        result = build_year_in_review(user.id, 2026)
    assert result['summary']['total_watch_events'] == 3
    assert result['summary']['distinct_titles'] == 2


def test_no_database_ids_exposed(user, app):
    with app.app_context():
        m = _media('IdCheck', runtime=90)
        _diary(user, m, date(2026, 6, 1))
        media_id = m.id
        result = build_year_in_review(user.id, 2026)
        # No raw ID *value* leaks as a JSON value (structural match,
        # not a naive substring).
        values = _walk_values(result)
        assert media_id not in values
        assert user.id not in values
    assert not _walk_id_keys(result)   # no id-named keys anywhere


def test_no_private_identity_exposed(user, app):
    with app.app_context():
        m = _media('Privacy', runtime=90)
        _diary(user, m, date(2026, 6, 1))
        result = build_year_in_review(user.id, 2026)
    blob = json.dumps(result)
    assert user.username not in blob
    assert user.email not in blob


# ════════════════════════════════════════════════════════════════════════════
# Highlights
# ════════════════════════════════════════════════════════════════════════════

def test_top_genre_highlight_uses_canonical_order(user, app):
    with app.app_context():
        m1 = _media('G1', runtime=90, genres='Drama')
        m2 = _media('G2', runtime=90, genres='Drama, Thriller')
        _diary(user, m1, date(2026, 1, 1))
        _diary(user, m2, date(2026, 1, 2))
        result = build_year_in_review(user.id, 2026)
    genre = result['highlights']['top_genre']
    assert genre['name'] == 'Drama'          # 2 events vs Thriller's 1
    assert genre['count'] == 2
    assert genre['text'].startswith('Most watched genre: Drama')


def test_no_genre_omits_highlight(user, app):
    with app.app_context():
        m = _media('NoGenre', runtime=90, genres=None)
        _diary(user, m, date(2026, 2, 1))
        result = build_year_in_review(user.id, 2026)
    assert 'top_genre' not in result['highlights']
    assert result['genres'] == []


def test_busiest_month_selected(user, app):
    with app.app_context():
        m = _media('M', runtime=90)
        for day in (1, 2, 3):
            _diary(user, m, date(2026, 8, day))   # August: 3 events
        _diary(user, m, date(2026, 3, 1))          # March: 1
        result = build_year_in_review(user.id, 2026)
    busiest = result['highlights']['busiest_month']
    assert busiest['month'] == '2026-08'
    assert busiest['count'] == 3
    assert busiest['text'] == ('Your busiest month was August 2026 '
                               'with 3 watch events.')


def test_monthly_tie_deterministic_earliest_month_wins(user, app):
    with app.app_context():
        m = _media('Tie', runtime=90)
        _diary(user, m, date(2026, 4, 10))   # April: 1
        _diary(user, m, date(2026, 9, 10))   # September: 1 — tie
        result = build_year_in_review(user.id, 2026)
    busiest = result['highlights']['busiest_month']
    assert busiest['month'] == '2026-04'     # earliest month wins
    assert busiest['count'] == 1


def test_busiest_month_never_zero_or_absent(user, app):
    # All months zero → handled by the empty-year branch; here a year
    # with events in exactly one month must never highlight another.
    with app.app_context():
        m = _media('One', runtime=90)
        _diary(user, m, date(2026, 12, 25))
        result = build_year_in_review(user.id, 2026)
    assert result['highlights']['busiest_month']['month'] == '2026-12'


def test_rating_summary_present(user, app):
    with app.app_context():
        m = _media('R', runtime=90)
        _diary(user, m, date(2026, 1, 1), rating=4.0)
        _diary(user, m, date(2026, 2, 1), rating=5.0)
        result = build_year_in_review(user.id, 2026)
    ratings = result['highlights']['ratings']
    assert ratings['average_rating'] == 4.5
    assert ratings['count'] == 2
    assert ratings['text'] == 'Average rating: 4.5 across 2 ratings.'
    assert result['ratings']['distribution']['4.0'] == 1
    assert result['ratings']['distribution']['5.0'] == 1


def test_no_ratings_omits_highlight_and_neutral_section(user, app):
    with app.app_context():
        m = _media('NR', runtime=90)
        _diary(user, m, date(2026, 1, 1))
        result = build_year_in_review(user.id, 2026)
    assert 'ratings' not in result['highlights']
    assert result['ratings'] == {'average_rating': None, 'count': 0,
                                 'distribution':
                                 result['ratings']['distribution']}
    assert sum(result['ratings']['distribution'].values()) == 0


def test_rating_distribution_preserved_canonical_buckets(user, app):
    with app.app_context():
        m = _media('RD', runtime=90)
        _diary(user, m, date(2026, 1, 1), rating=2.5)
        result = build_year_in_review(user.id, 2026)
    distribution = result['ratings']['distribution']
    assert list(distribution.keys()) == [
        '0.5', '1.0', '1.5', '2.0', '2.5', '3.0', '3.5', '4.0', '4.5',
        '5.0']
    assert distribution['2.5'] == 1


def test_rewatch_summary_explicit_semantics(user, app):
    with app.app_context():
        m = _media('RW', runtime=90)
        _diary(user, m, date(2026, 1, 1))
        _diary(user, m, date(2026, 2, 1), is_rewatch=True)
        result = build_year_in_review(user.id, 2026)
    rewatches = result['highlights']['rewatches']
    assert rewatches['count'] == 1
    assert rewatches['rate'] == 0.5
    assert result['rewatches'] == {'count': 1, 'rate': 0.5}


def test_no_rewatches_still_reports_zero(user, app):
    with app.app_context():
        m = _media('NoRW', runtime=90)
        _diary(user, m, date(2026, 1, 1))
        result = build_year_in_review(user.id, 2026)
    assert result['rewatches'] == {'count': 0, 'rate': 0.0}
    assert result['highlights']['rewatches']['count'] == 0


def test_rewatch_never_inferred_from_duplicate_titles(user, app):
    # Two unflagged events for the same title: NOT a rewatch (canonical
    # semantics require the explicit is_rewatch flag).
    with app.app_context():
        m = _media('Dup', runtime=90)
        _diary(user, m, date(2026, 1, 1))
        _diary(user, m, date(2026, 2, 1))
        result = build_year_in_review(user.id, 2026)
    assert result['summary']['rewatch_count'] == 0
    assert result['summary']['distinct_titles'] == 1
    assert result['summary']['total_watch_events'] == 2


# ── Media split (§12) ────────────────────────────────────────────────────────

def test_movie_heavy_year(user, app):
    with app.app_context():
        m = _media('Mov', runtime=90)
        for month in range(1, 5):
            _diary(user, m, date(2026, month, 1))    # 4 movies
        tv = _media('Tv', runtime=45, media_type='tv')
        _diary(user, tv, date(2026, 5, 1))           # 1 TV → 80% movie
        result = build_year_in_review(user.id, 2026)
    split = result['highlights']['media_split']
    assert split['description'] == 'mostly movies'
    assert split['movie'] == 4 and split['tv'] == 1


def test_tv_heavy_year(user, app):
    with app.app_context():
        tv = _media('Tv', runtime=45, media_type='tv')
        for month in range(1, 5):
            _diary(user, tv, date(2026, month, 1))   # 4 TV
        m = _media('Mov', runtime=90)
        _diary(user, m, date(2026, 5, 1))            # 1 movie → 80% TV
        result = build_year_in_review(user.id, 2026)
    assert result['highlights']['media_split']['description'] == 'mostly TV'


def test_balanced_media_year(user, app):
    with app.app_context():
        m = _media('Mov', runtime=90)
        tv = _media('Tv', runtime=45, media_type='tv')
        _diary(user, m, date(2026, 1, 1))
        _diary(user, tv, date(2026, 2, 1))           # 50/50 → balanced
        result = build_year_in_review(user.id, 2026)
    assert result['highlights']['media_split']['description'] == 'balanced'


def test_media_split_helper_thresholds():
    # Deterministic boundary behavior of the pure helper.
    d = year_in_review.describe_media_split
    assert d({'movie': 3, 'tv': 1})['description'] == 'mostly movies'
    assert d({'movie': 1, 'tv': 3})['description'] == 'mostly TV'
    assert d({'movie': 2, 'tv': 2})['description'] == 'balanced'
    assert d({'movie': 0, 'tv': 0}) is None
    assert d(None) is None


# ════════════════════════════════════════════════════════════════════════════
# Runtime completeness (§15/§16)
# ════════════════════════════════════════════════════════════════════════════

def test_runtime_complete_year(user, app):
    with app.app_context():
        m1 = _media('C1', runtime=100)
        m2 = _media('C2', runtime=50)
        _diary(user, m1, date(2026, 1, 1))
        _diary(user, m2, date(2026, 2, 1))
        result = build_year_in_review(user.id, 2026)
    runtime = result['runtime']
    assert runtime == {'hours': 2.5, 'covered_events': 2,
                       'missing_events': 0, 'complete': True,
                       'text': '2.5 hours watched.'}


def test_runtime_incomplete_claims_at_least(user, app):
    with app.app_context():
        m1 = _media('I1', runtime=100)
        m2 = _media('I2', runtime=None)          # no persisted runtime
        _diary(user, m1, date(2026, 1, 1))
        _diary(user, m2, date(2026, 2, 1))
        result = build_year_in_review(user.id, 2026)
    runtime = result['runtime']
    assert runtime['complete'] is False
    assert runtime['missing_events'] == 1
    assert runtime['hours'] == 1.7               # 100 min only
    assert runtime['text'].startswith('At least 1.7 hours')


def test_runtime_missing_entirely(user, app):
    with app.app_context():
        m = _media('NoRT', runtime=None)
        _diary(user, m, date(2026, 1, 1))
        result = build_year_in_review(user.id, 2026)
    runtime = result['runtime']
    assert runtime['hours'] == 0.0
    assert runtime['covered_events'] == 0
    assert runtime['missing_events'] == 1
    assert runtime['complete'] is False


# ════════════════════════════════════════════════════════════════════════════
# Year scope + boundaries (§3)
# ════════════════════════════════════════════════════════════════════════════

def test_year_boundary_inclusive(user, app):
    with app.app_context():
        m = _media('Edge', runtime=90)
        _diary(user, m, date(2025, 1, 1))    # Jan 1 included
        _diary(user, m, date(2025, 12, 31))  # Dec 31 included
        r2025 = build_year_in_review(user.id, 2025)
        r2026 = build_year_in_review(user.id, 2026)
    assert r2025['summary']['total_watch_events'] == 2
    assert r2026['available'] is False


def test_adjacent_year_excluded(user, app):
    with app.app_context():
        m = _media('Adj', runtime=90)
        _diary(user, m, date(2024, 12, 31))  # previous year
        _diary(user, m, date(2026, 1, 1))    # next year
        result = build_year_in_review(user.id, 2025)
    assert result['available'] is False


@pytest.mark.parametrize('bad_year', [1899, 2100, '2026', 2026.0, True,
                                      False, None])
def test_invalid_year_raises_value_error(user, app, bad_year):
    with app.app_context():
        with pytest.raises(ValueError):
            build_year_in_review(user.id, bad_year)


def test_monthly_series_is_full_calendar_year(user, app):
    with app.app_context():
        m = _media('Series', runtime=90)
        _diary(user, m, date(2026, 1, 1))
        _diary(user, m, date(2026, 12, 1))
        result = build_year_in_review(user.id, 2026)
    monthly = result['monthly']
    assert len(monthly) == 12                      # zero months retained
    assert monthly[0]['month'] == '2026-01'
    assert monthly[-1]['month'] == '2026-12'
    assert monthly[0]['count'] == 1 and monthly[-1]['count'] == 1
    assert monthly[5]['count'] == 0                # June stays zero


# ════════════════════════════════════════════════════════════════════════════
# Guards: one service call, zero extra queries, no network, no writes
# ════════════════════════════════════════════════════════════════════════════

def test_exactly_one_statistics_service_call(user, app):
    with app.app_context():
        m = _media('Calls', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        with patch('api.year_in_review.get_statistics',
                   wraps=year_in_review.get_statistics) as spy:
            result = build_year_in_review(user.id, 2026)
    assert spy.call_count == 1
    assert result['summary']['total_watch_events'] == 1


def test_zero_additional_db_queries_beyond_service(user, app):
    from sqlalchemy import event
    from models.base import db as _db

    statements = []

    def _count(conn, cursor, statement, *args, **kwargs):
        statements.append(statement)

    with app.app_context():
        m = _media('Q', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        statements.clear()
        event.listen(_db.engine, 'before_cursor_execute', _count)
        try:
            with patch('api.statistics.get_statistics',
                       wraps=year_in_review.get_statistics):
                # Wrapper executes the REAL service; count its queries.
                build_year_in_review(user.id, 2026)
        finally:
            event.remove(_db.engine, 'before_cursor_execute', _count)
    assert len(statements) == 5   # the canonical service's 5 statements


def test_invalid_year_never_touches_database(user, app):
    with app.app_context():
        with patch('api.year_in_review.get_statistics') as spy:
            with pytest.raises(ValueError):
                build_year_in_review(user.id, 1899)
    spy.assert_not_called()


def test_no_network_socket_guard(user, app):
    def _blocked(*args, **kwargs):
        raise AssertionError('network access attempted')

    with app.app_context():
        m = _media('Net', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        with patch.object(socket.socket, '__init__', _blocked), \
             patch.object(socket.socket, 'connect', _blocked), \
             patch.object(socket.socket, 'connect_ex', _blocked):
            result = build_year_in_review(user.id, 2026)
    assert result['available'] is True


def test_no_database_writes(user, app):
    from models import DiaryEntry
    with app.app_context():
        m = _media('RO', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        before = DiaryEntry.query.count()
        build_year_in_review(user.id, 2026)
        assert DiaryEntry.query.count() == before


def test_no_recommendation_dependencies(user, app):
    with app.app_context():
        m = _media('Indep', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        with patch('api.for_you.get_for_you',
                   side_effect=AssertionError('For You used')), \
             patch('api.taste_profile.compute_profile',
                   side_effect=AssertionError('TasteProfile used')), \
             patch('models.recommendation_feedback.RecommendationFeedback',
                   side_effect=AssertionError('feedback used')):
            result = build_year_in_review(user.id, 2026)
    assert result['available'] is True


def test_module_imports_are_recommendation_free():
    import api.statistics as stats_mod
    import api.year_in_review as yir_mod
    banned = ('for_you', 'taste_profile', 'recommendation_feedback',
              'smart_lists', 'cinebot', 'agents')
    for module in (yir_mod, stats_mod):
        source = open(module.__file__, encoding='utf-8').read()
        for name in banned:
            assert f'import {name}' not in source
            assert f'from {name}' not in source


# ════════════════════════════════════════════════════════════════════════════
# Determinism (§20) + wording (§21)
# ════════════════════════════════════════════════════════════════════════════

def test_deterministic_output_byte_identical(user, app):
    with app.app_context():
        m1 = _media('D1', runtime=100, genres='Drama')
        m2 = _media('D2', runtime=50, genres='Drama, Thriller')
        tv = _media('D3', runtime=45, media_type='tv')
        _diary(user, m1, date(2026, 2, 1), rating=4.0)
        _diary(user, m1, date(2026, 3, 1), rating=5.0, is_rewatch=True)
        _diary(user, m2, date(2026, 4, 1))
        _diary(user, tv, date(2026, 5, 1))
        first = json.dumps(build_year_in_review(user.id, 2026),
                           sort_keys=True)
        second = json.dumps(build_year_in_review(user.id, 2026),
                            sort_keys=True)
    assert first == second


def test_no_current_time_or_randomness_in_output(user, app):
    with app.app_context():
        m = _media('Time', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        result = build_year_in_review(user.id, 2026)
    blob = json.dumps(result)
    assert 'generated_at' not in blob
    assert 'timestamp' not in blob


def test_descriptive_wording_no_overstatement(user, app):
    with app.app_context():
        m = _media('W', runtime=90, genres='Science Fiction')
        _diary(user, m, date(2026, 3, 3))
        result = build_year_in_review(user.id, 2026)
    blob = json.dumps(result)
    for phrase in ('favorite', 'huge', 'love', 'You are', 'obsessed'):
        assert phrase not in blob.lower()


def test_no_unsupported_favorite_title_claims(user, app):
    # The statistics service exposes no title-level ranking, so the
    # story model must never name a movie/TV title at all.
    with app.app_context():
        m = _media('Secret Movie Title', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        result = build_year_in_review(user.id, 2026)
    assert 'Secret Movie Title' not in json.dumps(result)


# ════════════════════════════════════════════════════════════════════════════
# §32 — hand-calculated cross-check (service → story model, no drift)
# ════════════════════════════════════════════════════════════════════════════

def test_hand_calculated_cross_check(user, app):
    """Fixture: 4 events, 3 distinct titles, 2 movies + 1 TV event,
    4.9 hours, avg 4.5, 1 rewatch, busiest month March (2 events).

    Hand math:
      events      = 4 (3 first-watch + 1 explicit rewatch)
      titles      = 3 (A, B, TV)
      runtime     = 100 + 100 + 50 + 45 = 295 min → 4.916… → 4.9 h
      ratings     = (4.0 + 5.0) / 2 = 4.5
      rewatch     = 1/4 = 0.25
      months      = Feb/Mar/Mar/Apr/May events → March has 2 (busiest)
      genres      = Drama 3 events (2 titles), Thriller 1
      media split = 3 movies / 1 TV = 75% → "mostly movies" (≥ 0.75)
    """
    with app.app_context():
        a = _media('Cross A', runtime=100, genres='Drama')
        b = _media('Cross B', runtime=50, genres='Drama, Thriller')
        tv = _media('Cross TV', runtime=45, media_type='tv')
        _diary(user, a, date(2026, 3, 10), rating=4.0)
        _diary(user, a, date(2026, 3, 20), rating=5.0, is_rewatch=True)
        _diary(user, b, date(2026, 4, 10))
        _diary(user, tv, date(2026, 5, 10))
        result = build_year_in_review(user.id, 2026)

    assert result['available'] is True
    summary = result['summary']
    assert summary['total_watch_events'] == 4
    assert summary['distinct_titles'] == 3
    assert summary['movies_watched'] == 3
    assert summary['tv_watch_events'] == 1
    assert summary['total_hours_watched'] == 4.9
    assert summary['average_rating'] == 4.5
    assert summary['rating_count'] == 2
    assert summary['rewatch_count'] == 1
    assert summary['rewatch_rate'] == 0.25

    assert result['highlights']['busiest_month']['month'] == '2026-03'
    assert result['highlights']['busiest_month']['count'] == 2
    assert result['highlights']['top_genre']['name'] == 'Drama'
    assert result['highlights']['media_split']['description'] == \
        'mostly movies'
    assert result['runtime'] == {
        'hours': 4.9, 'covered_events': 4, 'missing_events': 0,
        'complete': True, 'text': '4.9 hours watched.'}
    assert result['ratings']['distribution']['4.0'] == 1
    assert result['ratings']['distribution']['5.0'] == 1
    months = {row['month']: row['count'] for row in result['monthly']}
    assert months['2026-02'] == 0 and months['2026-03'] == 2
    assert months['2026-04'] == 1 and months['2026-05'] == 1
