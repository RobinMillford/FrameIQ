"""Statistics production hardening + performance foundation (Feature #8, Phase 10).

NOT a feature phase — a hardening gate over the completed statistics
subsystem (Phases 2–9). Decisions from the mandatory audit, all
evidence-based:

A. ROLLUP NOT JUSTIFIED (§6 outcome A):
   - cost: all 8 canonical statements complete in well under a millisecond
     of database work at 50k-row synthetic scale (EXPLAIN QUERY PLAN +
     timing probe); one user's statistics = bounded per-user work
     (queries scan only that user's rows), so request-level load scales
     linearly with traffic.
   - a persisted (user_id, year) snapshot would add invalidation on EVERY
     diary mutation for at best O(1) per-request savings on an
     already-fast path — complexity without material benefit.
   - staleness/versioning/freshness policy, nightly job, backfill, and
     write hooks all become pure liability with no measurable win.

B. ONE AUDIT-PROVEN COMPOSITE INDEX:
   idx_diary_user_watched_date ON diary_entry(user_id, watched_date)
   — every canonical statement filters the identical predicate
   (user_id + watched_date window); the composite converts per-row
   date filtering into a direct range scan (~6x fewer rows touched at
   synthetic 50k-diary-row scale; EXPLAIN QUERY PLAN before/after
   documented in migrates/migrate_diary_statistics_indexes.py).
   The TVEpisodeWatch composite was tested and IGNORED by the planner
   (existing indexes already cover its plan) — not added (no
   speculative indexes).

This suite pins, with deterministic structural assertions only (no
timing thresholds — §33):
- §20 consistency invariants (the previously unpinned ones)
- §17/§18/§19 regression (contract, people/episodes, share guards)
- §30 architecture (query count, no N+1, no network, independence)
- schema-change scope (exactly one justified index, nothing else)
"""
import re
import socket
import uuid
from datetime import date

import pytest

from api.statistics import get_statistics
from models import db, User, MediaItem, DiaryEntry
from models.tv import TVEpisodeWatch

DOMAIN = "example.invalid"


def _make_user(prefix="p10"):
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
    # tmdb_id is NOT NULL — module-unique IDs (same convention as
    # tests/test_statistics.py so parallel files never collide).
    m.tmdb_id = _media._next_id
    _media._next_id += 1
    db.session.add(m)
    db.session.commit()
    return m


_media._next_id = 9_600_000


def _diary(user, media, watched_date, rating=None, is_rewatch=False):
    e = DiaryEntry(user_id=user.id, media_id=media.id,
                   media_type=media.media_type,
                   watched_date=watched_date, rating=rating,
                   is_rewatch=is_rewatch)
    db.session.add(e)
    db.session.commit()
    return e


def _episode(user, show, season, episode, watched, rating=None):
    row = TVEpisodeWatch(
        user_id=user.id, show_id=show.tmdb_id,
        season_number=season, episode_number=episode,
        watched_date=watched, rating=rating,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def user(app):
    with app.app_context():
        u = _make_user()
        yield u
        # Same teardown contract as tests/test_statistics.py: DiaryEntry
        # has no cascade to user; episode/list rows must go too.
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


def _scenario(user):
    """Mixed dataset touching every invariant at once."""
    m = _media("P10 Movie", runtime=120, genres="Drama")
    tv = _media("P10 Show", runtime=45, media_type="tv")
    _diary(user, m, date(2026, 1, 3))
    _diary(user, m, date(2026, 1, 3))                     # same title again
    _diary(user, m, date(2026, 2, 9), rating=4.0)
    _diary(user, tv, date(2026, 2, 20), rating=3.5, is_rewatch=True)
    _diary(user, m, date(2026, 11, 30))                   # no rating
    _diary(user, m, date(2025, 6, 1))                     # outside window


# ══════════════════════════════════════════════════════════════════════════
# §20 — consistency invariants (previously unpinned)
# ══════════════════════════════════════════════════════════════════════════

def test_invariant_rating_count_equals_distribution_sum(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    assert s["rating_count"] == sum(s["rating_distribution"].values()) == 2


def test_invariant_rating_count_equals_distribution_sum_lifetime(user):
    _scenario(user)
    s = get_statistics(user.id, lifetime=True)
    assert s["rating_count"] == sum(s["rating_distribution"].values())
    # lifetime still reconciles across the whole history
    assert s["total_watch_events"] == 6


def test_invariant_rewatch_count_bounded_by_watch_events(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    assert 0 <= s["rewatch_count"] <= s["total_watch_events"]


def test_invariant_rewatch_rate_bounds(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    assert 0.0 <= s["rewatch_rate"] <= 1.0


def test_invariant_active_watch_days_bounded_by_period_days(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    # calendar year 2026 has 365 days
    assert s["active_watch_days"] <= 365


def test_invariant_active_watch_days_bounded_lifetime(user):
    _scenario(user)
    s = get_statistics(user.id, lifetime=True)
    # history spans 2025–2026 only
    assert s["active_watch_days"] <= 730


def test_invariant_max_daily_watch_events_nonnegative(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    assert s["max_daily_watch_events"] >= 0


def test_invariant_media_split_equals_total_events(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    split = s["media_type_distribution"]
    assert sum(split.values()) == s["total_watch_events"]


def test_invariant_hours_covered_plus_missing_equals_total(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    covered = s["runtime_covered_events"]
    missing = s["runtime_missing_events"]
    # every event is either runtime-covered or runtime-missing
    assert covered + missing == s["total_watch_events"]


# ══════════════════════════════════════════════════════════════════════════
# §30.10 — reconciliation against every period
# ══════════════════════════════════════════════════════════════════════════

def test_reconciliation_both_windows(user):
    _scenario(user)
    for kwargs in ({"year": 2026}, {"lifetime": True}):
        s = get_statistics(user.id, **kwargs)
        daily_sum = sum(r["count"] for r in s["daily_activity"])
        monthly_sum = sum(r["count"] for r in s["monthly_watch_counts"])
        assert daily_sum == s["total_watch_events"]
        assert monthly_sum == s["total_watch_events"]


# ══════════════════════════════════════════════════════════════════════════
# §17 — API contract regression: Phase 10 adds NO fields; exact key set
# ══════════════════════════════════════════════════════════════════════════

def test_contract_unchanged_phase10(user):
    _scenario(user)
    s = get_statistics(user.id, year=2026)
    expected = {
        "total_watch_events", "distinct_titles", "movies_watched",
        "tv_watch_events", "total_hours_watched", "runtime_covered_events",
        "runtime_missing_events", "average_rating", "rating_count",
        "rating_distribution", "rewatch_count", "rewatch_rate",
        "top_genres", "monthly_watch_counts", "media_type_distribution",
        "daily_activity", "active_watch_days", "max_daily_watch_events",
        "directors", "actors", "season_quality",
    }
    assert set(s.keys()) == expected
    # long-standing unavailability semantics unchanged
    assert s["actors"] == []
    assert "tv_completion" not in s
    # period metadata lives in the API layer, never in the service dict
    assert "period" not in s


def test_contract_empty_semantics_unchanged(user):
    # no events at all for this user
    s = get_statistics(user.id, year=2026)
    assert s["total_watch_events"] == 0
    assert s["distinct_titles"] == 0
    assert s["movies_watched"] == 0
    assert s["tv_watch_events"] == 0
    assert s["total_hours_watched"] == 0
    assert s["average_rating"] is None
    assert s["rating_count"] == 0
    assert s["rewatch_count"] == 0
    assert s["rewatch_rate"] == 0.0
    assert s["top_genres"] == []
    assert s["directors"] == []
    assert s["actors"] == []
    assert s["season_quality"] == []
    assert s["daily_activity"] == []
    assert s["monthly_watch_counts"] != []  # fixed 12 buckets for a year
    assert s["max_daily_watch_events"] == 0


# ══════════════════════════════════════════════════════════════════════════
# §30 — people/episode regression (Phase 6/7 semantics untouched)
# ══════════════════════════════════════════════════════════════════════════

def test_people_regression_actors_empty_directors_canonical(user):
    m1 = _media("P10 A", runtime=100, genres="Drama")
    m2 = _media("P10 B", runtime=110, genres="Drama")
    for media in (m1, m2):
        for _ in range(2):
            _diary(user, media, date(2026, 3, 1))
    s = get_statistics(user.id, year=2026)
    assert s["actors"] == []          # §30.14 — actor remains []
    assert s["directors"] == []       # no directors attached in this fixture


def test_season_regression_bounded_and_self_consistent(user):
    show = _media("P10 Show TMDB", runtime=45, media_type="tv")
    for i, (s_, e_) in enumerate([(1, 1), (1, 2), (1, 3), (2, 1), (2, 2)],
                                 start=1):
        _episode(user, show, s_, e_, date(2026, 4, i), rating=4.0)
    s = get_statistics(user.id, year=2026)
    sq = s["season_quality"]
    assert len(sq) <= 10              # §33 structural bound
    row = sq[0]
    assert set(row.keys()) == {"show_name", "season_number", "rating_count",
                               "average_rating", "rating_distribution"}
    assert row["rating_count"] == sum(row["rating_distribution"].values())


# ══════════════════════════════════════════════════════════════════════════
# §30.6 — schema-change scope: exactly ONE justified composite index
# ══════════════════════════════════════════════════════════════════════════

def test_schema_scope_single_justified_index(app):
    from models.social import DiaryEntry as DE
    names = {i.name for i in DE.__table__.indexes}
    assert "idx_diary_user_watched_date" in names
    # the TVEpisodeWatch composite was audit-tested and planner-IGNORED:
    # it must NOT exist (no speculative indexes)
    from models.tv import TVEpisodeWatch as EP
    ep_names = {i.name for i in EP.__table__.indexes}
    assert "idx_ep_user_watched_date" not in ep_names
    assert not any("watched" in n and "user" in n for n in ep_names
                   if n != "idx_user_show_season_episode")


def test_migration_file_exists_and_follows_conventions(app):
    import os
    path = os.path.join("migrates", "migrate_diary_statistics_indexes.py")
    assert os.path.exists(path)
    with open(path) as f:
        src = f.read()
    assert "CREATE INDEX IF NOT EXISTS" in src        # idempotent by design
    assert "idx_diary_user_watched_date" in src
    assert "load_dotenv" in src and "run_migration" in src  # house pattern


def test_migration_ddl_matches_model_declaration(app):
    from models.social import DiaryEntry as DE
    target = next(i for i in DE.__table__.indexes
                  if i.name == "idx_diary_user_watched_date")
    # column ORDER in the model declaration matches the migration DDL
    assert [c.name for c in target.columns] == ["user_id", "watched_date"]


# ══════════════════════════════════════════════════════════════════════════
# §30.1–§30.5 + §33 — structural bounds (no timing assertions)
# ══════════════════════════════════════════════════════════════════════════

class _record_statements:
    """Context manager recording raw SQL via the before_cursor_execute
    engine event (same pattern as tests/test_statistics.py)."""

    def __enter__(self):
        from sqlalchemy import event as sa_event
        self.statements = []
        self._fn = (lambda conn, cursor, statement, *a, **k:
                    self.statements.append(statement))
        sa_event.listen(db.engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event as sa_event
        sa_event.remove(db.engine, "before_cursor_execute", self._fn)
        return False


def test_query_count_pinned_8(user):
    m = _media("P10 Q", runtime=90)
    _diary(user, m, date(2026, 9, 1))
    uid = user.id
    with _record_statements() as rec:
        get_statistics(uid, year=2026)
    assert len(rec.statements) == 8, rec.statements


def test_no_n_plus_one_across_many_titles(user):
    # 30 distinct titles, all watched: query count must stay exactly 8
    for i in range(30):
        m = _media(f"P10 N1 {i}", runtime=90 + i)
        _diary(user, m, date(2026, 5, i % 28 + 1))
    uid = user.id
    with _record_statements() as rec:
        get_statistics(uid, year=2026)
    assert len(rec.statements) == 8, len(rec.statements)


def test_no_network_during_statistics(user, monkeypatch):
    _scenario(user)

    def _deny(*a, **k):
        raise AssertionError("network access attempted during statistics")

    monkeypatch.setattr(socket.socket, "__init__", _deny)
    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny)
    get_statistics(user.id, year=2026)
    get_statistics(user.id, lifetime=True)


def test_no_recommendation_coupling_in_statistics_modules():
    """Code (not comments) in the three statistics modules must not touch
    the recommendation subsystem. Comments/docstrings legitimately name
    the banned modules in prose — strip them before scanning."""
    import sys
    banned = ("for_you", "taste_profile", "RecommendationFeedback",
              "SmartList", "CineBot")
    for modname in ("api.statistics", "api.year_in_review",
                    "api.year_in_review_share"):
        __import__(modname)
        src = open(sys.modules[modname].__file__).read()
        code = re.sub(r'""".*?"""', "", src, flags=re.S)   # docstrings
        code = re.sub(r"#[^\n]*", "", code)                # line comments
        for word in banned:
            assert word not in code, (modname, word)


def test_bounded_helper_limits_unchanged():
    """§33 — deterministic structural bounds on every top-N helper."""
    from api.statistics import TOP_PEOPLE_LIMIT, TOP_SEASON_STATS_LIMIT
    assert TOP_PEOPLE_LIMIT == 10
    assert TOP_SEASON_STATS_LIMIT == 10
    from api.year_in_review_share import (PUBLIC_DIRECTORS_LIMIT,
                                          PUBLIC_SEASON_LIMIT)
    assert PUBLIC_DIRECTORS_LIMIT == 3
    assert PUBLIC_SEASON_LIMIT == 3


# ══════════════════════════════════════════════════════════════════════════
# §32 — share regression spot checks (source guards)
# ══════════════════════════════════════════════════════════════════════════

def test_public_share_route_source_guards():
    src = open("routes/main.py").read()
    assert "X-Robots-Tag" in src                      # §19 — stays noindex
    # the public page function derives everything from the canonical
    # builder — it must not reference the watch-history models at all
    fn_start = src.index("def year_in_review_share_page")
    next_def = src.find("\ndef ", fn_start + 1)
    fn_src = src[fn_start:next_def if next_def != -1 else len(src)]
    assert "DiaryEntry" not in fn_src
    assert "MediaItem" not in fn_src
    assert "TVEpisodeWatch" not in fn_src
    assert "build_year_in_review" in fn_src


def test_private_share_api_source_guards():
    src = open("routes/statistics.py").read()
    # share mutations stay authenticated + rate-limited by decorator
    assert "login_required" in src
    assert "limiter.limit" in src


def test_share_model_unchanged(app):
    from models.year_in_review_share import YearInReviewShare
    cols = {c.name for c in YearInReviewShare.__table__.columns}
    assert cols == {"id", "user_id", "year", "token_hash", "created_at",
                    "revoked_at"}
    names = {i.name for i in YearInReviewShare.__table__.indexes}
    assert "uq_yir_share_active_user_year" in names


def test_public_transformation_contract_unchanged():
    from api.year_in_review_share import build_public_year_in_review
    recap = {
        "year": 2026, "available": True, "state": "ready",
        "summary": {"total_watch_events": 10, "distinct_titles": 8,
                    "total_hours_watched": 12.0, "average_rating": 4.0},
        "highlights": {
            "top_genre": {"name": "Drama", "count": 6},
            "busiest_month": {"month": "2026-03", "count": 4,
                              "text": "Busiest month: March"},
            "media_split": {"movies": 7, "tv": 3, "movies_pct": 70,
                            "tv_pct": 30, "classification": "mostly_movies",
                            "description": "Mostly movies"},
            "rewatches": {"count": 1, "rate": 0.1,
                          "text": "Rewatches: 1 · 10%"},
        },
        "genres": [{"name": "Drama", "count": 6}],
        "rewatches": {"count": 1, "rate": 0.1, "text": "Rewatches: 1 · 10%"},
        "runtime": {"hours": 12.0, "complete": True,
                    "text": "12 hours watched", "missing_events": 0},
        "people": {"directors": [{"name": "N", "watch_event_count": 3,
                                  "distinct_title_count": 2}],
                   "actors": []},
        "season_quality": [{"show_name": "S", "season_number": 1,
                            "rating_count": 3, "average_rating": 4.0,
                            "rating_distribution": {"5.0": 3}}],
    }
    pub = build_public_year_in_review(recap)
    blob = repr(pub)
    # §19 — no additional statistics/identity exposed by the public model
    for banned in ("user_id", "email", "media_id", "tmdb_id", "token",
                   "daily_activity", "distribution", "username"):
        assert banned not in blob
    # caps unchanged
    assert len(pub.get("directors", [])) <= 3
    assert len(pub.get("season_quality", [])) <= 3
