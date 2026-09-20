"""Regression: the TV event invariant must hold for orphan show_id rows.

Production incident: ``assert len(tv_episode_ids) == tv_event_count``
(api/statistics.py) fired a 500 for a user with 148 TVEpisodeWatch
rows, 148 distinct (show_id, season_number, episode_number)
identities, ZERO duplicate identities and ZERO rewatch rows.

Root cause (proven on the code, not assumed): Query 1's TV source
inner-joins MediaItem, but the write path
(``routes/tv_tracking.mark_episode_watched_core``) creates
TVEpisodeWatch rows WITHOUT creating a MediaItem row for the show.
Episodes whose show has no MediaItem row are dropped from the JOIN
while ``_tv_episode_ids`` (id-only, join-free) still returns them —
so the id list outgrew the join-filtered count and the assertion
failed even though the underlying data had no duplicates.

The fixture below mirrors the production shape: 148 unique episode
rows across 5 distinct show ids, zero duplicate episode identities,
zero rewatch rows — one show deliberately has NO MediaItem row (the
orphan). No production-specific ids are hard-coded.
"""
import uuid
from datetime import date, timedelta
from itertools import count

import pytest

from models import DiaryEntry, MediaItem, User
from models.tv import TVEpisodeWatch
from api.statistics import get_statistics

_TMDB = count(9_950_000)  # module-local base (calendar suite owns 9_900_000;
# shared session DB — bases must never overlap)


def _user(db):
    u = User(username=f"tvinv_{uuid.uuid4().hex[:10]}",
             email=f"tvinv_{uuid.uuid4().hex[:10]}@inv.test",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


def _movie(db):
    m = MediaItem(tmdb_id=next(_TMDB), media_type="movie",
                  title=f"Movie {uuid.uuid4().hex[:8]}", runtime=110)
    db.session.add(m)
    db.session.flush()
    return m


def _show(db):
    m = MediaItem(tmdb_id=next(_TMDB), media_type="tv",
                  title=f"Show {uuid.uuid4().hex[:8]}", runtime=55)
    db.session.add(m)
    db.session.flush()
    return m


def _episode(db, user_id, show_id, season, episode, day, **kw):
    db.session.add(TVEpisodeWatch(
        user_id=user_id, show_id=show_id,
        season_number=season, episode_number=episode,
        watched_date=day, **kw))


def _production_shape(db):
    """148 unique episodes / 5 shows / no duplicate identities /
    no rewatches. Shows 0-3 are hydrated (MediaItem exists); show 4
    is an ORPHAN — real TVEpisodeWatch rows, no MediaItem row —
    exactly the state the write path produces for shows that were
    never movie-style hydrated."""
    u = _user(db)
    hydrated = [_show(db) for _ in range(4)]
    orphan_show_id = next(_TMDB)  # consumed without creating a MediaItem

    rows_per_show = [30, 30, 30, 28, 30]  # sums to 148
    day0 = date(2026, 1, 1)
    made = 0
    for idx, show_id in enumerate(
            [m.tmdb_id for m in hydrated] + [orphan_show_id]):
        for n in range(rows_per_show[idx]):
            _episode(db, u.id, show_id, 1, n + 1,
                     day0 + timedelta(days=made % 200))
            made += 1
    assert made == 148
    db.session.commit()
    return u, orphan_show_id


# ── Phase 11 — production-shape reproduction ────────────────────────

def test_production_shape_returns_200_not_500(db, app, client):
    """The literal production incident: 148 episodes / 5 shows / no
    duplicate identities / no rewatches → get_statistics must succeed
    and /api/statistics must return 200 (not an AssertionError 500)."""
    u, _orphan = _production_shape(db)

    # Direct service call — used to raise AssertionError before the fix.
    stats = get_statistics(u.id, year=2026)
    assert stats["tv_watch_events"] == 148
    assert stats["total_watch_events"] == 148
    assert stats["tv_shows_watched"] == 5  # distinct show ids, orphan included
    assert stats["rewatch_count"] == 0
    assert stats["rating_count"] == 0

    # Same path production hit — the authenticated API route.
    client.post("/login", data={"username": u.username,
                                "password": "TestPass1"},
                follow_redirects=True)
    resp = client.get("/api/statistics")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["tv_watch_events"] == 148
    assert payload["total_watch_events"] == 148


def test_fully_orphan_show_still_counts(db, app):
    """Every TV row orphaned: no assertion, counts stay truthful."""
    u = _user(db)
    show_id = next(_TMDB)
    for n in range(3):
        _episode(db, u.id, show_id, 1, n + 1,
                 date(2026, 2, 1) + timedelta(days=n))
    db.session.commit()

    stats = get_statistics(u.id, year=2026)
    assert stats["tv_watch_events"] == 3
    assert stats["total_watch_events"] == 3
    assert stats["tv_shows_watched"] == 1
    assert stats["movies_watched"] == 0


# ── Phase 12 — period / source / semantics regressions ──────────────

def test_year_window_vs_lifetime_on_orphan_data(db, app):
    """Period predicates on BOTH sides (count + ids) must agree:
    year=2026 counts only 2026 rows; lifetime counts all."""
    u = _user(db)
    orphan = next(_TMDB)
    for n in range(2):   # 2025 rows
        _episode(db, u.id, orphan, 1, n + 1, date(2025, 6, 1 + n))
    for n in range(4):   # 2026 rows
        _episode(db, u.id, orphan, 2, n + 1, date(2026, 3, 1 + n))
    db.session.commit()

    year_stats = get_statistics(u.id, year=2026)
    assert year_stats["tv_watch_events"] == 4
    assert year_stats["total_watch_events"] == 4

    life = get_statistics(u.id, lifetime=True)
    assert life["tv_watch_events"] == 6
    assert life["total_watch_events"] == 6


def test_mixed_movie_and_orphan_tv_same_month(db, app, client):
    """Movie + TV events in one month must ADD up (no bucket
    overwrite) and totals must include orphan episodes."""
    u = _user(db)
    orphan = next(_TMDB)
    movie = _movie(db)
    db.session.add(DiaryEntry(user_id=u.id, media_id=movie.id,
                              media_type="movie",
                              watched_date=date(2026, 3, 5)))
    _episode(db, u.id, orphan, 1, 1, date(2026, 3, 10))
    _episode(db, u.id, orphan, 1, 2, date(2026, 3, 11))
    db.session.commit()

    stats = get_statistics(u.id, year=2026)
    assert stats["total_watch_events"] == 3
    # movies_watched is a TITLE count (distinct media ids) — exactly 1.
    assert stats["movies_watched"] == 1
    assert stats["tv_watch_events"] == 2
    assert stats["distinct_titles"] == 2  # 1 movie + 1 show

    march = next(m for m in stats["monthly_watch_counts"]
                 if m["month"] == "2026-03")
    assert march["count"] == 3  # 1 movie + 2 episodes, additive

    # Authenticated route must render the same numbers.
    client.post("/login", data={"username": u.username,
                                "password": "TestPass1"},
                follow_redirects=True)
    resp = client.get("/api/statistics")
    assert resp.status_code == 200
    assert resp.get_json()["tv_watch_events"] == 2


def test_rewatch_rows_on_orphan_show_counted(db, app):
    """Rewatch semantics are untouched: an explicit rewatch row is a
    watch event AND a rewatch (the incident was NOT a rewatch bug)."""
    u = _user(db)
    orphan = next(_TMDB)
    _episode(db, u.id, orphan, 1, 1, date(2026, 4, 1))
    _episode(db, u.id, orphan, 1, 1, date(2026, 4, 2), is_rewatch=True)
    db.session.commit()

    stats = get_statistics(u.id, year=2026)
    assert stats["tv_watch_events"] == 2
    assert stats["rewatch_count"] == 1


def test_hydrated_baseline_unchanged(db, app):
    """The good path (all shows hydrated) keeps identical semantics:
    runtime counts, distinct titles, and the invariant all hold."""
    u = _user(db)
    show = _show(db)
    movie = _movie(db)
    for n in range(4):
        _episode(db, u.id, show.tmdb_id, 1, n + 1,
                 date(2026, 5, 1 + n), rating=4.0)
    db.session.add(DiaryEntry(user_id=u.id, media_id=movie.id,
                              media_type="movie",
                              watched_date=date(2026, 5, 3), rating=3.0))
    db.session.commit()

    stats = get_statistics(u.id, year=2026)
    assert stats["tv_watch_events"] == 4
    assert stats["total_watch_events"] == 5
    assert stats["tv_shows_watched"] == 1
    assert stats["distinct_titles"] == 2
    assert stats["runtime_covered_events"] == 5  # both carry runtime
    assert stats["rating_count"] == 5
    assert stats["average_rating"] == pytest.approx(3.8)


def test_movie_only_user_unaffected(db, app):
    u = _user(db)
    movie = _movie(db)
    db.session.add(DiaryEntry(user_id=u.id, media_id=movie.id,
                              media_type="movie",
                              watched_date=date(2026, 1, 9)))
    db.session.commit()

    stats = get_statistics(u.id, year=2026)
    assert stats["total_watch_events"] == 1
    assert stats["tv_watch_events"] == 0
    assert stats["distinct_titles"] == 1


def test_empty_tv_data_neutral_shape(db, app):
    """A user with no events at all keeps the zero-shape contract."""
    u = _user(db)
    stats = get_statistics(u.id, year=2026)
    assert stats["total_watch_events"] == 0
    assert stats["tv_watch_events"] == 0
    assert stats["tv_shows_watched"] == 0
    assert stats["rating_count"] == 0
