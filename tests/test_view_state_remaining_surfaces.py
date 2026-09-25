"""Task C — view-state sync across the REMAINING surfaces.

For You, client-rendered browse/trending, CineBot cards, diary, and the
calendar now consume Task B's canonical state layer (api/user_view_state.py
+ static/js/view-state.js) instead of inventing new state:

- one batched private /api/view-state endpoint (page-scoped ids, no-store)
- calendar movie release events carry the canonical viewed flag
- /api/for-you stays a pure passthrough (ranking untouched)
- diary stays a historical event journal (no fabricated percentages)
- client surfaces (browse/trending/CineBot/For You) self-identify cards and
  merge state client-side so public/cached media responses stay public

TMDb calls are stubbed — no external services are required.
"""
from datetime import date, timedelta
from uuid import uuid4

import pytest

from models import (db, MediaItem, TVEpisodeWatch, TVShowProgress,
                    UpcomingEpisode)
from models.associations import user_viewed, user_watchlist
import api.user_view_state as uvs
import api.calendar as cal

MOVIE_A = 980101      # watched movie
MOVIE_B = 980102      # unwatched movie
SHOW_B = 980201       # 10 aired / 5 watched
SHOW_FUTURE = 980202  # fully watched + one future episode


def _details(last_episode, season=1):
    return {"id": 0,
            "last_episode_to_air": {"season_number": season,
                                    "episode_number": last_episode}}


@pytest.fixture
def factory(db):
    """Per-test builders with surgical teardown (shared test DB safe)."""
    users, media_ids, show_ids = [], [], []
    suffix = uuid4().hex[:8]

    def user(username="taskc"):
        from models import User
        u = User(username=f"{username}-{suffix}-{len(users)}",
                 email=f"{username}-{suffix}-{len(users)}@example.com",
                 email_verified=True)
        u.set_password("TaskCPass1")
        db.session.add(u)
        db.session.commit()
        users.append(u)
        return u

    def media(tmdb_id, media_type, title="T", release_date=None):
        m = MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title,
                      release_date=release_date)
        db.session.add(m)
        db.session.commit()
        media_ids.append(m.id)
        return m

    def view(u, m):
        db.session.execute(user_viewed.insert().values(
            user_id=u.id, media_id=m.id, media_type=m.media_type))
        db.session.commit()

    def watchlist(u, m):
        db.session.execute(user_watchlist.insert().values(
            user_id=u.id, media_id=m.id, media_type=m.media_type))
        db.session.commit()

    def watch(u, show_id, season, episode, rewatch=False):
        db.session.add(TVEpisodeWatch(
            user_id=u.id, show_id=show_id, season_number=season,
            episode_number=episode, is_rewatch=rewatch))
        db.session.commit()

    def track(u, show_id, status="watching"):
        db.session.add(TVShowProgress(user_id=u.id, show_id=show_id,
                                      status=status))
        db.session.commit()

    def upcoming(show_id, season, episode, delta):
        show_ids.append(show_id)
        db.session.add(UpcomingEpisode(
            show_id=show_id, show_name="S", season_number=season,
            episode_number=episode,
            air_date=date.today() + timedelta(days=delta)))
        db.session.commit()

    class _F:
        pass

    _F.user, _F.media, _F.view = user, media, view
    _F.watchlist, _F.watch, _F.track = watchlist, watch, track
    _F.upcoming = upcoming
    yield _F

    db.session.rollback()
    if users:
        TVEpisodeWatch.query.filter(
            TVEpisodeWatch.user_id.in_([u.id for u in users])
        ).delete(synchronize_session=False)
        TVShowProgress.query.filter(
            TVShowProgress.user_id.in_([u.id for u in users])
        ).delete(synchronize_session=False)
        from models.associations import user_watchlist as _uw
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.user_id.in_([u.id for u in users])))
        db.session.execute(_uw.delete().where(
            _uw.c.user_id.in_([u.id for u in users])))
        from models import User
        User.query.filter(
            User.id.in_([u.id for u in users])
        ).delete(synchronize_session=False)
    if media_ids:
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.media_id.in_(media_ids)))
        db.session.execute(user_watchlist.delete().where(
            user_watchlist.c.media_id.in_(media_ids)))
        MediaItem.query.filter(
            MediaItem.id.in_(media_ids)).delete(synchronize_session=False)
    if show_ids:
        UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(set(show_ids))
        ).delete(synchronize_session=False)
    db.session.commit()
    db.session.expire_all()


def _login(client, u):
    client.post("/login", data={"username": u.username,
                                "password": "TaskCPass1"},
                follow_redirects=True)


def _patch_details(monkeypatch, details_by_id):
    import api.continue_watching as cw
    monkeypatch.setattr(
        cw, "show_details",
        lambda sid, **kw: details_by_id.get(sid))
    cw._memo.clear()
    yield
    cw._memo.clear()


@pytest.fixture
def details_cached(monkeypatch):
    """Default details stub: every show has 10 aired episodes (S1E10 is
    the last_episode_to_air anchor). Tests needing other anchors pass an
    explicit details_loader instead."""
    yield from _patch_details(
        monkeypatch, {sid: _details(10) for sid in range(1, 999999)})


# Silence unused-import lint on the shared helper used by explicit tests.
_ = _details


# ── /api/view-state endpoint ────────────────────────────────────────────────

def test_view_state_endpoint_requires_login(client, factory):
    r = client.get("/api/view-state?movies=1&tv=2")
    assert r.status_code in (301, 302, 401)


def test_view_state_endpoint_batched_payload(client, factory,
                                             details_cached):
    u = factory.user()
    factory.view(u, factory.media(MOVIE_A, "movie", "Seen"))
    factory.media(MOVIE_B, "movie", "Unseen")
    for ep in range(1, 6):
        factory.watch(u, SHOW_B, 1, ep)
    _login(client, u)
    r = client.get(f"/api/view-state?movies={MOVIE_A},{MOVIE_B}&tv={SHOW_B}")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"
    data = r.get_json()
    assert data["viewed_movie_ids"] == [MOVIE_A]     # filtered to page ids
    # 5 watched of 10 aired (details anchor; no calendar rows here).
    # JSON keys are strings.
    assert data["tv_progress"][str(SHOW_B)] == {
        "watched": 5, "aired": 10, "percent": 50.0}


def test_view_state_endpoint_tv_ids_never_expand(factory):
    """Phase 17: only requested show ids are evaluated."""
    u = factory.user()
    for ep in range(1, 4):
        factory.watch(u, SHOW_B, 1, ep)
    payload = uvs.view_state_payload(
        u, [], [SHOW_B, SHOW_FUTURE],
        details_loader=lambda sid: _details(10))
    assert set(payload["tv_progress"]) == {SHOW_B}   # FUTURE not started
    assert payload["tv_progress"][SHOW_B]["percent"] == 30.0


def test_view_state_endpoint_malformed_ids_400(client, factory):
    u = factory.user()
    _login(client, u)
    r = client.get("/api/view-state?movies=abc")
    assert r.status_code == 400


def test_view_state_endpoint_query_budget_constant(client, factory,
                                                   details_cached):
    """Exactly 2 statements whether 1 or 50 shows are requested."""
    from sqlalchemy import event

    u = factory.user()
    ids = [SHOW_B + i for i in range(50)]
    for sid in ids:
        factory.watch(u, sid, 1, 1)

    def _count(needle, url):
        hits = []

        def _before(conn, cursor, statement, parameters, context,
                    executemany):
            if needle in statement:
                hits.append(statement)

        event.listen(db.engine, "before_cursor_execute", _before)
        try:
            client.get(url)
        finally:
            event.remove(db.engine, "before_cursor_execute", _before)
        return len(hits)

    _login(client, u)
    one_watch = _count("FROM tv_episode_watch",
                       f"/api/view-state?tv={SHOW_B}")
    many_watch = _count("FROM tv_episode_watch",
                        "/api/view-state?tv=" + ",".join(map(str, ids)))
    one_cal = _count("FROM upcoming_episode",
                     f"/api/view-state?tv={SHOW_B}")
    many_cal = _count("FROM upcoming_episode",
                      "/api/view-state?tv=" + ",".join(map(str, ids)))
    assert (one_watch, many_watch) == (1, 1)
    assert (one_cal, many_cal) == (1, 1)


def test_view_state_privacy_between_users(client, factory, details_cached):
    """User A's viewed state never leaks into user B's payload."""
    a = factory.user("alice")
    b = factory.user("bob")
    factory.view(a, factory.media(MOVIE_A, "movie", "A-only"))
    _login(client, b)
    r = client.get(f"/api/view-state?movies={MOVIE_A}")
    assert r.status_code == 200
    assert r.get_json()["viewed_movie_ids"] == []


# ── For You: pure passthrough (ranking untouched) ───────────────────────────

def test_for_you_endpoint_stays_pure_passthrough(auth_client, monkeypatch):
    """The route enriches NOTHING: items come back exactly as the engine
    produced them, the engine is called once with unchanged arguments, and
    no personalized state is merged server-side."""
    calls = []

    def fake_engine(user_id, region=None, limit=14):
        calls.append({"user_id": user_id, "region": region,
                      "limit": limit})
        return {
            "personalized": True, "mode": "full", "confidence": 0.9,
            "reason_state": "learned_taste",
            "items": [
                {"tmdb_id": MOVIE_A, "media_type": "movie",
                 "title": "Rec Movie", "poster_path": None,
                 "release_date": "2025-01-01", "source": "trending",
                 "reason": {"kind": "trending", "text": "Popular"}},
                {"tmdb_id": SHOW_B, "media_type": "tv",
                 "title": "Rec Show", "poster_path": None,
                 "release_date": "2025-01-01", "source": "trending",
                 "reason": {"kind": "trending", "text": "Popular"}},
            ],
        }

    monkeypatch.setattr("api.for_you.get_for_you", fake_engine)
    r = auth_client.get("/api/for-you")
    assert r.status_code == 200
    data = r.get_json()
    assert len(calls) == 1                              # engine called once
    assert calls[0]["limit"] == 14                      # unchanged default
    for item in data["items"]:                          # no server enrichment
        assert "is_viewed" not in item
        assert "tv_progress" not in item
        assert item["reason"] == {"kind": "trending", "text": "Popular"}
    assert data["items"][0]["tmdb_id"] == MOVIE_A       # order preserved
    assert data["items"][1]["tmdb_id"] == SHOW_B


def test_client_surfaces_invoke_sync():
    """Guard: every client-rendered surface merges state after render
    (including dynamic re-renders) through the ONE shared store."""
    def read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    assert "FrameIQViewState.syncCards" in read("static/js/for-you.js")
    assert "FrameIQViewState.syncCards" in read("static/js/movies-page.js")
    assert "FrameIQViewState.syncCards" in read("static/js/tv-shows.js")
    assert "FrameIQViewState.syncCards" in read("static/js/chat-page.js")
    assert "FrameIQViewState.syncCards" in read("templates/trending.html")
    # ...and the store owns the batched fetch (no per-card requests).
    store = read("static/js/view-state.js")
    assert store.count("fetch('/api/view-state") == 1


# ── Calendar ────────────────────────────────────────────────────────────────

def _in_window(delta):
    today = date.today()
    return today - timedelta(days=7) + timedelta(days=delta)


def test_calendar_movie_release_carries_viewed_flag(factory):
    """A release for a canonically-viewed watchlist title is flagged;
    release dates, labels and ordering are untouched."""
    u = factory.user()
    seen = factory.media(MOVIE_A, "movie", "Seen Movie",
                         release_date=date.today() + timedelta(days=5))
    unseen = factory.media(MOVIE_B, "movie", "Unseen Movie",
                           release_date=date.today() + timedelta(days=6))
    factory.watchlist(u, seen)
    factory.watchlist(u, unseen)
    factory.view(u, seen)

    start, end = cal.default_range()
    events, meta = cal.get_calendar_events(
        u.id, start, end, event_type="movie", scope="watchlist")
    flags = {e["tmdb_id"]: e["watched"] for e in events
             if e["event_type"] == "movie_release"}
    assert flags[MOVIE_A] is True
    assert flags[MOVIE_B] is False
    # release semantics unchanged
    by_id = {e["tmdb_id"]: e for e in events}
    assert by_id[MOVIE_A]["date"] == seen.release_date.isoformat()
    assert by_id[MOVIE_A]["event_type"] == "movie_release"
    assert by_id[MOVIE_A]["status"] == "upcoming"
    # ordering unchanged (deterministic sort key)
    keys = [(e["date"], e["event_type"], e["title"], e["id"])
            for e in events]
    assert keys == sorted(keys)


def test_calendar_episode_events_keep_watched_semantics(factory):
    """Existing TV watched flags still work through the shared builder —
    episode-level state, not a series percentage."""
    u = factory.user()
    factory.track(u, SHOW_B)
    factory.upcoming(SHOW_B, 1, 5, 1)     # airs tomorrow
    factory.upcoming(SHOW_B, 1, 6, 2)     # airs in 2 days
    factory.watch(u, SHOW_B, 1, 5)        # E5 already watched

    start, end = cal.default_range()
    events, _ = cal.get_calendar_events(
        u.id, start, end, event_type="tv", scope="tracking")
    flags = {(e["season_number"], e["episode_number"]): e["watched"]
             for e in events if e["event_type"] == "episode"}
    assert flags[(1, 5)] is True
    assert flags[(1, 6)] is False
    for e in events:
        assert "percent" not in e          # no fabricated series progress


def test_calendar_newly_aired_episode_reflects_in_progress(factory):
    """Phase 14: same canonical denominator as Task B — a newly aired
    episode lowers overall progress until watched (10/11 = 90.9%)."""
    u = factory.user()
    factory.track(u, SHOW_B)
    for ep in range(1, 11):
        factory.watch(u, SHOW_B, 1, ep)
        factory.upcoming(SHOW_B, 1, ep, -1)
    factory.upcoming(SHOW_B, 1, 11, 0)    # airs today (aired)

    payload = uvs.view_state_payload(
        u, [], [SHOW_B],
        details_loader=lambda sid: _details(11))
    assert payload["tv_progress"][SHOW_B] == {"watched": 10, "aired": 11,
                                              "percent": 90.9}
    # ...and the calendar marks E11 unwatched while it airs.
    start, end = cal.default_range()
    events, _ = cal.get_calendar_events(
        u.id, start, end, event_type="tv", scope="tracking")
    e11 = [e for e in events if e["episode_number"] == 11]
    assert e11 and e11[0]["watched"] is False


def test_calendar_future_episode_never_in_denominator(factory):
    u = factory.user()
    for ep in range(1, 11):
        factory.watch(u, SHOW_FUTURE, 1, ep)
    factory.upcoming(SHOW_FUTURE, 1, 11, 5)   # airs in 5 days (future)
    payload = uvs.view_state_payload(
        u, [], [SHOW_FUTURE],
        details_loader=lambda sid: _details(10))
    assert payload["tv_progress"][SHOW_FUTURE]["percent"] == 100.0


# ── Diary: historical integrity (no fabricated state, no dup records) ──────

def test_diary_movie_log_creates_no_duplicate_records(app, client,
                                                      factory):
    """Quick-log a movie → ONE diary event, canonical viewed row, and
    zero TVEpisodeWatch rows (the movie/TV split is untouched)."""
    from flask_login import FlaskLoginClient
    app.test_client_class = FlaskLoginClient
    u = factory.user("diarylogger")
    with app.test_client(user=u) as lc:
        r = lc.post(f"/api/media/{MOVIE_A}/log", json={
            "title": "Diary Movie", "poster_path": None})
        assert r.status_code in (200, 201)

        stream = lc.get("/api/diary").get_json()
        movie_events = [e for e in stream["entries"]
                        if e.get("media", {}).get("title")
                        == "Diary Movie"]
        assert len(movie_events) == 1             # one historical event
        assert "percent" not in movie_events[0]   # no fabricated state
        assert movie_events[0].get("episode") is None

        # The canonical viewed state now backs the card surfaces.
        payload = uvs.view_state_payload(u, [MOVIE_A], [])
        assert payload["viewed_movie_ids"] == [MOVIE_A]

        # Zero TV episode records were created by a movie log.
        assert TVEpisodeWatch.query.filter_by(
            user_id=u.id).count() == 0

    app.test_client_class = None
