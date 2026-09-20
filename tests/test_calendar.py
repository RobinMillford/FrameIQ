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
from datetime import date, datetime, timedelta
from itertools import count

import pytest

from models import (MediaItem, MovieReleaseDate, TVShowProgress,
                    UpcomingEpisode)
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
    m = _media(db, media_type="movie", title=title, release=release)
    return m


def _show(db, title=None):
    m = _media(db, media_type="tv", title=title)
    return m


def _media(db, media_type, title=None, release=None):
    """Create one MediaItem with a strictly increasing tmdb_id (module
    counter — never wasted on a discarded row). Monotonicity matters:
    the calendar hydrates watchlist movies ordered by tmdb_id, and the
    shared session DB persists rows across the whole suite."""
    m = MediaItem(tmdb_id=next(_TMDB), media_type=media_type,
                  title=title or f"{media_type.title()} {uuid.uuid4().hex[:8]}",
                  release_date=release)
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
        # fixed budget: shows + episodes + watched-keys + watchlist
        # hydration + release cache (login adds a couple of statements —
        # cap is generous but firm)
        assert len(rec.statements) < 15, len(rec.statements)


# ═════════════════════════ Feature 10B ══════════════════════════════
# Movie release data + watchlist synchronization: per-region cache
# (tmdb_id, region, release_type) keyed, refreshed OFF the request path
# by a bounded sync; events carry deterministic (tmdb_id, date, type)
# ids. Legacy MediaItem.release_date remains the fallback for titles
# the release sync has not covered yet.


def _release(db, tmdb_id, rtype, rdate, region="US"):
    row = MovieReleaseDate(tmdb_id=tmdb_id, region=region,
                           release_type=rtype, release_date=rdate)
    db.session.add(row)
    db.session.flush()
    return row


def _movie_events(events):
    return [e for e in events if e["event_type"] == "movie_release"]


class TestMovieReleaseEvents:
    """Route-level: cache-backed watchlist release events (§P 1-20)."""

    def test_watchlisted_movie_produces_one_event(self, db, client):
        u = _user(db)
        m = _movie(db, title="Cached Movie", release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=6))
        db.session.commit()

        _login(client, u)
        evs = _movie_events(_get(client).get_json()["events"])
        assert len(evs) == 1
        ev = evs[0]
        assert ev["event_type"] == "movie_release"
        assert ev["media_type"] == "movie"
        assert ev["title"] == "Cached Movie"
        assert ev["tmdb_id"] == m.tmdb_id
        assert ev["date"] == (_TODAY + timedelta(days=6)).isoformat()
        assert ev["release_type"] == "theatrical"
        assert ev["is_watchlisted"] is True
        assert ev["is_tracked"] is False
        assert ev["watched"] is False
        assert ev["detail_url"] == "/movie/%d" % m.tmdb_id
        assert ev["status"] == "upcoming"
        assert ev["time"] is None

    def test_non_watchlisted_release_never_appears(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)            # exists, NOT watchlisted
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=4))
        db.session.commit()

        _login(client, u)
        assert _get(client).get_json()["events"] == []

    def test_user_isolation_watchlist_events(self, db, client):
        ua, ub = _user(db), _user(db)
        m = _movie(db, title="OnlyA", release=None)
        _watchlist(db, ua, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=5))
        db.session.commit()

        _login(client, ua)
        assert len(_movie_events(_get(client).get_json()["events"])) == 1
        client.get("/logout")
        _login(client, ub)
        assert _movie_events(_get(client).get_json()["events"]) == []

    def _seed_mixed(self, db, u):
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2))
        m = _movie(db, title="Mix", release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=8))
        db.session.commit()
        return m

    def test_type_movie_returns_movies_only(self, db, client):
        u = _user(db)
        self._seed_mixed(db, u)
        _login(client, u)
        evs = _get(client, "?type=movie").get_json()["events"]
        assert evs and all(e["event_type"] == "movie_release" for e in evs)

    def test_type_tv_excludes_movies(self, db, client):
        u = _user(db)
        self._seed_mixed(db, u)
        _login(client, u)
        evs = _get(client, "?type=tv").get_json()["events"]
        assert evs and all(e["event_type"] == "episode" for e in evs)

    def test_type_all_merges_tv_and_movies(self, db, client):
        u = _user(db)
        self._seed_mixed(db, u)
        _login(client, u)
        evs = _get(client, "?type=all").get_json()["events"]
        kinds = {e["event_type"] for e in evs}
        assert kinds == {"episode", "movie_release"}
        assert _get(client).get_json()["meta"]["counts"]["total"] == 2

    def test_scope_contract_preserved(self, db, client):
        u = _user(db)
        self._seed_mixed(db, u)
        _login(client, u)
        wl = _get(client, "?scope=watchlist").get_json()["events"]
        assert wl and all(e["event_type"] == "movie_release" for e in wl)
        tr = _get(client, "?scope=tracking").get_json()["events"]
        assert tr and all(e["event_type"] == "episode" for e in tr)

    def test_repeated_requests_are_duplicate_free(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=6))
        _release(db, m.tmdb_id, 4, _TODAY + timedelta(days=6))
        db.session.commit()

        _login(client, u)
        ids1 = [e["id"] for e in _get(client).get_json()["events"]]
        ids2 = [e["id"] for e in _get(client).get_json()["events"]]
        assert ids1 == ids2
        assert len(ids1) == 2            # theatrical + digital, same date
        assert len(set(ids1)) == 2       # deterministic distinct ids

    def test_distinct_release_types_distinct_events(self, db, client):
        """§I: different types on one date stay separate; identical
        (date, type) pairs collapse to one event."""
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        same = _TODAY + timedelta(days=6)
        _release(db, m.tmdb_id, 3, same)
        _release(db, m.tmdb_id, 4, same)
        db.session.commit()

        _login(client, u)
        evs = _movie_events(_get(client).get_json()["events"])
        assert {e["release_type"] for e in evs} == {"theatrical", "digital"}
        assert len({e["id"] for e in evs}) == 2

    def test_unknown_release_type_label_is_never_fabricated(self, db):
        """§C: an unmapped TMDb type integer labels as 'unknown', never
        as a category the source does not support."""
        import api.calendar as calendar_svc

        ev = calendar_svc._movie_release_event(
            123, "T", None, _TODAY, 99, _TODAY)
        assert ev["release_type"] == "unknown"
        assert ev["id"] == "movie-123-%s-t99" % _TODAY.isoformat()

    def test_unknown_release_date_bucket_and_resolution(self, db, client):
        """No cache row + no legacy date → release_date_unknown bucket;
        once ANY cache coverage exists the bucket empties."""
        u = _user(db)
        m = _movie(db, title="Dateless", release=None)
        _watchlist(db, u, m)
        db.session.commit()

        _login(client, u)
        data = _get(client).get_json()
        assert data["meta"]["release_date_unknown"]["count"] == 1
        assert data["meta"]["release_date_unknown"]["titles"] == ["Dateless"]
        assert _movie_events(data["events"]) == []

        # Coverage of ANY type (even a past, non-event row) clears the
        # unknown bucket — the sync has spoken for this title.
        _release(db, m.tmdb_id, 1, _TODAY - timedelta(days=30))
        db.session.commit()
        data = _get(client).get_json()
        assert data["meta"]["release_date_unknown"]["count"] == 0

    def test_range_boundaries_inclusive_next_day_excluded(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        start = _TODAY + timedelta(days=1)
        end = _TODAY + timedelta(days=10)
        _release(db, m.tmdb_id, 3, start)
        _release(db, m.tmdb_id, 4, end)
        _release(db, m.tmdb_id, 5, end + timedelta(days=1))
        db.session.commit()

        _login(client, u)
        q = "?start=%s&end=%s" % (start.isoformat(), end.isoformat())
        dates = {e["date"] for e in _get(client, q).get_json()["events"]}
        assert dates == {start.isoformat(), end.isoformat()}

    def test_released_past_movie_still_listed(self, db, client):
        """§P 29: watched/released state never removes a release event."""
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY - timedelta(days=2))
        db.session.commit()

        _login(client, u)
        evs = _movie_events(_get(client).get_json()["events"])
        assert len(evs) == 1
        assert evs[0]["status"] == "released"

    def test_legacy_fallback_when_sync_never_ran(self, db, client):
        """§P 30 / 10A compatibility: titles with only the add-time
        MediaItem.release_date keep producing 10A-shaped events."""
        u = _user(db)
        legacy = _TODAY + timedelta(days=12)
        m = _movie(db, release=legacy)
        _watchlist(db, u, m)
        db.session.commit()

        _login(client, u)
        evs = _movie_events(_get(client).get_json()["events"])
        assert len(evs) == 1
        assert evs[0]["id"] == "movie-%d" % m.tmdb_id
        assert evs[0]["release_type"] == "theatrical"
        assert evs[0]["date"] == legacy.isoformat()

    def test_legacy_fallback_dropped_once_cache_covers(self, db, client):
        """The release cache is strictly fresher than the add-time date:
        once ANY cache row exists for the title, the legacy date is no
        longer surfaced (stale add-time data must not resurface)."""
        u = _user(db)
        m = _movie(db, release=_TODAY + timedelta(days=12))
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 1, _TODAY - timedelta(days=30))
        db.session.commit()

        _login(client, u)
        assert _movie_events(_get(client).get_json()["events"]) == []

    def test_regions_resolve_independently(self, db, client):
        """§D: rows are per-region; another region's date never leaks."""
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        d = _TODAY + timedelta(days=6)
        _release(db, m.tmdb_id, 3, d, region="US")
        _release(db, m.tmdb_id, 3, d + timedelta(days=20), region="GB")
        db.session.commit()

        _login(client, u)                 # no saved region → default US
        dates = {e["date"] for e in _movie_events(
            _get(client).get_json()["events"])}
        assert dates == {d.isoformat()}

    def test_movie_event_key_set_pinned_no_internal_pk(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=6))
        db.session.commit()

        _login(client, u)
        for ev in _movie_events(_get(client).get_json()["events"]):
            assert set(ev.keys()) == {
                "id", "event_type", "media_type", "title", "poster",
                "date", "time", "tmdb_id", "season_number",
                "episode_number", "release_type", "status", "source",
                "is_tracked", "is_watchlisted", "watched", "detail_url",
                "metadata"}
            assert isinstance(ev["id"], str)
            assert ev["id"].startswith("movie-")

    def test_envelope_preserved(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=6))
        db.session.commit()

        _login(client, u)
        meta = _get(client).get_json()["meta"]
        assert set(meta.keys()) == {
            "start_date", "end_date", "today", "counts",
            "release_date_unknown", "range_capped", "max_range_days"}
        assert meta["counts"]["movie"] == 1
        assert meta["max_range_days"] == 62

    def test_deterministic_sorting_tv_then_movie_same_date(self, db, client):
        u = _user(db)
        s = _show(db, title="Zeta Show")
        _track(db, u, s)
        same = _TODAY + timedelta(days=3)
        _upcoming(db, s, 1, 1, same)
        m = _movie(db, title="Alpha Movie", release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, same)
        db.session.commit()

        _login(client, u)
        evs = _get(client).get_json()["events"]
        assert [e["event_type"] for e in evs] == ["episode", "movie_release"]


class TestReleaseSyncService:
    """Service-level: bounded, deterministic, failure-isolated,
    idempotent (§P 8/17/22/25/26/28).

    The candidate selection is deliberately global (release rows are
    title-level shared cache), and the shared session DB holds
    watchlisted titles from earlier tests — so every assertion here
    pins per-title outcomes and structural invariants (bounds, order,
    isolation), never exact global counts."""

    def _run(self, data=None, error_for=None, max_titles=150):
        import api.watchlist_release_sync as sync_mod
        calls = []

        def fake_fetch(tmdb_id):
            calls.append(tmdb_id)
            if error_for is not None and tmdb_id == error_for:
                raise LookupError("boom")
            return list(data or [])

        orig = sync_mod.fetch_movie_release_dates
        sync_mod.fetch_movie_release_dates = fake_fetch
        try:
            result = sync_mod.sync_watchlist_release_data(
                max_titles=max_titles)
        finally:
            sync_mod.fetch_movie_release_dates = orig
        return result, calls

    def test_sync_upserts_and_is_idempotent(self, db):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        d1 = _TODAY + timedelta(days=9)
        d2 = _TODAY + timedelta(days=40)
        db.session.commit()

        r1, calls = self._run([("US", 3, d1), ("US", 4, d2), ("GB", 3, d1)])
        assert r1["failed"] == 0 and r1["succeeded"] >= 1
        assert m.tmdb_id in calls
        db.session.commit()
        rows = MovieReleaseDate.query.filter_by(tmdb_id=m.tmdb_id).all()
        assert {(r.region, r.release_type, r.release_date)
                for r in rows} == {
            ("US", 3, d1), ("US", 4, d2), ("GB", 3, d1)}

        # Second run: this title's rows are fresh → never re-fetched,
        # no duplicates. (Other, older candidates may be selected —
        # only THIS title's behavior is asserted.)
        r2, calls2 = self._run([("US", 3, d1), ("US", 4, d2), ("GB", 3, d1)])
        assert m.tmdb_id not in calls2
        assert MovieReleaseDate.query.filter_by(
            tmdb_id=m.tmdb_id).count() == 3

    def test_stale_titles_are_refreshed(self, db):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=3))
        MovieReleaseDate.query.filter_by(tmdb_id=m.tmdb_id).update(
            {MovieReleaseDate.fetched_at:
             datetime.utcnow() - timedelta(days=10)})
        db.session.commit()

        result, calls = self._run([("US", 3, _TODAY + timedelta(days=3))])
        assert m.tmdb_id in calls       # stale → re-selected for refresh
        assert result["failed"] == 0
        db.session.commit()
        fresh = MovieReleaseDate.query.filter_by(
            tmdb_id=m.tmdb_id).first().fetched_at
        assert (datetime.utcnow() - fresh).days < 1

    def test_one_failed_title_does_not_abort_batch(self, db):
        u = _user(db)
        ma = _movie(db, title="Fails", release=None)
        mb = _movie(db, title="Works", release=None)
        _watchlist(db, u, ma)
        _watchlist(db, u, mb)
        db.session.commit()

        result, calls = self._run(
            [("US", 3, _TODAY + timedelta(days=5))],
            error_for=ma.tmdb_id)
        assert result["failed"] >= 1
        assert ma.tmdb_id in calls and mb.tmdb_id in calls
        db.session.commit()
        # The failed title wrote nothing; the healthy one synced.
        assert MovieReleaseDate.query.filter_by(
            tmdb_id=ma.tmdb_id).count() == 0
        assert MovieReleaseDate.query.filter_by(
            tmdb_id=mb.tmdb_id).count() == 1

    def test_sync_hard_upper_bound_and_deterministic_selection(self, db):
        u = _user(db)
        for _ in range(5):
            m = _movie(db, release=None)
            _watchlist(db, u, m)
        db.session.commit()

        result, calls = self._run(
            [("US", 3, _TODAY + timedelta(days=5))], max_titles=2)
        # §P 25/26: the hard cap holds no matter how large the watchlist.
        assert result["selected"] == 2
        assert len(calls) == 2
        # §E: deterministic order — candidates are processed in ascending
        # tmdb_id (never database row order).
        assert calls == sorted(calls)

    def test_sync_never_touches_non_watchlisted_titles(self, db):
        _user(db)
        m = _movie(db, release=None)      # exists, NOT watchlisted
        db.session.commit()

        result, calls = self._run()
        # §G: candidacy comes from user_watchlist only — a title no user
        # watchlisted is never selected, whatever else runs.
        assert m.tmdb_id not in calls
        db.session.commit()
        assert MovieReleaseDate.query.filter_by(
            tmdb_id=m.tmdb_id).count() == 0

    def test_sync_uses_canonical_cached_helper_once_per_title(self, db):
        u = _user(db)
        m1 = _movie(db, release=None)
        m2 = _movie(db, release=None)
        _watchlist(db, u, m1)
        _watchlist(db, u, m2)
        db.session.commit()

        import api.tmdb.movies as movies_mod
        import api.watchlist_release_sync as sync_mod
        helper_calls = []

        def fake_helper(url, **kwargs):
            helper_calls.append(url)
            return {"results": [{
                "iso_3166_1": "US",
                "release_dates": [
                    {"release_date": "2030-01-01", "type": 3},
                    {"release_date": "", "type": 4},        # no date
                    {"release_date": "garbage", "type": 4},  # malformed
                ]}]}

        orig = movies_mod.cached_tmdb_request
        movies_mod.cached_tmdb_request = fake_helper
        try:
            result = sync_mod.sync_watchlist_release_data()
        finally:
            movies_mod.cached_tmdb_request = orig
        assert result["failed"] == 0
        # §P 22/26: the canonical helper, exactly once per title —
        # regardless of how many regions/types one response carries.
        for tmdb_id in (m1.tmdb_id, m2.tmdb_id):
            matching = [c for c in helper_calls
                        if "/movie/%d/release_dates" % tmdb_id in c]
            assert len(matching) == 1
        db.session.commit()
        # Only parseable dates persisted; empty/malformed entries were
        # skipped, never invented.
        rows = MovieReleaseDate.query.filter(
            MovieReleaseDate.tmdb_id.in_([m1.tmdb_id, m2.tmdb_id])).all()
        assert len(rows) == 2
        for row in rows:
            assert row.release_date == date(2030, 1, 1)
            assert row.region == "US"
            assert row.release_type == 3

    def test_tmdb_failure_leaves_cached_calendar_intact(self, db, client):
        """§P 24: sync-time TMDb failures never corrupt an existing
        cache — the calendar keeps serving what it already has."""
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=6))
        MovieReleaseDate.query.filter_by(tmdb_id=m.tmdb_id).update(
            {MovieReleaseDate.fetched_at:
             datetime.utcnow() - timedelta(days=10)})
        db.session.commit()

        result, _ = self._run(error_for=m.tmdb_id)   # fetch blows up
        assert result["failed"] >= 1
        db.session.commit()

        _login(client, u)     # stale row remains valid for the calendar
        evs = _movie_events(_get(client).get_json()["events"])
        assert len(evs) == 1


class TestReadPathTMDbBounds:
    """§F: calendar reads stay zero-TMDb and never trigger sync."""

    def test_calendar_read_makes_zero_tmdb_calls_with_cache(self, db, client):
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=1))
        for _ in range(5):
            m = _movie(db, release=None)
            _watchlist(db, u, m)
            _release(db, m.tmdb_id, 3, _TODAY + timedelta(days=6))
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
        assert data["meta"]["counts"]["total"] == 6
        assert calls == []

    def test_calendar_read_never_triggers_sync(self, db, client):
        u = _user(db)
        m = _movie(db, release=None)
        _watchlist(db, u, m)
        db.session.commit()

        import api.watchlist_release_sync as sync_mod

        def bomb(*a, **k):
            raise AssertionError("sync ran on the request path")

        orig = sync_mod.sync_watchlist_release_data
        sync_mod.sync_watchlist_release_data = bomb
        try:
            _login(client, u)
            assert _get(client).status_code == 200
        finally:
            sync_mod.sync_watchlist_release_data = orig

    def test_query_budget_with_release_cache(self, db, client):
        """§L: 10 movies × 3 cache rows + TV — statement count stays
        fixed, never proportional to rows."""
        u = _user(db)
        s = _show(db)
        _track(db, u, s)
        for _ in range(10):
            m = _movie(db, release=None)
            _watchlist(db, u, m)
            d = _TODAY + timedelta(days=6)
            _release(db, m.tmdb_id, 3, d)
            _release(db, m.tmdb_id, 4, d)
            _release(db, m.tmdb_id, 5, d)
        _upcoming(db, s, 1, 1, _TODAY + timedelta(days=2))
        db.session.commit()

        _login(client, u)
        with _record_sql() as rec:
            data = _get(client, "?start=%s&end=%s" % (
                (_TODAY - timedelta(days=7)).isoformat(),
                (_TODAY + timedelta(days=30)).isoformat())).get_json()
        assert data["meta"]["counts"]["movie"] == 20   # 2 event types × 10
        assert data["meta"]["counts"]["total"] == 21
        assert len(rec.statements) < 15, len(rec.statements)
