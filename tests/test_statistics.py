"""Canonical personal statistics service (Feature #8, Phase 1).

Covers api/statistics.py — the data/service foundation for Wrapped-style
surfaces (no UI/charts in this phase):

- source-of-truth semantics: DiaryEntry is the watch-event history;
  distinct titles, rewatches, runtime hours, ratings, genres, media
  split, monthly trend
- time windows: current year (default), calendar year=, lifetime
- pure helpers: deterministic, no DB, no network
- isolation: statistics do NOT read TasteProfile, RecommendationFeedback,
  watchlist, wishlist, or likes; list/feedback data must not masquerade
  as watched history
- bounded queries (exactly 5 SQL statements), no N+1, no network
"""
import socket
import uuid
from datetime import date

import pytest

from api.statistics import (
    aggregate_genres,
    average_rating,
    get_statistics,
    hours_watched,
    media_type_distribution,
    month_bucket,
    parse_genres,
    rating_distribution,
    rewatch_rate,
)
from models import db, User, MediaItem, DiaryEntry

DOMAIN = "example.invalid"


def _make_user(prefix="stats"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:8]}@{DOMAIN}",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


def _media(title, runtime=None, genres=None, media_type="movie", year=2020):
    m = MediaItem(
        tmdb_id=None, media_type=media_type, title=title,
        runtime=runtime, genres=genres,
        release_date=date(year, 6, 15),
    )
    m.tmdb_id = _media._next_id
    _media._next_id += 1
    db.session.add(m)
    db.session.commit()
    return m


_media._next_id = 9_100_000


def _diary(user, media, watched_date, rating=None, is_rewatch=False):
    e = DiaryEntry(user_id=user.id, media_id=media.id,
                   media_type=media.media_type,
                   watched_date=watched_date, rating=rating,
                   is_rewatch=is_rewatch)
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def user(app):
    with app.app_context():
        u = _make_user()
        yield u
        # DiaryEntry.user has no cascade; explicit delete avoids NULLing
        # the NOT NULL FK at teardown. Media rows are module-unique IDs
        # and leave with the temp-file DB.
        DiaryEntry.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
        db.session.commit()


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers
# ════════════════════════════════════════════════════════════════════════════

def test_hours_watched_conversion():
    assert hours_watched(120) == 2.0
    assert hours_watched(185) == 3.1  # 185/60 = 3.083 → 3.1


def test_hours_watched_missing_runtime_is_zero():
    assert hours_watched(None) == 0.0
    assert hours_watched(0) == 0.0


def test_rewatch_rate_formula():
    assert rewatch_rate(2, 10) == 0.2
    assert rewatch_rate(0, 5) == 0.0


def test_rewatch_rate_zero_denominator_safe():
    assert rewatch_rate(0, 0) == 0.0
    assert rewatch_rate(3, 0) == 0.0


def test_average_rating():
    assert average_rating([4.0, 5.0]) == 4.5
    assert average_rating([3.5]) == 3.5


def test_average_rating_no_ratings_returns_none():
    assert average_rating([]) is None
    assert average_rating([None, None]) is None


def test_rating_distribution_fixed_buckets():
    dist = rating_distribution([0.5, 0.5, 5.0, 3.5])
    assert dist["0.5"] == 2 and dist["5.0"] == 1 and dist["3.5"] == 1
    assert len(dist) == 10  # always all ten buckets


def test_rating_distribution_ignores_out_of_scale():
    dist = rating_distribution([7.5, -1.0, 4.0])
    assert dist["4.0"] == 1 and sum(dist.values()) == 1


def test_month_bucket_format():
    assert month_bucket(date(2026, 3, 7)) == "2026-03"
    assert month_bucket(date(2025, 12, 31)) == "2025-12"


def test_parse_genres_splits_and_strips():
    assert parse_genres("Thriller, Drama , Thriller") == ["Thriller", "Drama"]


def test_parse_genres_missing_metadata():
    assert parse_genres(None) == []
    assert parse_genres("") == []


def test_aggregate_genres_deterministic_ranking():
    rows = aggregate_genres([["Thriller", "Drama"], ["Thriller"],
                             ["Comedy"]])
    assert [r["name"] for r in rows] == ["Thriller", "Comedy", "Drama"]
    assert rows[0]["count"] == 2


def test_aggregate_genres_bounded_to_top_ten():
    many = [[f"G{i:02d}"] for i in range(15)]
    assert len(aggregate_genres(many)) == 10


def test_media_type_distribution_fixed_shape():
    assert media_type_distribution({"movie": 3}) == {"movie": 3, "tv": 0}
    assert media_type_distribution({}) == {"movie": 0, "tv": 0}


# ════════════════════════════════════════════════════════════════════════════
# Window semantics
# ════════════════════════════════════════════════════════════════════════════

def test_window_bounds_default_is_current_year():
    from api.statistics import _window_bounds
    import datetime
    start, end, kind, _year = _window_bounds(None, False)
    assert kind == "year"
    assert start == date(datetime.datetime.now().year, 1, 1)
    assert end == date(datetime.datetime.now().year, 12, 31)


def test_window_bounds_lifetime_ignores_year():
    from api.statistics import _window_bounds
    start, end, kind, _year = _window_bounds(2001, True)
    assert (start, end, kind) == (None, None, "lifetime")


def test_window_bounds_resolves_default_year():
    from api.statistics import _window_bounds
    import datetime
    *_, year = _window_bounds(None, False)
    assert year == datetime.datetime.now().year


def test_window_bounds_rejects_bad_years():
    from api.statistics import _window_bounds
    import datetime
    with pytest.raises(ValueError):
        _window_bounds(1899, False)
    with pytest.raises(ValueError):
        _window_bounds(datetime.datetime.now().year + 1, False)
    with pytest.raises(ValueError):
        _window_bounds("2026", False)


def test_monthly_trend_lifetime_bounded(app):
    from api.statistics import MAX_LIFETIME_MONTHS, _monthly_trend
    with app.app_context():
        months = _monthly_trend(1, None, None, "lifetime", None)
        assert len(months) <= MAX_LIFETIME_MONTHS


# ════════════════════════════════════════════════════════════════════════════
# Service — zero-history user
# ════════════════════════════════════════════════════════════════════════════

def test_zero_history_user(user):
    s = get_statistics(user.id)
    assert s["available"] is False
    assert s["watch"]["events"] == 0
    assert s["watch"]["distinct_titles"] == 0
    assert s["watch"]["rewatch_events"] == 0
    assert s["watch"]["rewatch_rate"] == 0.0
    assert s["watch"]["hours_watched"] == 0.0
    assert s["ratings"]["count"] == 0
    assert s["ratings"]["average"] is None
    assert s["genres"] == []
    assert len(s["months"]) == 12  # current year, all zeros


# ════════════════════════════════════════════════════════════════════════════
# Events vs distinct titles / rewatches
# ════════════════════════════════════════════════════════════════════════════

def test_single_watch(user):
    m = _media("One Movie", runtime=120)
    _diary(user, m, date(2026, 1, 10), rating=4.0)
    s = get_statistics(user.id, year=2026)
    assert s["available"] is True
    assert s["watch"]["events"] == 1
    assert s["watch"]["distinct_titles"] == 1
    assert s["watch"]["rewatch_events"] == 0
    assert s["watch"]["rewatch_rate"] == 0.0


def test_multiple_watches_distinct_titles(user):
    a = _media("Movie A", runtime=90)
    b = _media("Movie B", runtime=110)
    _diary(user, a, date(2026, 2, 1))
    _diary(user, b, date(2026, 2, 5))
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["events"] == 2
    assert s["watch"]["distinct_titles"] == 2


def test_rewatch_does_not_increment_distinct_titles(user):
    m = _media("Rewatched", runtime=100)
    _diary(user, m, date(2026, 3, 1))
    _diary(user, m, date(2026, 3, 9), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["events"] == 2
    assert s["watch"]["distinct_titles"] == 1


def test_rewatch_increments_watch_events(user):
    m = _media("Rewatched", runtime=100)
    _diary(user, m, date(2026, 3, 1))
    _diary(user, m, date(2026, 3, 9), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["events"] == 2


def test_rewatch_rate_and_first_watch_not_rewatch(user):
    m = _media("Trilogy", runtime=150)
    _diary(user, m, date(2026, 4, 1))
    _diary(user, m, date(2026, 4, 2), is_rewatch=True)
    _diary(user, m, date(2026, 4, 3), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["rewatch_events"] == 2
    assert s["watch"]["rewatch_rate"] == round(2 / 3, 2)


# ════════════════════════════════════════════════════════════════════════════
# Runtime / hours
# ════════════════════════════════════════════════════════════════════════════

def test_runtime_hours_includes_rewatch_runtime(user):
    m = _media("Double Feature", runtime=90)
    _diary(user, m, date(2026, 5, 1))
    _diary(user, m, date(2026, 5, 2), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["hours_watched"] == 3.0  # 180 min


def test_missing_runtime_excluded_and_tracked(user):
    with_runtime = _media("Has Runtime", runtime=120)
    no_runtime = _media("No Runtime", runtime=None)
    _diary(user, with_runtime, date(2026, 5, 1))
    _diary(user, no_runtime, date(2026, 5, 2))
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["hours_watched"] == 2.0
    assert s["watch"]["events_missing_runtime"] == 1
    assert s["watch"]["events"] == 2  # event still counted


# ════════════════════════════════════════════════════════════════════════════
# Ratings
# ════════════════════════════════════════════════════════════════════════════

def test_average_and_distribution_from_diary(user):
    a = _media("Rated A", runtime=90)
    b = _media("Rated B", runtime=90)
    c = _media("Unrated C", runtime=90)
    _diary(user, a, date(2026, 6, 1), rating=4.0)
    _diary(user, b, date(2026, 6, 2), rating=5.0)
    _diary(user, c, date(2026, 6, 3))  # unrated event
    s = get_statistics(user.id, year=2026)
    assert s["ratings"]["count"] == 2
    assert s["ratings"]["average"] == 4.5
    assert s["ratings"]["distribution"]["4.0"] == 1
    assert s["ratings"]["distribution"]["5.0"] == 1


def test_no_rating_user(user):
    m = _media("Unrated Only", runtime=90)
    _diary(user, m, date(2026, 6, 1))
    s = get_statistics(user.id, year=2026)
    assert s["ratings"]["count"] == 0
    assert s["ratings"]["average"] is None
    assert sum(s["ratings"]["distribution"].values()) == 0


# ════════════════════════════════════════════════════════════════════════════
# Genres
# ════════════════════════════════════════════════════════════════════════════

def test_genre_aggregation_counts_events(user):
    thriller = _media("Thriller One", runtime=90, genres="Thriller, Drama")
    thriller2 = _media("Thriller Two", runtime=90, genres="Thriller")
    _diary(user, thriller, date(2026, 7, 1))
    _diary(user, thriller2, date(2026, 7, 2))
    s = get_statistics(user.id, year=2026)
    by_name = {g["name"]: g for g in s["genres"]}
    assert by_name["Thriller"]["count"] == 2
    assert by_name["Drama"]["count"] == 1
    assert by_name["Thriller"]["titles"] == 2
    assert by_name["Drama"]["titles"] == 1


def test_genre_rewatch_counts_event_not_new_title(user):
    m = _media("Same Thriller", runtime=90, genres="Thriller")
    _diary(user, m, date(2026, 7, 1))
    _diary(user, m, date(2026, 7, 2), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    by_name = {g["name"]: g for g in s["genres"]}
    assert by_name["Thriller"]["count"] == 2      # events
    assert by_name["Thriller"]["titles"] == 1     # distinct breadth


def test_missing_genre_metadata_degrades_gracefully(user):
    m = _media("No Genres", runtime=90, genres=None)
    _diary(user, m, date(2026, 7, 1))
    s = get_statistics(user.id, year=2026)
    assert s["genres"] == []
    assert s["available"] is True


# ════════════════════════════════════════════════════════════════════════════
# Media type split
# ════════════════════════════════════════════════════════════════════════════

def test_movie_tv_split(user):
    movie = _media("A Movie", runtime=90, media_type="movie")
    show = _media("A Show", runtime=45, media_type="tv")
    _diary(user, movie, date(2026, 8, 1))
    _diary(user, show, date(2026, 8, 2))
    _diary(user, show, date(2026, 8, 3), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["media_types"] == {"movie": 1, "tv": 2}


def test_movie_tv_split_uses_stored_media_type(user):
    m = _media("Stored Type", runtime=90, media_type="tv")
    _diary(user, m, date(2026, 8, 1))
    s = get_statistics(user.id, year=2026)
    assert s["media_types"]["tv"] == 1


# ════════════════════════════════════════════════════════════════════════════
# Monthly trend
# ════════════════════════════════════════════════════════════════════════════

def test_monthly_aggregation_year_window(user):
    m = _media("Monthly", runtime=90)
    _diary(user, m, date(2026, 1, 5))
    _diary(user, m, date(2026, 1, 20), is_rewatch=True)
    _diary(user, m, date(2026, 3, 9))
    s = get_statistics(user.id, year=2026)
    months = {row["month"]: row["count"] for row in s["months"]}
    assert months["2026-01"] == 2
    assert months["2026-03"] == 1
    assert months["2026-02"] == 0  # zero month present
    assert len(s["months"]) == 12


def test_lifetime_window_includes_all_history(user):
    m = _media("Old One", runtime=90, year=1999)
    _diary(user, m, date(1999, 6, 1))
    m2 = _media("New One", runtime=90)
    _diary(user, m2, date(2026, 2, 1))
    s = get_statistics(user.id, lifetime=True)
    assert s["window"]["kind"] == "lifetime"
    assert s["watch"]["events"] == 2
    assert s["watch"]["distinct_titles"] == 2


def test_year_window_excludes_other_years(user):
    m = _media("Y2025", runtime=90)
    _diary(user, m, date(2025, 6, 1))
    _diary(user, m, date(2026, 6, 1))
    s = get_statistics(user.id, year=2025)
    assert s["watch"]["events"] == 1
    assert s["window"]["year"] == 2025


def test_boundary_dates_inclusive(user):
    m = _media("Boundary", runtime=90)
    _diary(user, m, date(2026, 1, 1))   # first day
    _diary(user, m, date(2026, 12, 31))  # last day
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["events"] == 2


def test_deterministic_repeated_output(user):
    m = _media("Determinism", runtime=90, genres="Drama")
    _diary(user, m, date(2026, 9, 1), rating=4.5)
    first = get_statistics(user.id, year=2026)
    second = get_statistics(user.id, year=2026)
    assert first == second


# ════════════════════════════════════════════════════════════════════════════
# Isolation — statistics are an independent consumer of watch history
# ════════════════════════════════════════════════════════════════════════════

def test_watchlist_data_is_not_watch_history(user):
    from models import user_watchlist
    m = _media("Only Wishlisted", runtime=90)
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=m.id, media_type="movie", priority=1))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["available"] is False
    assert s["watch"]["events"] == 0


def test_feedback_clicks_do_not_count_as_watched(user):
    from models.recommendation_feedback import RecommendationFeedback
    m = _media("Clicked Only", runtime=90)
    db.session.add(RecommendationFeedback(
        user_id=user.id, media_id=m.tmdb_id, media_type="movie",
        surface="home_for_you", source="for_you", event="click"))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["events"] == 0
    assert s["available"] is False


def test_taste_profile_not_touched(user):
    from api import taste_profile
    m = _media("Profile Bait", runtime=90)
    _diary(user, m, date(2026, 9, 1))
    calls = []
    real = taste_profile.compute_profile
    taste_profile.compute_profile = lambda *a, **k: calls.append(a)
    try:
        get_statistics(user.id, year=2026)
    finally:
        taste_profile.compute_profile = real
    assert calls == []


def test_user_isolation(user):
    other = _make_user("other")
    try:
        m = _media("Other's Movie", runtime=90)
        _diary(other, m, date(2026, 9, 1))
        s = get_statistics(user.id, year=2026)
        assert s["watch"]["events"] == 0
    finally:
        DiaryEntry.query.filter_by(user_id=other.id).delete()
        db.session.delete(other)
        db.session.commit()


# ════════════════════════════════════════════════════════════════════════════
# Performance / purity guards
# ════════════════════════════════════════════════════════════════════════════

def test_bounded_query_count(user, monkeypatch):
    from sqlalchemy import event as sa_event
    m = _media("Q Count", runtime=90, genres="Drama")
    _diary(user, m, date(2026, 9, 1), rating=4.0)
    _diary(user, m, date(2026, 9, 2), is_rewatch=True)
    uid = user.id  # read BEFORE the listener: commit expiry would add a
    statements = []  # refresh SELECT that is not the service's doing

    def _record(conn, cursor, statement, *args, **kwargs):
        statements.append(statement)

    sa_event.listen(db.engine, "before_cursor_execute", _record)
    try:
        get_statistics(uid, year=2026)
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", _record)
    assert len(statements) <= 5, statements


def test_no_n_plus_one_across_many_titles(user):
    for i in range(12):
        m = _media(f"N+1 Probe {i}", runtime=90, genres="Drama")
        _diary(user, m, date(2026, 9, 1))
    s = get_statistics(user.id, year=2026)
    assert s["watch"]["distinct_titles"] == 12  # single batched pass


def test_no_network_socket_guard(user):
    m = _media("Socket Guard", runtime=90)
    _diary(user, m, date(2026, 9, 1))
    real_socket = socket.socket

    class _Blocked(real_socket):
        def __init__(self, *args, **kwargs):
            raise AssertionError("network call attempted")

    socket.socket = _Blocked
    try:
        get_statistics(user.id, year=2026)
    finally:
        socket.socket = real_socket


def test_no_tmdb_dependency(user, monkeypatch):
    m = _media("No TMDb", runtime=90)
    _diary(user, m, date(2026, 9, 1))
    import api.tmdb.movies as tmdb_movies
    monkeypatch.setattr(
        tmdb_movies, "fetch_movie_details",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("TMDb call")))
    get_statistics(user.id, year=2026)  # must not raise


def test_presentation_shape_compact(user):
    m = _media("Shape", runtime=90, genres="Drama")
    _diary(user, m, date(2026, 9, 1), rating=4.0)
    s = get_statistics(user.id, year=2026)
    assert set(s.keys()) == {"available", "window", "watch",
                             "media_types", "ratings", "genres", "months"}
    # No internal IDs or ORM rows anywhere in the presentation shape.
    assert "id" not in s["watch"] and "user_id" not in s["watch"]
    assert all(isinstance(v, (int, float, str, bool, type(None)))
               for row in s["months"] for v in row.values())


def test_stats_module_has_no_forbidden_imports():
    # AST-level guard: api/statistics.py must not IMPORT the subsystems
    # it must stay independent from (docstring mentions are fine).
    import ast
    tree = ast.parse(open("api/statistics.py").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for banned in ("recommendation_feedback", "taste_profile",
                   "for_you", "tmdb", "requests", "urllib"):
        assert not any(banned in mod for mod in imported), imported
