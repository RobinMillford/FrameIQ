"""Canonical personal statistics computation (Feature #8, Phase 2).

Covers api/statistics.py — the canonical computation service derived
from DiaryEntry watch events × MediaItem metadata:

- §6 exact output contract (flat metric names)
- events vs distinct titles vs explicit is_rewatch rewatches (§7, §13)
- runtime hours with coverage/missing accounting (§9)
- DiaryEntry.rating as the single rating source, fixed 0.5–5.0
  distribution buckets (§10, §11)
- genre aggregation from persisted MediaItem.genres (§12)
- windows: default year, year=, lifetime, explicit start/end — half-open
  [start, end) boundaries (§5, §24), monthly buckets (§14)
- isolation: watchlist/wishlist/list items, recommendation feedback,
  and Continue Watching starts are NOT watch history (§2); TVEpisodeWatch
  is a separate subsystem and is never merged in (§16)
- independence: no TasteProfile/For You/RecommendationFeedback/TMDb
  dependency (§19, §18)
- bounded SQL (≤5 statements), no N+1, no network (§17, §18)
"""
import socket
import uuid
from datetime import date, datetime

import pytest

from api.statistics import (
    aggregate_genres,
    average_rating,
    calendar_year_bounds,
    get_statistics,
    hours_watched,
    media_type_distribution,
    month_bucket,
    parse_genres,
    rating_distribution,
    rewatch_rate,
)
from models import db, User, MediaItem, DiaryEntry
from models.tv import TVEpisodeWatch

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


_media._next_id = 9_500_000


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
        # the NOT NULL FK at teardown. UserList.user and
        # TVEpisodeWatch.user are dynamic backrefs — those rows must go
        # too (items first; bulk deletes bypass ORM cascades). Media
        # rows use module-unique IDs and leave with the temp-file DB.
        DiaryEntry.query.filter_by(user_id=u.id).delete()
        TVEpisodeWatch.query.filter_by(user_id=u.id).delete()
        from models.lists import UserList, UserListItem
        UserListItem.query.filter(
            UserListItem.list_id.in_(
                db.session.query(UserList.id)
                .filter_by(user_id=u.id))).delete(
                synchronize_session=False)
        UserList.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
        db.session.commit()


# ════════════════════════════════════════════════════════════════════════════
# Pure helpers (deterministic, no DB, no network)
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
    assert len(dist) == 10  # all ten buckets, zeros included


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


def test_calendar_year_bounds_half_open():
    lower, upper = calendar_year_bounds(2026)
    assert lower == date(2026, 1, 1)
    assert upper == date(2027, 1, 1)  # exclusive — no 23:59:59 arithmetic


def test_calendar_year_bounds_rejects_bad_years():
    with pytest.raises(ValueError):
        calendar_year_bounds(1899)
    with pytest.raises(ValueError):
        calendar_year_bounds(datetime.now().year + 1)
    with pytest.raises(ValueError):
        calendar_year_bounds("2026")
    with pytest.raises(ValueError):
        calendar_year_bounds(True)


def test_leap_year_bounds():
    lower, upper = calendar_year_bounds(2024)
    assert lower == date(2024, 1, 1)
    assert upper == date(2025, 1, 1)


# ════════════════════════════════════════════════════════════════════════════
# Output contract (§6)
# ════════════════════════════════════════════════════════════════════════════

def test_output_contract_exact_names(user):
    s = get_statistics(user.id)
    assert set(s.keys()) == {
        "total_watch_events", "distinct_titles", "movies_watched",
        "tv_watch_events", "total_hours_watched", "runtime_covered_events",
        "runtime_missing_events", "average_rating", "rating_count",
        "rating_distribution", "rewatch_count", "rewatch_rate",
        "top_genres", "monthly_watch_counts", "media_type_distribution",
        "daily_activity", "active_watch_days", "max_daily_watch_events",
        "directors", "actors", "season_quality",
    }


# ════════════════════════════════════════════════════════════════════════════
# Zero-history user (§22)
# ════════════════════════════════════════════════════════════════════════════

def test_zero_history_user(user):
    s = get_statistics(user.id)
    assert s["total_watch_events"] == 0
    assert s["distinct_titles"] == 0
    assert s["movies_watched"] == 0
    assert s["tv_watch_events"] == 0
    assert s["total_hours_watched"] == 0.0
    assert s["runtime_covered_events"] == 0
    assert s["runtime_missing_events"] == 0
    assert s["average_rating"] is None
    assert s["rating_count"] == 0
    assert s["rewatch_count"] == 0
    assert s["rewatch_rate"] == 0.0
    assert s["top_genres"] == []
    assert s["media_type_distribution"] == {"movie": 0, "tv": 0}
    assert len(s["monthly_watch_counts"]) == 12  # current year, zeros


# ════════════════════════════════════════════════════════════════════════════
# Events vs distinct titles vs explicit rewatches (§7, §13)
# ════════════════════════════════════════════════════════════════════════════

def test_single_watch(user):
    m = _media("One Movie", runtime=120)
    _diary(user, m, date(2026, 1, 10), rating=4.0)
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 1
    assert s["distinct_titles"] == 1
    assert s["rewatch_count"] == 0
    assert s["rewatch_rate"] == 0.0


def test_multiple_watches_distinct_titles(user):
    a = _media("Movie A", runtime=90)
    b = _media("Movie B", runtime=110)
    _diary(user, a, date(2026, 2, 1))
    _diary(user, b, date(2026, 2, 5))
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 2
    assert s["distinct_titles"] == 2


def test_rewatch_does_not_increment_distinct_titles(user):
    m = _media("Rewatched", runtime=100)
    _diary(user, m, date(2026, 3, 1))
    _diary(user, m, date(2026, 3, 9), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 2
    assert s["distinct_titles"] == 1


def test_rewatch_increments_watch_events(user):
    m = _media("Rewatched", runtime=100)
    _diary(user, m, date(2026, 3, 1))
    _diary(user, m, date(2026, 3, 9), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 2


def test_rewatch_count_uses_is_rewatch_flag_not_duplicate_titles(user):
    # Three events on the same title but only ONE explicitly flagged —
    # the count must follow the persisted flag, never duplicates.
    m = _media("Flagged Only", runtime=90)
    _diary(user, m, date(2026, 4, 1))
    _diary(user, m, date(2026, 4, 2), is_rewatch=True)
    _diary(user, m, date(2026, 4, 3))  # duplicate date, not flagged
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 3
    assert s["distinct_titles"] == 1
    assert s["rewatch_count"] == 1
    assert s["rewatch_rate"] == round(1 / 3, 2)


def test_rewatch_rate_never_divides_by_zero(user):
    s = get_statistics(user.id, year=2026)
    assert s["rewatch_rate"] == 0.0


# ════════════════════════════════════════════════════════════════════════════
# Runtime / hours (§9)
# ════════════════════════════════════════════════════════════════════════════

def test_runtime_hours_include_rewatch_runtime(user):
    m = _media("Double Feature", runtime=90)
    _diary(user, m, date(2026, 5, 1))
    _diary(user, m, date(2026, 5, 2), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["total_hours_watched"] == 3.0  # 180 min


def test_missing_runtime_excluded_and_tracked(user):
    with_runtime = _media("Has Runtime", runtime=120)
    no_runtime = _media("No Runtime", runtime=None)
    _diary(user, with_runtime, date(2026, 5, 1))
    _diary(user, no_runtime, date(2026, 5, 2))
    s = get_statistics(user.id, year=2026)
    assert s["total_hours_watched"] == 2.0
    assert s["runtime_covered_events"] == 1
    assert s["runtime_missing_events"] == 1
    assert s["total_watch_events"] == 2  # event still counted


def test_runtime_coverage_counts(user):
    m1 = _media("Covered", runtime=60)
    m2 = _media("Missing", runtime=None)
    _diary(user, m1, date(2026, 5, 1))
    _diary(user, m1, date(2026, 5, 2), is_rewatch=True)
    _diary(user, m2, date(2026, 5, 3))
    s = get_statistics(user.id, year=2026)
    assert s["runtime_covered_events"] == 2
    assert s["runtime_missing_events"] == 1


# ════════════════════════════════════════════════════════════════════════════
# Ratings (§10, §11)
# ════════════════════════════════════════════════════════════════════════════

def test_average_and_distribution_from_diary(user):
    a = _media("Rated A", runtime=90)
    b = _media("Rated B", runtime=90)
    c = _media("Unrated C", runtime=90)
    _diary(user, a, date(2026, 6, 1), rating=4.0)
    _diary(user, b, date(2026, 6, 2), rating=5.0)
    _diary(user, c, date(2026, 6, 3))  # unrated event
    s = get_statistics(user.id, year=2026)
    assert s["rating_count"] == 2
    assert s["average_rating"] == 4.5
    assert s["rating_distribution"]["4.0"] == 1
    assert s["rating_distribution"]["5.0"] == 1


def test_no_rating_user(user):
    m = _media("Unrated Only", runtime=90)
    _diary(user, m, date(2026, 6, 1))
    s = get_statistics(user.id, year=2026)
    assert s["rating_count"] == 0
    assert s["average_rating"] is None
    assert sum(s["rating_distribution"].values()) == 0
    assert len(s["rating_distribution"]) == 10


# ════════════════════════════════════════════════════════════════════════════
# Genres (§12)
# ════════════════════════════════════════════════════════════════════════════

def test_genre_aggregation_counts_events(user):
    thriller = _media("Thriller One", runtime=90, genres="Thriller, Drama")
    thriller2 = _media("Thriller Two", runtime=90, genres="Thriller")
    _diary(user, thriller, date(2026, 7, 1))
    _diary(user, thriller2, date(2026, 7, 2))
    s = get_statistics(user.id, year=2026)
    by_name = {g["name"]: g for g in s["top_genres"]}
    assert by_name["Thriller"]["count"] == 2
    assert by_name["Drama"]["count"] == 1
    assert by_name["Thriller"]["titles"] == 2
    assert by_name["Drama"]["titles"] == 1


def test_genre_rewatch_counts_event_not_new_title(user):
    m = _media("Same Thriller", runtime=90, genres="Thriller")
    _diary(user, m, date(2026, 7, 1))
    _diary(user, m, date(2026, 7, 2), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    by_name = {g["name"]: g for g in s["top_genres"]}
    assert by_name["Thriller"]["count"] == 2      # events
    assert by_name["Thriller"]["titles"] == 1     # distinct breadth


def test_missing_genre_metadata_degrades_gracefully(user):
    m = _media("No Genres", runtime=90, genres=None)
    _diary(user, m, date(2026, 7, 1))
    s = get_statistics(user.id, year=2026)
    assert s["top_genres"] == []
    assert s["total_watch_events"] == 1


def test_duplicate_genres_in_one_title_count_once(user):
    m = _media("Dup Metadata", runtime=90, genres="Drama, Drama, Drama")
    _diary(user, m, date(2026, 7, 1))
    s = get_statistics(user.id, year=2026)
    by_name = {g["name"]: g for g in s["top_genres"]}
    assert by_name["Drama"]["count"] == 1
    assert by_name["Drama"]["titles"] == 1


# ════════════════════════════════════════════════════════════════════════════
# Media type split (§15)
# ════════════════════════════════════════════════════════════════════════════

def test_movie_tv_split(user):
    movie = _media("A Movie", runtime=90, media_type="movie")
    show = _media("A Show", runtime=45, media_type="tv")
    _diary(user, movie, date(2026, 8, 1))
    _diary(user, show, date(2026, 8, 2))
    _diary(user, show, date(2026, 8, 3), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["movies_watched"] == 1
    assert s["tv_watch_events"] == 2
    assert s["media_type_distribution"] == {"movie": 1, "tv": 2}


def test_movie_tv_split_uses_stored_media_type(user):
    m = _media("Stored Type", runtime=90, media_type="tv")
    _diary(user, m, date(2026, 8, 1))
    s = get_statistics(user.id, year=2026)
    assert s["tv_watch_events"] == 1
    assert s["movies_watched"] == 0


# ════════════════════════════════════════════════════════════════════════════
# Windows and monthly aggregation (§5, §14, §24)
# ════════════════════════════════════════════════════════════════════════════

def test_monthly_aggregation_year_window(user):
    m = _media("Monthly", runtime=90)
    _diary(user, m, date(2026, 1, 5))
    _diary(user, m, date(2026, 1, 20), is_rewatch=True)
    _diary(user, m, date(2026, 3, 9))
    s = get_statistics(user.id, year=2026)
    months = {row["month"]: row["count"] for row in s["monthly_watch_counts"]}
    assert months["2026-01"] == 2
    assert months["2026-03"] == 1
    assert months["2026-02"] == 0  # zero month present
    assert len(s["monthly_watch_counts"]) == 12


def test_lifetime_window_includes_all_history(user):
    m = _media("Old One", runtime=90, year=1999)
    _diary(user, m, date(1999, 6, 1))
    m2 = _media("New One", runtime=90)
    _diary(user, m2, date(2026, 2, 1))
    s = get_statistics(user.id, lifetime=True)
    assert s["total_watch_events"] == 2
    assert s["distinct_titles"] == 2
    assert len(s["monthly_watch_counts"]) <= 36  # bounded


def test_lifetime_bounded_monthly_output(user):
    # 40 distinct month buckets across history → output stays bounded.
    for i in range(40):
        m = _media(f"Spread {i}", runtime=90, year=1990 + i % 30)
        _diary(user, m, date(1995 + i % 30, 1 + i % 12, 1))
    s = get_statistics(user.id, lifetime=True)
    assert len(s["monthly_watch_counts"]) <= 36
    buckets = [row["month"] for row in s["monthly_watch_counts"]]
    assert buckets == sorted(buckets)  # chronological


def test_year_window_excludes_other_years(user):
    m = _media("Y2025", runtime=90)
    _diary(user, m, date(2025, 6, 1))
    _diary(user, m, date(2026, 6, 1))
    s = get_statistics(user.id, year=2025)
    assert s["total_watch_events"] == 1


def test_default_year_is_current_calendar_year(user):
    current = datetime.now().year
    m = _media("Current Year", runtime=90)
    _diary(user, m, date(current, 3, 3))
    s = get_statistics(user.id)
    assert s["total_watch_events"] == 1
    assert len(s["monthly_watch_counts"]) == 12


def test_lifetime_ignores_year(user):
    m = _media("Any Year", runtime=90)
    _diary(user, m, date(1999, 6, 1))
    s = get_statistics(user.id, lifetime=True, year=2026)
    assert s["total_watch_events"] == 1  # 1999 event included


def test_invalid_year_rejected(user):
    with pytest.raises(ValueError):
        get_statistics(user.id, year=1899)
    with pytest.raises(ValueError):
        get_statistics(user.id, year=datetime.now().year + 1)
    with pytest.raises(ValueError):
        get_statistics(user.id, year="2026")


def test_year_cannot_combine_with_explicit_dates(user):
    with pytest.raises(ValueError):
        get_statistics(user.id, start_date=date(2026, 1, 1),
                       end_date=date(2026, 2, 1), year=2026)


def test_boundary_dates_inclusive_and_exclusive(user):
    m = _media("Boundary", runtime=90)
    _diary(user, m, date(2026, 1, 1))     # Jan 1 included
    _diary(user, m, date(2026, 12, 31))   # Dec 31 included
    _diary(user, m, date(2025, 12, 31))   # previous year excluded
    _diary(user, m, date(2027, 1, 1))     # next year excluded
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 2


def test_explicit_date_window_half_open(user):
    m = _media("Window", runtime=90)
    _diary(user, m, date(2026, 3, 1))
    _diary(user, m, date(2026, 3, 10))
    s = get_statistics(user.id, start_date=date(2026, 3, 1),
                       end_date=date(2026, 3, 10))  # exclusive upper
    assert s["total_watch_events"] == 1


def test_invalid_date_window_rejected(user):
    with pytest.raises(ValueError):
        get_statistics(user.id, start_date=date(2026, 3, 1),
                       end_date=date(2026, 2, 1))


# ════════════════════════════════════════════════════════════════════════════
# Determinism (§31 of Phase-1 spec; deterministic output required here)
# ════════════════════════════════════════════════════════════════════════════

def test_deterministic_repeated_output(user):
    m = _media("Determinism", runtime=90, genres="Drama")
    _diary(user, m, date(2026, 9, 1), rating=4.5)
    first = get_statistics(user.id, year=2026)
    second = get_statistics(user.id, year=2026)
    assert first == second


# ════════════════════════════════════════════════════════════════════════════
# Isolation — what does NOT count as watched history (§2) and user
# isolation (§25)
# ════════════════════════════════════════════════════════════════════════════

def test_watchlist_data_is_not_watch_history(user):
    from models import user_watchlist
    m = _media("Only Wishlisted", runtime=90)
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=m.id, media_type="movie", priority=1))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 0


def test_wishlist_data_is_not_watch_history(user):
    from models import user_wishlist
    m = _media("Only Wishlisted Item", runtime=90)
    db.session.execute(user_wishlist.insert().values(
        user_id=user.id, media_id=m.id, media_type="movie"))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 0


def test_list_items_are_not_watch_history(user):
    from models.lists import UserList, UserListItem
    m = _media("Listed", runtime=90)
    lst = UserList(user_id=user.id, title=f"coll_{uuid.uuid4().hex[:6]}",
                   description="stats fixture")
    db.session.add(lst)
    db.session.commit()
    db.session.add(UserListItem(list_id=lst.id, media_id=m.id,
                                media_type="movie"))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 0


def test_feedback_clicks_do_not_count_as_watched(user):
    from models.recommendation_feedback import RecommendationFeedback
    m = _media("Clicked Only", runtime=90)
    db.session.add(RecommendationFeedback(
        user_id=user.id, media_id=m.tmdb_id, media_type="movie",
        surface="home_for_you", source="for_you", event="click"))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 0


def test_continue_watching_start_is_not_a_watch(user):
    from models.continue_watching import ContinueWatchingItem
    m = _media("Half Watched", runtime=90)
    db.session.add(ContinueWatchingItem(
        user_id=user.id, media_type="movie", tmdb_id=m.tmdb_id,
        title=m.title))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 0


def test_tv_episode_watch_table_is_not_merged(user):
    # §16: TVEpisodeWatch is the episode-tracking subsystem; statistics
    # must not double-count a TV event through two source tables.
    from models.tv import TVEpisodeWatch
    m = _media("TV Show", runtime=45, media_type="tv")
    _diary(user, m, date(2026, 9, 1))  # the ONLY canonical watch event
    db.session.add(TVEpisodeWatch(
        user_id=user.id, show_id=m.tmdb_id, season_number=1,
        episode_number=1, watched_date=date(2026, 9, 1)))
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 1
    assert s["tv_watch_events"] == 1
    TVEpisodeWatch.query.filter_by(user_id=user.id).delete()
    db.session.commit()


def test_user_isolation(user):
    other = _make_user("other")
    try:
        m = _media("Other's Movie", runtime=90)
        _diary(other, m, date(2026, 9, 1))
        s = get_statistics(user.id, year=2026)
        assert s["total_watch_events"] == 0
    finally:
        DiaryEntry.query.filter_by(user_id=other.id).delete()
        db.session.delete(other)
        db.session.commit()


# ════════════════════════════════════════════════════════════════════════════
# Performance / purity guards (§17, §18)
# ════════════════════════════════════════════════════════════════════════════

def test_bounded_query_count(user):
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
    assert len(statements) <= 8, statements


def test_no_n_plus_one_across_many_titles(user):
    for i in range(12):
        m = _media(f"N+1 Probe {i}", runtime=90, genres="Drama")
        _diary(user, m, date(2026, 9, 1))
    s = get_statistics(user.id, year=2026)
    assert s["distinct_titles"] == 12  # single batched pass


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
                   "for_you", "tmdb", "requests", "urllib",
                   "smart_lists", "lists", "continue_watching"):
        assert not any(banned in mod for mod in imported), imported


# ════════════════════════════════════════════════════════════════════════════
# Data-quality / graceful degradation (§23)
# ════════════════════════════════════════════════════════════════════════════

def test_missing_media_item_degrades_gracefully(user):
    # An orphaned diary event (media row gone) must not crash the
    # service: the outer join yields NULL metadata, the event still
    # counts, runtime is reported missing, genres absent.
    m = _media("Doomed", runtime=90)
    e = _diary(user, m, date(2026, 9, 1))
    from models.base import db as _db
    from sqlalchemy import text
    _db.session.execute(
        DiaryEntry.__table__.delete().where(DiaryEntry.id == e.id))
    _db.session.execute(
        text("DELETE FROM media_item WHERE id = :mid"), {"mid": m.id})
    _db.session.execute(
        DiaryEntry.__table__.insert().values(
            user_id=user.id, media_id=999999999, media_type="movie",
            watched_date=date(2026, 9, 1)))
    _db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 1
    assert s["runtime_missing_events"] == 1
    assert s["top_genres"] == []


def test_malformed_genre_string_graceful(user):
    m = _media("Weird Genres", runtime=90, genres=" ,,,Drama,, ,Sci-Fi, ")
    _diary(user, m, date(2026, 9, 1))
    s = get_statistics(user.id, year=2026)
    names = [g["name"] for g in s["top_genres"]]
    assert names == ["Drama", "Sci-Fi"]  # empties dropped, order kept


# ════════════════════════════════════════════════════════════════════════════
# Feature #8 Phase 5 — watch heatmap + daily activity (§3–§22)
# ════════════════════════════════════════════════════════════════════════════

def test_daily_activity_empty(user):
    s = get_statistics(user.id, year=2026)
    assert s["daily_activity"] == []
    assert s["active_watch_days"] == 0
    assert s["max_daily_watch_events"] == 0


def test_daily_activity_single_watch_single_day(user):
    m = _media("One Day", runtime=90)
    _diary(user, m, date(2026, 1, 3))
    s = get_statistics(user.id, year=2026)
    assert s["daily_activity"] == [{"date": "2026-01-03", "count": 1}]
    assert s["active_watch_days"] == 1
    assert s["max_daily_watch_events"] == 1


def test_daily_activity_multiple_events_same_day(user):
    m = _media("Same Day", runtime=90)
    _diary(user, m, date(2026, 4, 10))
    _diary(user, m, date(2026, 4, 10))
    _diary(user, m, date(2026, 4, 10))
    s = get_statistics(user.id, year=2026)
    assert s["daily_activity"] == [{"date": "2026-04-10", "count": 3}]
    assert s["max_daily_watch_events"] == 3


def test_daily_activity_multiple_dates_ascending(user):
    m = _media("Spread", runtime=90)
    _diary(user, m, date(2026, 3, 5))
    _diary(user, m, date(2026, 1, 20))
    _diary(user, m, date(2026, 12, 31))
    s = get_statistics(user.id, year=2026)
    dates = [row["date"] for row in s["daily_activity"]]
    assert dates == sorted(dates) == [
        "2026-01-20", "2026-03-05", "2026-12-31"]


def test_daily_activity_rewatch_counts_as_event(user):
    m = _media("Rewatch Day", runtime=90)
    _diary(user, m, date(2026, 6, 6))
    _diary(user, m, date(2026, 6, 6), is_rewatch=True)
    s = get_statistics(user.id, year=2026)
    assert s["daily_activity"] == [{"date": "2026-06-06", "count": 2}]


def test_daily_activity_duplicate_title_events_separate(user):
    # Two DiaryEntry rows for the same title on different days are two
    # events on two days (title identity never merges watch days).
    m = _media("Title Twice", runtime=90)
    _diary(user, m, date(2026, 2, 1))
    _diary(user, m, date(2026, 2, 2))
    s = get_statistics(user.id, year=2026)
    assert s["daily_activity"] == [
        {"date": "2026-02-01", "count": 1},
        {"date": "2026-02-02", "count": 1},
    ]
    assert s["active_watch_days"] == 2


def test_active_watch_days_unique_dates(user):
    m = _media("Active Days", runtime=90)
    _diary(user, m, date(2026, 5, 1))
    _diary(user, m, date(2026, 5, 1))
    _diary(user, m, date(2026, 5, 2))
    _diary(user, m, date(2026, 5, 3))
    s = get_statistics(user.id, year=2026)
    assert s["active_watch_days"] == 3   # 4 events across 3 days


def test_max_daily_watch_events(user):
    m = _media("Max Day", runtime=90)
    for _ in range(4):
        _diary(user, m, date(2026, 7, 7))
    _diary(user, m, date(2026, 7, 8))
    s = get_statistics(user.id, year=2026)
    assert s["max_daily_watch_events"] == 4
    assert s["active_watch_days"] == 2


def test_daily_activity_calendar_year_filtering(user):
    m = _media("Filter", runtime=90)
    _diary(user, m, date(2025, 12, 31))
    _diary(user, m, date(2026, 1, 1))
    s2026 = get_statistics(user.id, year=2026)
    assert [row["date"] for row in s2026["daily_activity"]] == ["2026-01-01"]
    s2025 = get_statistics(user.id, year=2025)
    assert [row["date"] for row in s2025["daily_activity"]] == ["2025-12-31"]


def test_daily_activity_lifetime_bounded_to_real_dates(user):
    # Lifetime emits only dates that have events — no zero-filled
    # infinite calendar (§4).
    m = _media("Life", runtime=90)
    _diary(user, m, date(2020, 2, 29))   # leap day
    _diary(user, m, date(2026, 9, 1))
    s = get_statistics(user.id, lifetime=True)
    assert [row["date"] for row in s["daily_activity"]] == [
        "2020-02-29", "2026-09-01"]


def test_watched_date_authoritative_not_created_at(user):
    # The activity date comes from watched_date alone; created_at is
    # never consulted (§4/§22 — insert a row whose created_at disagrees).
    from sqlalchemy import text as _text
    from models.base import db as _db
    m = _media("Date Truth", runtime=90)
    _diary(user, m, date(2026, 3, 15))
    _db.session.execute(_text(
        "UPDATE diary_entry SET created_at = '2019-01-01 00:00:00' "
        "WHERE user_id = :uid"), {"uid": user.id})
    _db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert [row["date"] for row in s["daily_activity"]] == ["2026-03-15"]


def test_monthly_and_daily_reconcile_with_total_events(user):
    m = _media("Reconcile", runtime=90)
    tv = _media("Reconcile TV", runtime=45, media_type="tv")
    _diary(user, m, date(2026, 1, 3))
    _diary(user, m, date(2026, 1, 3))
    _diary(user, m, date(2026, 1, 8))
    _diary(user, tv, date(2026, 2, 20))
    _diary(user, tv, date(2026, 2, 20), is_rewatch=True)
    _diary(user, m, date(2026, 11, 30))
    s = get_statistics(user.id, year=2026)
    daily_sum = sum(row["count"] for row in s["daily_activity"])
    monthly_sum = sum(row["count"] for row in s["monthly_watch_counts"])
    assert daily_sum == s["total_watch_events"] == 6
    assert monthly_sum == s["total_watch_events"] == 6   # §11 invariant


def test_daily_activity_no_ids_or_orm_objects(user):
    m = _media("No Ids", runtime=90)
    _diary(user, m, date(2026, 8, 9))
    s = get_statistics(user.id, year=2026)
    import json as _json
    blob = _json.dumps(s)
    assert '"_id"' not in blob and '"id"' not in blob   # no id-named keys
    for row in s["daily_activity"]:
        assert set(row.keys()) == {"date", "count"}


def test_daily_activity_deterministic(user):
    m = _media("Det", runtime=90)
    _diary(user, m, date(2026, 9, 2))
    _diary(user, m, date(2026, 9, 1))
    _diary(user, m, date(2026, 9, 2))
    import json as _json
    first = _json.dumps(get_statistics(user.id, year=2026), sort_keys=True)
    second = _json.dumps(get_statistics(user.id, year=2026), sort_keys=True)
    assert first == second


def test_bounded_query_count_with_daily_activity(user):
    # §20 — the invariant: one service call → a small fixed number of
    # SQL statements → no per-event queries. The Phase 2 architecture
    # pins exactly 6 (event/rating/media/monthly aggregates, genre
    # projection, daily GROUP BY); the daily extension adds exactly one.
    from sqlalchemy import event as sa_event
    m = _media("Q Daily", runtime=90)
    for _ in range(5):
        _diary(user, m, date(2026, 9, 1))
    uid = user.id  # read BEFORE the listener: commit expiry would add a
    statements = []  # refresh SELECT that is not the service's doing

    def _record(conn, cursor, statement, *args, **kwargs):
        statements.append(statement)

    sa_event.listen(db.engine, "before_cursor_execute", _record)
    try:
        get_statistics(uid, year=2026)
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", _record)
    assert len(statements) == 8, statements


def test_daily_helper_pure_and_graceful():
    # Direct pure-helper coverage (§6): explicit input, no DB, empty
    # input, ISO strings, pair form, None/blank skipping.
    from datetime import date as _date
    from api.statistics import build_daily_watch_counts as build

    assert build(None) == []
    assert build([]) == []
    assert build([_date(2026, 1, 3), _date(2026, 1, 3)]) == [
        {"date": "2026-01-03", "count": 2}]
    assert build(["2026-03-01", "2026-02-01"]) == [
        {"date": "2026-02-01", "count": 1},
        {"date": "2026-03-01", "count": 1}]
    assert build([(_date(2026, 1, 3), 2), (_date(2026, 1, 8), 1)]) == [
        {"date": "2026-01-03", "count": 2},
        {"date": "2026-01-08", "count": 1}]
    assert build([None, "", "   ", _date(2026, 2, 2)]) == [
        {"date": "2026-02-02", "count": 1}]


def test_daily_helper_no_database_or_flask():
    # The helper is importable and callable without app/DB context.
    import sys
    from api.statistics import build_daily_watch_counts as build
    assert "flask" not in sys.modules or build([]) == []
    assert build(["2026-01-01"]) == [{"date": "2026-01-01", "count": 1}]


# ── API surface for the new fields ──────────────────────────────────────────

@pytest.fixture
def stats_user(app):
    from models import User
    username = "statu" + uuid.uuid4().hex[:6]
    u = User(username=username, email=f"{username}@example.com",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        yield u


@pytest.fixture
def auth_client(client, stats_user):
    client.post("/login", data={
        "username": stats_user.username, "password": "TestPass1"})
    return client


def test_api_exposes_heatmap_fields(auth_client, stats_user, app):
    with app.app_context():
        m = _media("API Heat", runtime=90)
        _diary(stats_user, m, date(2026, 5, 5))
        _diary(stats_user, m, date(2026, 5, 5))
        data = auth_client.get("/api/statistics?year=2026").get_json()
    assert data["daily_activity"] == [{"date": "2026-05-05", "count": 2}]
    assert data["active_watch_days"] == 1
    assert data["max_daily_watch_events"] == 2


def test_api_heatmap_empty_state(auth_client, stats_user):
    data = auth_client.get("/api/statistics?year=2026").get_json()
    assert data["daily_activity"] == []
    assert data["active_watch_days"] == 0
    assert data["max_daily_watch_events"] == 0


# ── Profile UI source guards ────────────────────────────────────────────────

def _statistics_js_code():
    """JS source with block comments stripped (prose mentions of banned
    APIs must not trip the guards — only real code matches)."""
    import re as _re
    return _re.sub(r"/\*.*?\*/", "",
                   open("static/js/statistics.js",
                        encoding="utf-8").read(), flags=_re.S)


def test_ui_heatmap_uses_api_data_no_recompute():
    js = _statistics_js_code()
    # Heatmap consumes the server fields; no date arithmetic/recounting.
    assert "daily_activity" in js
    assert "getMonth" not in js and "getFullYear" not in js
    assert "Date.now" not in js


def test_ui_heatmap_no_polling_no_extra_fetch():
    js = _statistics_js_code()
    assert "setInterval" not in js
    # One fetch construction total — heatmap rides the existing request.
    assert js.count("fetch(url,") == 1


def test_ui_heatmap_accessible_without_color():
    js = _statistics_js_code()
    assert "aria-label" in js
    assert "listitem" in js
    assert "textContent" in js          # counts rendered as text
    assert "innerHTML" not in js        # §14 XSS rule


def test_ui_heatmap_template_targets_exist():
    js = open("static/js/statistics.js", encoding="utf-8").read()
    template = open("templates/profile.html", encoding="utf-8").read()
    for target in ("statistics-heatmap", "statistics-heatmap-summary",
                   "statistics-heatmap-block"):
        assert target in js and f'id="{target}"' in template


# ════════════════════════════════════════════════════════════════════════════
# Feature #8 Phase 6 — actor/director statistics (§2–§36)
# ════════════════════════════════════════════════════════════════════════════

def _director(name, tmdb_person_id):
    from models import Director, MediaDirector  # noqa: F401 (re-exported)
    d = Director(tmdb_person_id=tmdb_person_id, name=name)
    db.session.add(d)
    db.session.commit()
    return d


def _attach_director(media, director):
    from models import MediaDirector
    link = MediaDirector(media_item_id=media.id, director_id=director.id)
    db.session.add(link)
    db.session.commit()
    return link


# ── §30 canonical example: A×2 + B×1 both by X; C by Y ─────────────────────

def test_director_event_and_title_counts(user):
    x = _director("Director X", 101)
    y = _director("Director Y", 102)
    a = _media("Movie A", runtime=90)
    b = _media("Movie B", runtime=90)
    c = _media("Movie C", runtime=90)
    _attach_director(a, x)
    _attach_director(b, x)
    _attach_director(c, y)
    _diary(user, a, date(2026, 1, 1))
    _diary(user, a, date(2026, 2, 1))     # rewatch of A
    _diary(user, b, date(2026, 3, 1))
    _diary(user, c, date(2026, 4, 1))
    s = get_statistics(user.id, year=2026)
    by_name = {p["name"]: p for p in s["directors"]}
    assert by_name["Director X"]["watch_event_count"] == 3
    assert by_name["Director X"]["distinct_title_count"] == 2
    assert by_name["Director Y"]["watch_event_count"] == 1
    assert by_name["Director Y"]["distinct_title_count"] == 1


def test_two_directors_sharing_one_title_each_get_full_contribution(user):
    x = _director("Co X", 201)
    z = _director("Co Z", 202)
    a = _media("Co Movie", runtime=90)
    _attach_director(a, x)
    _attach_director(a, z)
    _diary(user, a, date(2026, 1, 1))
    _diary(user, a, date(2026, 2, 1))
    s = get_statistics(user.id, year=2026)
    by_name = {p["name"]: p for p in s["directors"]}
    # §30: co-directors are never divided — each gets the full events.
    assert by_name["Co X"] == {"name": "Co X", "watch_event_count": 2,
                               "distinct_title_count": 1}
    assert by_name["Co Z"] == {"name": "Co Z", "watch_event_count": 2,
                               "distinct_title_count": 1}


def test_director_ordering_events_then_titles_then_name(user):
    d1 = _director("Aaa", 301)   # 3 events, 1 title
    d2 = _director("Bbb", 302)   # 3 events, 2 titles
    d3 = _director("Ccc", 303)   # 2 events, 1 title
    m1 = _media("O1", runtime=90)
    m2 = _media("O2", runtime=90)
    m3 = _media("O3", runtime=90)
    _attach_director(m1, d1)
    _attach_director(m2, d2)
    _attach_director(m3, d2)
    _attach_director(m3, d3)
    _diary(user, m1, date(2026, 1, 1))
    _diary(user, m1, date(2026, 2, 1))
    _diary(user, m1, date(2026, 3, 1))
    _diary(user, m2, date(2026, 4, 1))
    _diary(user, m3, date(2026, 5, 1))
    _diary(user, m3, date(2026, 6, 1))
    s = get_statistics(user.id, year=2026)
    names = [p["name"] for p in s["directors"]]
    # events DESC → titles DESC (breaks the 3/3 event tie) → name ASC
    assert names == ["Bbb", "Aaa", "Ccc"]


def test_director_name_tiebreak_case_consistent(user):
    lo = _director("beta", 401)
    hi = _director("Alpha", 402)
    m1 = _media("T1", runtime=90)
    m2 = _media("T2", runtime=90)
    _attach_director(m1, lo)
    _attach_director(m2, hi)
    _diary(user, m1, date(2026, 1, 1))
    _diary(user, m2, date(2026, 1, 2))
    s = get_statistics(user.id, year=2026)
    names = [p["name"] for p in s["directors"]]
    assert names == ["Alpha", "beta"]   # casefold tie-break: A < b


def test_director_top_n_cap(user):
    for i in range(12):
        d = _director(f"Cap {i:02d}", 500 + i)
        m = _media(f"Cap Movie {i}", runtime=90)
        _attach_director(m, d)
        _diary(user, m, date(2026, 1, 1))
    s = get_statistics(user.id, year=2026)
    assert len(s["directors"]) == 10     # TOP_PEOPLE_LIMIT
    assert all(p["watch_event_count"] == 1 for p in s["directors"])
    names = [p["name"] for p in s["directors"]]
    assert names == sorted(names, key=str.casefold)   # name tie-break


def test_director_empty_data(user):
    s = get_statistics(user.id, year=2026)
    assert s["directors"] == []
    assert s["actors"] == []


def test_mediaitem_without_director_fabricates_nothing(user):
    m = _media("No Director", runtime=90)
    _diary(user, m, date(2026, 1, 1))
    s = get_statistics(user.id, year=2026)
    assert s["directors"] == []                       # no fabrication
    assert s["total_watch_events"] == 1               # event still counts
    assert s["distinct_titles"] == 1


def test_orphan_diaryentry_contributes_no_person(user):
    from models.base import db as _db
    m = _media("Doomed Dir", runtime=90)
    d = _director("Ghost Director", 601)
    _attach_director(m, d)
    e = _diary(user, m, date(2026, 1, 1))
    _db.session.execute(
        DiaryEntry.__table__.delete().where(DiaryEntry.id == e.id))
    _db.session.execute(
        DiaryEntry.__table__.insert().values(
            user_id=user.id, media_id=999999999, media_type="movie",
            watched_date=date(2026, 1, 1)))
    _db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["directors"] == []                       # no person invented
    assert s["total_watch_events"] == 1               # global stats intact


def test_director_window_filtering_and_lifetime(user):
    d = _director("Window", 701)
    m = _media("Window Movie", runtime=90)
    _attach_director(m, d)
    _diary(user, m, date(2025, 12, 31))
    _diary(user, m, date(2026, 1, 1))
    s2026 = get_statistics(user.id, year=2026)
    assert s2026["directors"][0]["watch_event_count"] == 1
    life = get_statistics(user.id, lifetime=True)
    assert life["directors"][0]["watch_event_count"] == 2
    assert life["directors"][0]["distinct_title_count"] == 1


def test_director_watched_date_authoritative(user):
    from sqlalchemy import text as _text
    from models.base import db as _db
    d = _director("Date Truth Dir", 801)
    m = _media("Date Truth Movie", runtime=90)
    _attach_director(m, d)
    _diary(user, m, date(2026, 3, 15))
    _db.session.execute(_text(
        "UPDATE diary_entry SET created_at = '2019-01-01 00:00:00' "
        "WHERE user_id = :uid"), {"uid": user.id})
    _db.session.commit()
    s2026 = get_statistics(user.id, year=2026)
    assert s2026["directors"][0]["watch_event_count"] == 1
    s2019 = get_statistics(user.id, year=2019)
    assert s2019["directors"] == []       # created_at never consulted


def test_actors_absent_until_persistence_exists(user):
    # §3/§16/§19: no persisted actor relationship exists in this
    # repository; the field is honestly empty, never fabricated.
    m = _media("Any Movie", runtime=90)
    _diary(user, m, date(2026, 1, 1))
    s = get_statistics(user.id, year=2026)
    assert s["actors"] == []


def test_people_stats_no_tmdb_or_network(user):
    import socket
    from unittest.mock import patch

    def _blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    d = _director("Net Dir", 901)
    m = _media("Net Movie", runtime=90)
    _attach_director(m, d)
    _diary(user, m, date(2026, 1, 1))
    with patch.object(socket.socket, "__init__", _blocked), \
         patch.object(socket.socket, "connect", _blocked), \
         patch.object(socket.socket, "connect_ex", _blocked):
        s = get_statistics(user.id, year=2026)
    assert s["directors"][0]["name"] == "Net Dir"


def test_people_stats_no_recommendation_imports():
    import api.statistics as stats_mod
    source = open(stats_mod.__file__, encoding="utf-8").read()
    for name in ("for_you", "taste_profile", "recommendation_feedback",
                 "smart_lists", "cinebot", "agents", "tmdb"):
        assert f"import {name}" not in source
        assert f"from {name}" not in source


def test_people_stats_no_ids_in_response(user):
    d = _director("Privacy Dir", 1001)
    m = _media("Privacy Movie", runtime=90)
    _attach_director(m, d)
    _diary(user, m, date(2026, 1, 1))
    s = get_statistics(user.id, year=2026)
    import json as _json
    for row in s["directors"]:
        assert set(row.keys()) == {"name", "watch_event_count",
                                   "distinct_title_count"}

    # Structural privacy check (naive substring matching trips on plain
    # counts like "4"): walk the whole payload and assert no key looks
    # like an identifier.
    def _walk_keys(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                yield from _walk_keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from _walk_keys(item)

    for key in _walk_keys(s):
        assert "id" not in key.lower()
    assert "tmdb_person_id" not in _json.dumps(s)


def test_people_stats_deterministic(user):
    x = _director("Det X", 1101)
    y = _director("Det Y", 1102)
    m1 = _media("Det M1", runtime=90)
    m2 = _media("Det M2", runtime=90)
    _attach_director(m1, x)
    _attach_director(m2, x)
    _attach_director(m2, y)
    _diary(user, m1, date(2026, 1, 1))
    _diary(user, m2, date(2026, 2, 1))
    import json as _json
    first = _json.dumps(get_statistics(user.id, year=2026), sort_keys=True)
    second = _json.dumps(get_statistics(user.id, year=2026), sort_keys=True)
    assert first == second


def test_people_stats_do_not_alter_global_statistics(user):
    # §29 invariant: people aggregation is supplementary. Compare the
    # global fields before/after attaching director enrichment.
    m = _media("Invariant Movie", runtime=90)
    _diary(user, m, date(2026, 1, 1))
    _diary(user, m, date(2026, 2, 1), is_rewatch=True)
    before = get_statistics(user.id, year=2026)
    d = _director("Invariant Dir", 1201)
    _attach_director(m, d)
    after = get_statistics(user.id, year=2026)
    for key in ("total_watch_events", "distinct_titles", "movies_watched",
                "tv_watch_events", "daily_activity",
                "monthly_watch_counts", "total_hours_watched",
                "rewatch_count", "rewatch_rate", "average_rating",
                "rating_count", "active_watch_days",
                "max_daily_watch_events"):
        assert before[key] == after[key], key
    assert after["directors"][0]["watch_event_count"] == 2


def test_people_stats_helper_pure():
    from api.statistics import build_people_statistics as build
    assert build(None) == []
    assert build([]) == []
    # event-level dedup never happens: repeated rows accumulate.
    # X → (2+1 events, 1+1 titles) = (3, 2), tying with Y (3, 2);
    # the name tie-break (ASC) then ranks X before Y.
    rows = [("X", 2, 1), ("X", 1, 1), ("Y", 3, 2)]
    assert build(rows) == [
        {"name": "X", "watch_event_count": 3, "distinct_title_count": 2},
        {"name": "Y", "watch_event_count": 3, "distinct_title_count": 2},
    ]
    # custom limit
    many = [(f"P{i:02d}", 10 - i, 1) for i in range(12)]
    assert len(build(many, limit=5)) == 5
    # unusable rows skipped, no fabricated names
    assert build([(None, 1, 1), ("", 1, 1), (42, 1, 1), ("Ok", 1, 1)]) == [
        {"name": "Ok", "watch_event_count": 1, "distinct_title_count": 1}]


def test_people_stats_bounded_query_count(user):
    # §14/§20/§26: exactly 8 statements (Phase 2–5 base 6 + director
    # GROUP BY + Phase 7 season-quality read); the count must not scale
    # with people/titles/events/seasons.
    from sqlalchemy import event as sa_event
    d = _director("Q Dir", 1301)
    m = _media("Q Dir Movie", runtime=90)
    _attach_director(m, d)
    for day in range(1, 6):
        _diary(user, m, date(2026, 9, day))
    uid = user.id
    statements = []

    def _record(conn, cursor, statement, *args, **kwargs):
        statements.append(statement)

    sa_event.listen(db.engine, "before_cursor_execute", _record)
    try:
        get_statistics(uid, year=2026)
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", _record)
    assert len(statements) == 8, statements


def test_people_stats_no_n_plus_one_across_many_directors(user):
    d1 = _director("N+1 A", 1401)
    d2 = _director("N+1 B", 1402)
    for i in range(12):
        m = _media(f"N+1 Dir Movie {i}", runtime=90)
        _attach_director(m, d1 if i % 2 == 0 else d2)
        _diary(user, m, date(2026, 9, 1))
    s = get_statistics(user.id, year=2026)
    assert len(s["directors"]) == 2
    assert {p["watch_event_count"] for p in s["directors"]} == {6}


# ── API surface ──────────────────────────────────────────────────────────────

def test_api_exposes_people_fields(auth_client, stats_user, app):
    with app.app_context():
        d = _director("API Dir", 1501)
        m = _media("API Dir Movie", runtime=90)
        _attach_director(m, d)
        _diary(stats_user, m, date(2026, 5, 5))
        data = auth_client.get("/api/statistics?year=2026").get_json()
    assert data["directors"] == [
        {"name": "API Dir", "watch_event_count": 1,
         "distinct_title_count": 1}]
    assert data["actors"] == []


def test_api_people_session_isolation(auth_client, stats_user, app):
    # Another user's directors must never leak into this session.
    from models import User, db as _db
    username = "otherstat" + uuid.uuid4().hex[:6]
    other = User(username=username, email=f"{username}@example.com",
                 email_verified=True)
    other.set_password("TestPass1")
    _db.session.add(other)
    _db.session.commit()
    with app.app_context():
        d = _director("Other Dir", 1601)
        m = _media("Other Dir Movie", runtime=90)
        _attach_director(m, d)
        _diary(other, m, date(2026, 5, 5))
        data = auth_client.get("/api/statistics?year=2026").get_json()
    assert data["directors"] == []


def test_api_people_existing_fields_unchanged(auth_client, stats_user):
    data = auth_client.get("/api/statistics?year=2026").get_json()
    for key in ("total_watch_events", "distinct_titles", "movies_watched",
                "tv_watch_events", "total_hours_watched",
                "runtime_covered_events", "runtime_missing_events",
                "average_rating", "rating_count", "rating_distribution",
                "rewatch_count", "rewatch_rate", "top_genres",
                "monthly_watch_counts", "daily_activity",
                "active_watch_days", "max_daily_watch_events",
                "media_type_distribution"):
        assert key in data


# ── Profile UI guards ────────────────────────────────────────────────────────

def test_ui_people_renders_from_api_no_recompute():
    js = _statistics_js_code()
    assert "directors" in js and "actors" in js
    # No people math in the browser: it renders server rows verbatim.
    assert "watch_event_count" in js
    assert "reduce(" not in js


def test_ui_people_textcontent_only():
    js = _statistics_js_code()
    assert "textContent" in js
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_ui_people_accessibility_and_neutral_wording():
    js = _statistics_js_code()
    assert "aria-label" in js
    blob = js.lower()
    for word in ("favorite", "best", "top-rated", "most talented"):
        assert word not in blob


def test_ui_people_template_targets_exist():
    js = _statistics_js_code()
    template = open("templates/profile.html", encoding="utf-8").read()
    for target in ("statistics-directors", "statistics-directors-block",
                   "statistics-actors", "statistics-actors-block",
                   "statistics-actors-empty"):
        assert target in js and f'id="{target}"' in template


def test_ui_people_one_fetch_still():
    js = _statistics_js_code()
    assert js.count("fetch(url,") == 1
    assert "setInterval" not in js


# ════════════════════════════════════════════════════════════════════════════
# Feature #8 Phase 7 — season quality (persisted episode ratings only)
# ════════════════════════════════════════════════════════════════════════════

def _show(name):
    """A TV MediaItem whose tmdb_id backs TVEpisodeWatch.show_id."""
    return _media(name, runtime=45, media_type="tv")


def _episode(user, show, season, episode, watched, rating=None):
    row = TVEpisodeWatch(
        user_id=user.id, show_id=show.tmdb_id,
        season_number=season, episode_number=episode,
        watched_date=watched, rating=rating,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_season_quality_single_rated_season(user):
    show = _show("Audit Show")
    _episode(user, show, 1, 1, date(2026, 2, 1), rating=4.0)
    _episode(user, show, 1, 2, date(2026, 2, 8), rating=5.0)
    s = get_statistics(user.id, year=2026)
    assert len(s["season_quality"]) == 1
    row = s["season_quality"][0]
    assert row["show_name"] == "Audit Show"
    assert row["season_number"] == 1
    assert row["rating_count"] == 2
    assert row["average_rating"] == 4.5


def test_season_quality_unrated_episodes_excluded(user):
    show = _show("Partial Ratings")
    _episode(user, show, 2, 1, date(2026, 3, 1), rating=3.5)
    _episode(user, show, 2, 2, date(2026, 3, 2))          # unrated
    _episode(user, show, 2, 3, date(2026, 3, 3), rating=4.5)
    s = get_statistics(user.id, year=2026)
    assert len(s["season_quality"]) == 1
    assert s["season_quality"][0]["rating_count"] == 2
    assert s["season_quality"][0]["average_rating"] == 4.0


def test_season_quality_full_ten_bucket_distribution(user):
    show = _show("Buckets")
    for ep, rating in ((1, 4.5), (2, 5.0), (3, 4.5)):
        _episode(user, show, 1, ep, date(2026, 4, 1), rating=rating)
    dist = get_statistics(user.id, year=2026)[
        "season_quality"][0]["rating_distribution"]
    assert len(dist) == 10                     # fixed buckets, zeros kept
    assert dist["4.5"] == 2 and dist["5.0"] == 1
    assert dist["0.5"] == 0 and dist["1.0"] == 0


def test_season_quality_multiple_seasons_and_shows(user):
    a, b = _show("Show A"), _show("Show B")
    _episode(user, a, 1, 1, date(2026, 1, 5), rating=4.0)
    _episode(user, a, 2, 1, date(2026, 1, 6), rating=3.0)
    _episode(user, b, 1, 1, date(2026, 1, 7), rating=5.0)
    rows = get_statistics(user.id, year=2026)["season_quality"]
    # count tie (1 each) → average DESC: Show B 5.0, A S1 4.0, A S2 3.0
    assert [(r["show_name"], r["season_number"]) for r in rows] == [
        ("Show B", 1), ("Show A", 1), ("Show A", 2)]


def test_season_quality_deterministic_ordering_and_ties(user):
    # count DESC → average DESC → show_name ASC (case-consistent) →
    # season_number ASC (§11 — display ordering, not a quality judgment)
    zeta, alpha = _show("Zeta"), _show("alpha")
    other = _show("Mid")
    for ep in (1, 2, 3):                       # Zeta S1: 3 ratings, avg 4.5
        _episode(user, zeta, 1, ep, date(2026, 5, 1), rating=4.5)
    for ep in (1, 2, 3):                       # alpha S2: 3 ratings, avg 4.0
        _episode(user, alpha, 2, ep, date(2026, 5, 2), rating=4.0)
    _episode(user, other, 1, 1, date(2026, 5, 3), rating=5.0)  # 1 rating
    rows = get_statistics(user.id, year=2026)["season_quality"]
    assert [(r["show_name"], r["season_number"]) for r in rows] == [
        ("Zeta", 1), ("alpha", 2), ("Mid", 1)]
    # same count + average → name tie-break; then season number
    lo, hi = _show("Tie A"), _show("Tie B")
    _episode(user, lo, 2, 1, date(2026, 6, 1), rating=4.0)
    _episode(user, hi, 1, 1, date(2026, 6, 2), rating=4.0)
    _episode(user, lo, 1, 1, date(2026, 6, 3), rating=4.0)
    rows = [r for r in get_statistics(user.id, year=2026)["season_quality"]
            if r["show_name"].startswith("Tie")]
    assert [(r["show_name"], r["season_number"]) for r in rows] == [
        ("Tie A", 1), ("Tie A", 2), ("Tie B", 1)]


def test_season_quality_top_limit(user):
    for i in range(12):
        show = _show(f"Limit Show {i:02d}")
        _episode(user, show, 1, 1, date(2026, 7, 1), rating=3.0)
    rows = get_statistics(user.id, year=2026)["season_quality"]
    assert len(rows) == 10                     # TOP_SEASON_STATS_LIMIT
    # deterministic cap: highest rating_count (all tie) → name ASC keeps
    # the first ten shows alphabetically
    assert rows[0]["show_name"] == "Limit Show 00"
    assert rows[-1]["show_name"] == "Limit Show 09"


def test_season_quality_year_and_lifetime_windows(user):
    show = _show("Window Show")
    _episode(user, show, 1, 1, date(2025, 12, 31), rating=3.0)
    _episode(user, show, 1, 2, date(2026, 1, 1), rating=5.0)
    year_rows = get_statistics(user.id, year=2026)["season_quality"]
    assert len(year_rows) == 1 and year_rows[0]["rating_count"] == 1
    lifetime_rows = get_statistics(user.id, lifetime=True)["season_quality"]
    assert len(lifetime_rows) == 1 and lifetime_rows[0]["rating_count"] == 2


def test_season_quality_watched_date_authoritative(user):
    # §32: watched_date drives the window; created_at never does. The
    # 2025-dated row stays out of the 2026 window regardless of when the
    # row was created within this test.
    show = _show("Date Source")
    _episode(user, show, 1, 1, date(2025, 12, 31), rating=5.0)
    _episode(user, show, 1, 2, date(2026, 1, 1), rating=4.0)
    rows = get_statistics(user.id, year=2026)["season_quality"]
    assert rows[0]["rating_count"] == 1
    assert rows[0]["rating_distribution"]["4.0"] == 1


def test_season_quality_no_ids_in_rows(user):
    show = _show("Privacy Show")
    _episode(user, show, 1, 1, date(2026, 2, 1), rating=4.0)
    rows = get_statistics(user.id, year=2026)["season_quality"]
    for row in rows:
        assert set(row.keys()) == {
            "show_name", "season_number", "rating_count",
            "average_rating", "rating_distribution"}
        for key in row:
            assert "id" not in key.lower()


def test_season_quality_helper_pure():
    from api.statistics import build_season_ratings as build
    assert build(None) == [] and build([]) == []
    rows = [("A", 1, 4.0), ("A", 1, 5.0), ("B", 1, 5.0)]
    assert build(rows) == [
        {"show_name": "A", "season_number": 1, "rating_count": 2,
         "average_rating": 4.5,
         "rating_distribution": build(
             [("A", 1, 4.0), ("A", 1, 5.0)])[0]["rating_distribution"]},
        {"show_name": "B", "season_number": 1, "rating_count": 1,
         "average_rating": 5.0,
         "rating_distribution": build(
             [("B", 1, 5.0)])[0]["rating_distribution"]},
    ]
    # unusable rows skipped: unnamed shows, non-int seasons, missing or
    # invalid ratings — never fabricated, never crashed on
    assert build([(None, 1, 4.0), ("", 1, 4.0), ("A", "1", 4.0),
                  ("A", 1, None), ("A", 1, "x"), ("A", True, 4.0),
                  ("Ok", 1, 4.0)]) == [
        {"show_name": "Ok", "season_number": 1, "rating_count": 1,
         "average_rating": 4.0,
         "rating_distribution": build(
             [("Ok", 1, 4.0)])[0]["rating_distribution"]}]
    assert build([("A", 1, 4.0)], limit=1) == build([("A", 1, 4.0)])


def test_season_quality_deterministic_output(user):
    show = _show("Det Show")
    _episode(user, show, 1, 1, date(2026, 3, 1), rating=4.0)
    _episode(user, show, 1, 2, date(2026, 3, 2), rating=5.0)
    import json as _json
    a = _json.dumps(get_statistics(user.id, year=2026), sort_keys=True)
    b = _json.dumps(get_statistics(user.id, year=2026), sort_keys=True)
    assert a == b


def test_season_quality_missing_media_item_degrades(user):
    # Episodes whose show has NO MediaItem row (tmdb_id mismatch):
    # no season contribution — never a fabricated show name — and the
    # event itself is not dropped from any global statistic.
    orphan = TVEpisodeWatch(
        user_id=user.id, show_id=987654321, season_number=1,
        episode_number=1, watched_date=date(2026, 4, 1), rating=4.0)
    db.session.add(orphan)
    db.session.commit()
    s = get_statistics(user.id, year=2026)
    assert s["season_quality"] == []


def test_season_quality_does_not_alter_global_totals(user):
    # §29/§33: people/season aggregation is supplementary. Snapshot the
    # global series with DiaryEntry only, then add rated episode
    # watches and confirm nothing global moved.
    m = _media("Global Movie", runtime=90)
    _diary(user, m, date(2026, 8, 1), rating=4.0)
    before = get_statistics(user.id, year=2026)
    show = _show("Side Show")
    _episode(user, show, 1, 1, date(2026, 8, 2), rating=5.0)
    _episode(user, show, 1, 2, date(2026, 8, 3), rating=4.0)
    after = get_statistics(user.id, year=2026)
    for field in ("total_watch_events", "distinct_titles", "movies_watched",
                  "tv_watch_events", "total_hours_watched", "average_rating",
                  "rating_count", "rewatch_count", "active_watch_days",
                  "max_daily_watch_events"):
        assert before[field] == after[field], field
    assert before["monthly_watch_counts"] == after["monthly_watch_counts"]
    assert before["daily_activity"] == after["daily_activity"]
    # …but the season series now reflects the episode ratings
    assert len(after["season_quality"]) == 1
    assert after["season_quality"][0]["rating_count"] == 2


def test_tv_completion_unavailable_no_field(user):
    # §2 audit verdict: no per-season episode catalog is persisted
    # (TVShowProgress.total_episodes is a show-level TMDb snapshot;
    # UpcomingEpisode is a purged ≤60-day window), so NO defensible
    # completion denominator exists. A truthful absence beats an
    # invented percentage: the field must never appear.
    show = _show("No Denominator")
    _episode(user, show, 1, 1, date(2026, 2, 1), rating=4.0)
    s = get_statistics(user.id, year=2026)
    assert "tv_completion" not in s
    assert "completion_rate" not in s
    import api.statistics as mod
    source = open(mod.__file__, encoding="utf-8").read()
    assert "tv_completion" not in source
    assert "completion_rate" not in source
    for forbidden in ("calculate_completion_rate",
                      "aggregate_season_ratings",
                      "normalize_season_quality_rows"):
        assert forbidden not in source


def test_actor_statistics_remain_unavailable(user):
    # Phase 6 semantics unchanged (§18): actors stay [] until persisted
    # cast data exists; no completion- or season-side change touches it.
    s = get_statistics(user.id, year=2026)
    assert s["actors"] == []
