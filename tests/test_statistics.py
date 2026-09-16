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
    assert len(statements) <= 5, statements


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
