"""Unified personal entertainment calendar (Feature 10) — focused suite.

Covers the Phase 29 matrix:

API:      auth required, user isolation, default/bounded/invalid ranges,
          deterministic ordering
TV:       tracked episode appears; untracked excluded; correct S/E;
          no duplicates; watched state does not remove the event
MOVIES:   watchlisted release appears; unrelated releases excluded;
          missing release date → unknown bucket, calendar not blocked
MIXED:    movie + TV same day/month, chronological order, tie-break
FILTERS:  type + scope combinations
PERF:     fixed bounded query count regardless of event count
SECURITY: no user_id parameter; another user's data never appears

Runs on the session temp-file SQLite (conftest). All tmdb ids come from
a module-local 9_900_000+ range (other suites claim 9.6M/9.7M/9.8M).
"""
import uuid
from datetime import date, timedelta
from itertools import count

import pytest

from models import MediaItem, TVShowProgress, UpcomingEpisode
from models.associations import user_watchlist, user_viewed
from models.base import db
from models.tv import TVEpisodeWatch

_TMDB = count(9_900_000)

_TODAY = date.today()


def _user(db):
    from models import User

    u = User(username=f"cal_{uuid.uuid4().hex[:10]}",
             email=f"cal_{uuid.uuid4().hex[:10]}@cal.test",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


def _movie(db, title=None, release=None):
    m = MediaItem(tmdb_id=next(_TMDB), media_type="movie",
                  title=title or f"Movie {uuid.uuid4().hex[:8]}",
                  release_date=release)
    db.session.add(m)
    db.session.flush()
    return m


def _show(db, title=None):
    m = MediaItem(tmdb_id=next(_TMDB), media_type="tv",
                  title=title or f"Show {uuid.uuid4().hex[:8]}")
    db.session.add(m)
    db.session.flush()
    return m


def _track(db, user, show, status="watching"):
    row = TVShowProgress(user_id=user.id, show_id=show.tmdb_id,
                         status=status)
    db.session.add(row)
    db.session.flush()
    return row


def _upcoming(db, show, season, episode, when, name=None, air_time=None):
    row = UpcomingEpisode(
        show_id=show.tmdb_id, show_name=show.title,
        poster_path=None, season_number=season, episode_number=episode,
        episode_name=name, air_date=when, air_time=air_time)
    db.session.add(row)
    return row


def _watch(db, user, show, season, episode):
    db.session.add(TVEpisodeWatch(
        user_id=user.id, show_id=show.tmdb_id,
        season_number=season, episode_number=episode,
        watched_date=date(2026, 1, 1)))


def _watchlist(db, user, media):
    db.session.execute(user_watchlist.insert().values(
        user_id=user.id, media_id=media.id,
        media_type=media.media_type))


def _viewed(db, user, media):
    db.session.execute(user_viewed.insert().values(
        user_id=user.id, media_id=media.id,
        media_type=media.media_type))


def _login(client, user):
    client.post("/login", data={"username": user.username,
                                "password": "TestPass1"},
                follow_redirects=True)


def _get(client, q=""):
    return client.get(f"/api/calendar{q}")


# ─────────────────────────── API contract ───────────────────────────

class TestCalendarAPI:
    def test_requires_auth(self, client):
        resp = client.get("/api/calendar")
        assert resp.status_code in (301, 302, 401)

    def test_default_range_is_bounded(self, db, client):
        u = _user(db)
        _login(client, u)
        data = _get(client).get_json()
        meta = data["meta"]
        span = (date.fromisoformat(meta["end_date"])
                - date.fromisoformat(meta["start_date"])).days
        assert 0 < span <= 62
        assert meta["counts"]["total"] == 0

    def test_invalid_date_rejected(self, db, client):
        u = _user(db)
        _login(client, u)
        assert _get(client, "?start=not-a-date").status_code == 400
        assert _get(client, "?end=2026-13-99").status_code == 400

    def test_oversized_range_is_capped(self, db, client):
        u = _user(db)
        _login(client, u)
        data = _get(
            client, "?start=2026-01-01&end=2027-06-01").get_json()
        assert data["meta"]["range_capped"] is True
        span = (date.fromisoformat(data["meta"]["end_date"])
                - date.fromisoformat(data["meta"]["start_date"])).days
        assert span <= 62

    def test_deterministic_ordering(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        later = _TODAY + timedelta(days=10)
        sooner = _TODAY + timedelta(days=2)
        _upcoming(db, s, 1, 1, later)
        _upcoming(db, s, 1, 2, sooner)
        db.session.commit()

        _login(client, u)
        events = _get(client).get_json()["events"]
        dates = [e["date"] for e in events]
        assert dates == sorted(dates)
        assert events[0]["episode_number"] == 2  # sooner first

    def test_unknown_release_dates_do_not_block(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        db.session.commit()

        _login(client, u)
        data = _get(client).get_json()
        assert data["events"] == []
        assert data["meta"]["release_date_unknown"]["count"] == 1


# ─────────────────────────── TV events ──────────────────────────────

class TestTVEvents:
    def test_tracked_episode_appears(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        when = _TODAY + timedelta(days=3)
        _upcoming(db, s, 4, 8, when, name="Final Episode", air_time="21:00")
        db.session.commit()

        _login(client, u)
        ev = _get(client).get_json()["events"][0]
        assert ev["event_type"] == "episode"
        assert ev["title"] == s.title
        assert ev["season_number"] == 4
        assert ev["episode_number"] == 8
        assert ev["metadata"]["episode_name"] == "Final Episode"
        assert ev["time"] == "21:00"   # shown only because sync captured it
        assert ev["is_tracked"] is True
        assert ev["detail_url"] == f"/tv/{s.tmdb_id}"

    def test_untracked_show_excluded(self, db, client):
        u, other = _user(db), _user(db)
        s = _show(db)
        _track(db, other, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=3))
        db.session.commit()

        _login(client, u)
        assert _get(client).get_json()["events"] == []

    def test_no_duplicate_events(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        when = _TODAY + timedelta(days=1)
        _upcoming(db, s, 1, 1, when)
        db.session.commit()

        # The sync deduplicates at the SCHEMA level: (show_id, season,
        # episode) is UNIQUE on upcoming_episode — a second row for the
        # same episode is impossible, so the calendar can never double-
        # list an episode from duplicate sync rows.
        with pytest.raises(Exception):
            _upcoming(db, s, 1, 1, when)
            db.session.flush()
        db.session.rollback()

        _login(client, u)
        events = _get(client).get_json()["events"]
        assert len(events) == 1

    def test_watched_episode_still_listed_but_flagged(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        when = _TODAY + timedelta(days=1)
        _upcoming(db, s, 1, 1, when)
        _watch(db, u, s, 1, 1)
        db.session.commit()

        _login(client, u)
        events = _get(client).get_json()["events"]
        # tracking is not watching: the event stays, flagged watched
        assert len(events) == 1
        assert events[0]["watched"] is True

    def test_tracked_only_status_excluded(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s, status="completed")
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2))
        db.session.commit()

        _login(client, u)
        assert _get(client).get_json()["events"] == []


# ─────────────────────────── movie events ───────────────────────────

class TestMovieEvents:
    def test_watchlisted_release_appears(self, db, client):
        u = _user(db)
        when = _TODAY + timedelta(days=20)
        m = _movie(db, release=when)
        _watchlist(db, u, m)
        db.session.commit()

        _login(client, u)
        ev = _get(client).get_json()["events"][0]
        assert ev["event_type"] == "movie_release"
        assert ev["title"] == m.title
        assert ev["date"] == when.isoformat()
        # general release date only — never relabelled as streaming
        assert ev["release_type"] == "theatrical"
        assert ev["is_watchlisted"] is True
        assert ev["detail_url"] == f"/movie/{m.tmdb_id}"

    def test_non_watchlisted_release_excluded(self, db, client):
        u = _user(db)
        _movie(db, release=_TODAY + timedelta(days=5))  # nobody watchlisted it
        db.session.commit()

        _login(client, u)
        assert _get(client).get_json()["events"] == []

    def test_viewed_movie_not_duplicated_as_release(self, db, client):
        u = _user(db)
        m = _movie(db, release=_TODAY + timedelta(days=5))
        _viewed(db, u, m)   # watched, NOT watchlisted → not a calendar event
        db.session.commit()

        _login(client, u)
        assert _get(client).get_json()["events"] == []

    def test_past_watchlisted_release_excluded_outside_window(self, db, client):
        u = _user(db)
        m = _movie(db, release=_TODAY - timedelta(days=40))
        _watchlist(db, u, m)
        db.session.commit()

        _login(client, u)
        assert _get(client).get_json()["events"] == []


# ─────────────────────────── mixed + filters ────────────────────────

class TestMixedAndFilters:
    def test_same_day_movie_and_tv(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        when = _TODAY + timedelta(days=4)
        _upcoming(db, s, 1, 1, when)
        m = _movie(db, release=when)
        _watchlist(db, u, m)
        db.session.commit()

        _login(client, u)
        events = _get(client).get_json()["events"]
        assert len(events) == 2
        assert {e["event_type"] for e in events} == {
            "episode", "movie_release"}
        # deterministic tie-break: episodes before movie_release
        assert [e["event_type"] for e in events] == [
            "episode", "movie_release"]

    def test_type_filter_tv(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2))
        _watchlist(db, u, _movie(db, release=_TODAY + timedelta(days=3)))
        db.session.commit()

        _login(client, u)
        events = _get(client, "?type=tv").get_json()["events"]
        assert {e["event_type"] for e in events} == {"episode"}

    def test_scope_watchlist_excludes_tv(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2))
        _watchlist(db, u, _movie(db, release=_TODAY + timedelta(days=3)))
        db.session.commit()

        _login(client, u)
        events = _get(client, "?scope=watchlist").get_json()["events"]
        assert {e["event_type"] for e in events} == {"movie_release"}

    def test_scope_tracking_excludes_movies(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2))
        _watchlist(db, u, _movie(db, release=_TODAY + timedelta(days=3)))
        db.session.commit()

        _login(client, u)
        events = _get(client, "?scope=tracking").get_json()["events"]
        assert {e["event_type"] for e in events} == {"episode"}

    def test_invalid_type_and_scope_rejected(self, db, client):
        u = _user(db)
        _login(client, u)
        assert _get(client, "?type=franchise").status_code == 400
        assert _get(client, "?scope=world").status_code == 400


# ─────────────────────────── 10A gap coverage ───────────────────────

class TestRangeValidation:
    def test_reversed_range_rejected(self, db, client):
        """10A §D: start <= end is a contract rule — a reversed window
        is a client bug and must be rejected, not silently swapped."""
        u = _user(db)
        _login(client, u)
        resp = _get(client, "?start=2026-10-31&end=2026-10-01")
        assert resp.status_code == 400
        assert "end" in resp.get_json()["error"].lower()

    def test_boundary_dates_inclusive_next_day_excluded(self, db, client):
        """§D/§20: inclusive externally — start and end dates appear,
        the day after end does not (half-open internally)."""
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, date(2026, 10, 1))   # == start
        _upcoming(db, s, 1, 2, date(2026, 10, 31))  # == end
        _upcoming(db, s, 1, 3, date(2026, 11, 1))   # end + 1 day
        db.session.commit()

        _login(client, u)
        events = _get(
            client, "?start=2026-10-01&end=2026-10-31").get_json()["events"]
        assert [e["episode_number"] for e in events] == [1, 2]


class TestResponseContract:
    def test_event_serialization_shape_pinned(self, db, client):
        """§E: stable, documented key-set — additive changes only."""
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2), name="Ep")
        db.session.commit()
        _login(client, u)
        ev = _get(client).get_json()["events"][0]
        assert set(ev.keys()) == {
            "id", "event_type", "media_type", "title", "poster", "date",
            "time", "tmdb_id", "season_number", "episode_number",
            "release_type", "status", "source", "is_tracked",
            "is_watchlisted", "watched", "detail_url", "metadata"}

    def test_no_external_tmdb_calls_in_request_path(self, db, client):
        """§H: calendar is read-only over local data — zero external
        TMDb calls, even with 20 events (10 TV + 10 watchlist movies)."""
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        for i in range(10):
            _upcoming(db, s, 1, i + 1, _TODAY + timedelta(days=i + 1))
            _watchlist(db, u, _movie(db, release=_TODAY + timedelta(days=i + 1)))
        db.session.commit()

        calls = []
        import api.tmdb.cache as tmdb_cache_mod
        orig = tmdb_cache_mod.cached_tmdb_request
        tmdb_cache_mod.cached_tmdb_request = (
            lambda *a, **k: calls.append(a))
        try:
            _login(client, u)
            data = _get(client).get_json()
        finally:
            tmdb_cache_mod.cached_tmdb_request = orig
        assert data["meta"]["counts"]["total"] == 20
        assert calls == []


# ─────────────────────────── security ───────────────────────────────

class TestSecurity:
    def test_no_user_id_parameter_bypass(self, db, client):
        u, other = _user(db), _user(db)
        s = _show(db)
        _track(db, other, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=3))
        db.session.commit()

        _login(client, u)
        for q in ("?user_id=%d" % other.id,
                  "?user=%d" % other.id,
                  "?uid=%d" % other.id):
            assert _get(client, q).get_json()["events"] == []

    def test_isolation_both_users_data_present(self, db, client):
        """§5/§6: scoping by absence-of-leak is weaker than by
        cross-visibility — prove each user sees exactly their own
        events when BOTH users have tracked episodes. Sequential
        logins on one client (parallel test clients proved flaky in
        this sandbox — the session identity leaked across clients)."""
        ua, ub = _user(db), _user(db)
        sa, sb = _show(db), _show(db)
        _track(db, ua, sa)
        _track(db, ub, sb)
        _upcoming(db, sa, 1, 1, _TODAY + timedelta(days=2), name="ForA")
        _upcoming(db, sb, 1, 1, _TODAY + timedelta(days=3), name="ForB")
        db.session.commit()

        _login(client, ua)
        ea = _get(client).get_json()["events"]
        assert [e["metadata"]["episode_name"] for e in ea] == ["ForA"]
        client.get("/logout")

        _login(client, ub)
        eb = _get(client).get_json()["events"]
        assert [e["metadata"]["episode_name"] for e in eb] == ["ForB"]

    def test_tv_upcoming_page_still_renders(self, db, client):
        """§F/§13: legacy /tv/upcoming regression — route registered,
        renders successfully for an authenticated user."""
        u = _user(db)
        _login(client, u)
        resp = client.get("/tv/upcoming")
        assert resp.status_code == 200

    def test_calendar_page_requires_auth(self, client):
        resp = client.get("/calendar")
        assert resp.status_code in (301, 302, 401)

    def test_legacy_tv_calendar_redirects_to_unified(self, db, client):
        u = _user(db)
        _login(client, u)
        resp = client.get("/tv/calendar")
        assert resp.status_code in (301, 302)
        assert "calendar" in resp.headers.get("Location", "")
        assert "type=tv" in resp.headers.get("Location", "")


# ─────────────────────────── performance ────────────────────────────

class _record_sql:
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


class TestPerformance:
    def test_query_budget_fixed_regardless_of_events(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        for i in range(20):
            _upcoming(db, s, 1, i + 1, _TODAY + timedelta(days=i % 25 + 1))
        for i in range(10):
            _watchlist(db, u, _movie(db, release=_TODAY + timedelta(days=5)))
        _watch(db, u, s, 1, 1)
        db.session.commit()

        _login(client, u)
        with _record_sql() as rec:
            data = _get(client, "?start=%s&end=%s" % (
                (_TODAY - timedelta(days=7)).isoformat(),
                (_TODAY + timedelta(days=30)).isoformat())).get_json()
        assert data["meta"]["counts"]["total"] == 30
        # fixed budget: shows + episodes + watched-keys + dated + unknown
        # (login adds a couple of statements — cap is generous but firm)
        assert len(rec.statements) < 15, len(rec.statements)
