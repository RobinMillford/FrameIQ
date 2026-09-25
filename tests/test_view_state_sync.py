"""View-state sync tests (Task B: single shared user viewing contract).

api/user_view_state.py is the ONE source of personalized media state:
movie "Viewed" badges come from the user_viewed junction (never a second
"watched" definition) and TV cards show the user's aired-episode progress

    watched unique non-rewatch aired episodes / total aired non-special

recomputed per request with future episodes never in the denominator.

These tests pin: the canonical semantics, the percent matrix, user scoping
(privacy), the two-statement query budget (no per-card N+1), and the
end-to-end render on the homepage rails/hero, the TV detail hero, and the
/api/tv/<id>/aired-progress endpoint the client store refreshes from.

TMDb calls are stubbed — no external services are required.
"""
from datetime import date, timedelta
from uuid import uuid4

import pytest

from models import db, MediaItem, TVEpisodeWatch, UpcomingEpisode
from models.associations import user_viewed
import api.user_view_state as uvs

# TMDb ids deliberately outside any fixture range other tests use.
MOVIE_ID = 999501
SHOW_ID = 990001


def _loader(details_by_id):
    """Stub details_loader: {show_id: details-or-None}; raise on marker."""

    def _load(show_id):
        details = details_by_id.get(show_id, "missing")
        if details == "raise":
            raise RuntimeError("tmdb down")
        if details == "missing":
            return None
        return details

    return _load


def _details(last_episode, season=1):
    if last_episode is None:
        return None
    return {
        "id": SHOW_ID,
        "name": "S",
        "last_episode_to_air": {"season_number": season,
                                "episode_number": last_episode},
    }


@pytest.fixture
def factory(db):
    """Per-test builders with surgical teardown (shared test DB safe)."""
    users, media_ids, show_ids = [], [], []
    # Unique per-fixture suffix: a leftover user from a crashed teardown
    # must never collide with the next test's INSERT (shared DB).
    suffix = uuid4().hex[:8]

    def user(username="viewsync"):
        from models import User
        u = User(username=f"{username}-{suffix}-{len(users)}",
                 email=f"{username}-{suffix}-{len(users)}@example.com",
                 email_verified=True)
        u.set_password("ViewSync1")
        db.session.add(u)
        db.session.commit()
        users.append(u)
        return u

    def media(tmdb_id, media_type, title="T"):
        m = MediaItem(tmdb_id=tmdb_id, media_type=media_type, title=title)
        db.session.add(m)
        db.session.commit()
        media_ids.append(m.id)
        return m

    def view(u, m):
        db.session.execute(user_viewed.insert().values(
            user_id=u.id, media_id=m.id, media_type=m.media_type))
        db.session.commit()

    def watch(u, show_id, season, episode, rewatch=False):
        db.session.add(TVEpisodeWatch(
            user_id=u.id, show_id=show_id, season_number=season,
            episode_number=episode, is_rewatch=rewatch))
        db.session.commit()

    def _cal(show_id, season, episode, delta):
        show_ids.append(show_id)
        db.session.add(UpcomingEpisode(
            show_id=show_id, show_name="S", season_number=season,
            episode_number=episode,
            air_date=date.today() + timedelta(days=delta)))
        db.session.commit()

    class _F:
        pass

    _F.user, _F.media, _F.view, _F.watch = user, media, view, watch
    _F.aired = lambda s, se, ep: _cal(s, se, ep, -1)
    _F.future = lambda s, se, ep: _cal(s, se, ep, 3)
    yield _F

    # Teardown — pure Core/Query deletes, no ORM cascade: the ORM path
    # (session.delete(user)) re-deletes user_viewed rows already removed
    # below and raises StaleDataError on the shared session.
    db.session.rollback()  # clear any poisoned transaction from the test
    if users:
        TVEpisodeWatch.query.filter(
            TVEpisodeWatch.user_id.in_([u.id for u in users])
        ).delete(synchronize_session=False)
        from models.associations import user_watchlist
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.user_id.in_([u.id for u in users])))
        db.session.execute(user_watchlist.delete().where(
            user_watchlist.c.user_id.in_([u.id for u in users])))
        from models import User
        User.query.filter(
            User.id.in_([u.id for u in users])
        ).delete(synchronize_session=False)
    if media_ids:
        db.session.execute(user_viewed.delete().where(
            user_viewed.c.media_id.in_(media_ids)))
        MediaItem.query.filter(
            MediaItem.id.in_(media_ids)).delete(synchronize_session=False)
    if show_ids:
        UpcomingEpisode.query.filter(
            UpcomingEpisode.show_id.in_(set(show_ids))
        ).delete(synchronize_session=False)
    db.session.commit()
    db.session.expire_all()


# ── Movie viewed contract ────────────────────────────────────────────────────

def test_viewed_keys_anonymous_empty(factory):
    assert uvs.user_viewed_keys(None) == set()


def test_viewed_keys_reflect_canonical_junction(factory):
    u = factory.user()
    assert uvs.user_viewed_keys(u) == set()
    m = factory.media(MOVIE_ID, "movie", "The Matrix")
    factory.view(u, m)
    assert uvs.user_viewed_keys(u) == {(MOVIE_ID, "movie")}


# ── TV aired-progress matrix ────────────────────────────────────────────────

def test_tv_progress_anonymous_empty(factory):
    assert uvs.tv_aired_progress(None, [SHOW_ID]) == {}


def test_tv_progress_unwatched_show_has_no_entry(factory):
    """Entries exist only for started shows — unwatched means no badge."""
    u = factory.user()
    assert uvs.tv_aired_progress(
        u, [SHOW_ID], _loader({SHOW_ID: _details(5)})) == {}


def test_tv_progress_nothing_aired_yet_has_no_entry(factory):
    u = factory.user()
    factory.watch(u, SHOW_ID, 1, 1)
    assert uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: None})) == {}


def test_tv_progress_partial(factory):
    u = factory.user()
    for ep in range(1, 2):
        factory.watch(u, SHOW_ID, 1, ep)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(5)}))
    assert p[SHOW_ID] == {"watched": 1, "aired": 5, "percent": 20.0}


def test_tv_progress_full(factory):
    u = factory.user()
    for ep in range(1, 6):
        factory.watch(u, SHOW_ID, 1, ep)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(5)}))
    assert p[SHOW_ID]["percent"] == 100.0


def test_tv_progress_calendar_extends_details_denominator(factory):
    """Watched 1-5; calendar says 6-10 also aired; details anchor E10."""
    u = factory.user()
    for ep in range(1, 6):
        factory.watch(u, SHOW_ID, 1, ep)
        factory.aired(SHOW_ID, 1, ep + 5)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(10)}))
    assert p[SHOW_ID] == {"watched": 5, "aired": 10, "percent": 50.0}


def test_tv_progress_future_episode_never_in_denominator(factory):
    """E11 is dated in the future and absent from details — stays out."""
    u = factory.user()
    for ep in range(1, 11):
        factory.watch(u, SHOW_ID, 1, ep)
    factory.future(SHOW_ID, 1, 11)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(10)}))
    assert p[SHOW_ID]["percent"] == 100.0


def test_tv_progress_new_air_drops_percent(factory):
    """Running show: 10/10 becomes 10/11 the moment E11 airs."""
    u = factory.user()
    for ep in range(1, 11):
        factory.watch(u, SHOW_ID, 1, ep)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(11)}))
    assert p[SHOW_ID] == {"watched": 10, "aired": 11, "percent": 90.9}


def test_tv_progress_rewatch_no_inflation(factory):
    u = factory.user()
    factory.watch(u, SHOW_ID, 1, 1)
    factory.watch(u, SHOW_ID, 1, 1, rewatch=True)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(5)}))
    assert p[SHOW_ID]["watched"] == 1


def test_tv_progress_duplicate_rows_collapse(factory):
    u = factory.user()
    factory.watch(u, SHOW_ID, 1, 1)
    factory.watch(u, SHOW_ID, 1, 1)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(5)}))
    assert p[SHOW_ID]["watched"] == 1


def test_tv_progress_specials_excluded_both_sides(factory):
    """Season 0 never counts, even when watched and even when aired."""
    u = factory.user()
    factory.watch(u, SHOW_ID, 0, 1)
    factory.watch(u, SHOW_ID, 1, 1)
    factory.aired(SHOW_ID, 0, 2)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: _details(5)}))
    assert p[SHOW_ID] == {"watched": 1, "aired": 5, "percent": 20.0}


def test_tv_progress_cross_season(factory):
    """S1 fully aired (calendar), S2 started; denominator spans both."""
    u = factory.user()
    for ep in range(1, 6):
        factory.watch(u, SHOW_ID, 1, ep)
        factory.aired(SHOW_ID, 1, ep)          # all of S1 aired
        factory.aired(SHOW_ID, 1, ep + 5)      # aired, never watched
    for ep in range(1, 4):
        factory.watch(u, SHOW_ID, 2, ep)
    p = uvs.tv_aired_progress(u, [SHOW_ID],
                              _loader({SHOW_ID: _details(3, season=2)}))
    assert p[SHOW_ID] == {"watched": 8, "aired": 13, "percent": 61.5}


def test_tv_progress_details_failure_falls_back_to_calendar(factory):
    u = factory.user()
    for ep in range(1, 6):
        factory.watch(u, SHOW_ID, 1, ep)
        factory.aired(SHOW_ID, 1, ep)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: "raise"}))
    assert p[SHOW_ID]["percent"] == 100.0


def test_tv_progress_details_none_falls_back_to_calendar(factory):
    u = factory.user()
    factory.watch(u, SHOW_ID, 1, 1)
    factory.aired(SHOW_ID, 1, 1)
    factory.aired(SHOW_ID, 1, 2)
    p = uvs.tv_aired_progress(u, [SHOW_ID], _loader({SHOW_ID: "missing"}))
    assert p[SHOW_ID] == {"watched": 1, "aired": 2, "percent": 50.0}


# ── Query budget (no per-card N+1) ──────────────────────────────────────────

def _count_sql(needle, fn):
    from sqlalchemy import event
    hits = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if needle in statement:
            hits.append(statement)

    event.listen(db.engine, "before_cursor_execute", _before)
    try:
        result = fn()
    finally:
        event.remove(db.engine, "before_cursor_execute", _before)
    return result, hits


def test_tv_progress_query_budget_constant(factory):
    """Exactly two statements whether 1 or 20 shows are on the page."""

    def run(tag, show_ids):
        u = factory.user(f"vbudget-{tag}")
        for sid in show_ids:
            factory.watch(u, sid, 1, 1)
        loader = _loader({sid: _details(3) for sid in show_ids})
        _, hits = _count_sql(
            "FROM tv_episode_watch",
            lambda: uvs.tv_aired_progress(u, show_ids, loader))
        _, hits_cal = _count_sql(
            "FROM upcoming_episode",
            lambda: uvs.tv_aired_progress(u, show_ids, loader))
        return len(hits), len(hits_cal)

    one = run("one", [SHOW_ID])
    many = run("many", list(range(990101, 990121)))
    assert one == (1, 1)
    assert many == (1, 1)


# ── Homepage surfaces ───────────────────────────────────────────────────────

TRENDING = [{"id": MOVIE_ID + i, "title": f"Film {i}",
             "poster_path": f"/f{i}.jpg", "backdrop_path": f"/b{i}.jpg",
             "overview": "o", "release_date": "2025-01-01",
             "vote_average": 7.0, "genre_ids": [18]} for i in range(3)]

TV_RAIL_IDS = [SHOW_ID + i for i in range(6)]


@pytest.fixture
def homepage_tmdb(monkeypatch):
    """Stub every TMDb fetcher the homepage touches, plus the shared
    details cache used by tv_aired_progress."""
    import routes.browse as browse
    monkeypatch.setattr(browse, "fetch_now_playing_movies", lambda *a, **k: [])
    monkeypatch.setattr(browse, "fetch_popular_movies", lambda *a, **k: [])
    monkeypatch.setattr(browse, "fetch_upcoming_movies",
                        lambda *a, **k: [])
    monkeypatch.setattr(browse, "fetch_airing_today_shows",
                        lambda *a, **k: [])
    monkeypatch.setattr(browse, "fetch_on_the_air_shows", lambda *a, **k: [])
    monkeypatch.setattr(browse, "fetch_popular_shows", lambda *a, **k: [
        {"id": sid, "media_type": "tv", "name": f"Show {sid}",
         "title": f"Show {sid}", "poster_path": f"/s{sid}.jpg",
         "first_air_date": "2025-01-01", "vote_average": 7.5}
        for sid in TV_RAIL_IDS])
    monkeypatch.setattr(browse, "fetch_trending_people", lambda *a, **k: [])
    monkeypatch.setattr(browse, "fetch_trending_movies",
                        lambda *a, **k: TRENDING)
    import api.tmdb_client as tmdb_client
    monkeypatch.setattr(
        tmdb_client, "fetch_tv_show_details",
        lambda sid, **kw: {"id": sid, "name": "S",
                           "last_episode_to_air": {
                               "season_number": 1, "episode_number": 4}})
    # _tonights_picks + _trending_rail go through the cached TMDb client.
    monkeypatch.setattr(browse, "cached_tmdb_request",
                        lambda url, **kw: {"results": TRENDING})
    # tv_aired_progress reads the shared details cache — patch its source
    # function (api.continue_watching.show_details), not the client.
    import api.continue_watching as cw
    monkeypatch.setattr(
        cw, "show_details",
        lambda sid, **kw: {"id": sid, "name": "S",
                           "last_episode_to_air": {
                               "season_number": 1, "episode_number": 4}})
    cw._memo.clear()
    yield
    cw._memo.clear()


def _login(client, u):
    client.post("/login", data={"username": u.username,
                                "password": "ViewSync1"},
                follow_redirects=True)


def test_homepage_anonymous_renders_without_personal_badges(
        client, factory, homepage_tmdb):
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode()
    assert "ql-watched-badge" not in html
    assert "tv-progress-badge" not in html
    assert "__VIEWED_MOVIE_IDS__" not in html


def test_homepage_hero_and_rails_show_viewed_chip(
        client, factory, homepage_tmdb):
    u = factory.user()
    m = factory.media(MOVIE_ID, "movie", "Film 0")
    factory.view(u, m)
    _login(client, u)
    html = client.get("/").data.decode()
    assert "hero-viewed-chip" in html          # hero surface
    assert "ql-watched-badge" in html          # rail cards always present…
    assert "gap-1 hidden" in html              # …hidden for unviewed cards


def test_homepage_store_seeds_viewed_movie_ids(client, factory, homepage_tmdb):
    u = factory.user()
    factory.view(u, factory.media(MOVIE_ID, "movie", "Film 0"))
    _login(client, u)
    html = client.get("/").data.decode()
    assert "__VIEWED_MOVIE_IDS__" in html
    assert str(MOVIE_ID) in html.split("__VIEWED_MOVIE_IDS__", 1)[1]


def test_homepage_tv_rail_shows_progress_badge(client, factory, homepage_tmdb):
    u = factory.user()
    for ep in (1, 2):
        factory.watch(u, TV_RAIL_IDS[0], 1, ep)
    _login(client, u)
    html = client.get("/").data.decode()
    assert "tv-progress-badge" in html
    assert "2 of 4" in html                    # title attr, 50% watched


def test_homepage_tv_progress_has_no_per_card_queries(
        client, factory, homepage_tmdb):
    u = factory.user()
    for sid in TV_RAIL_IDS:                    # 6 started shows on the page
        for ep in (1, 2):
            factory.watch(u, sid, 1, ep)
    _login(client, u)
    _, watch_hits = _count_sql(
        "FROM tv_episode_watch", lambda: client.get("/"))
    _, cal_hits = _count_sql(
        "FROM upcoming_episode", lambda: client.get("/"))
    assert len(watch_hits) <= 1                # batched, not one per card
    assert len(cal_hits) <= 1


# ── TV detail hero + refresh endpoint ───────────────────────────────────────

TV_STUB = {
    "id": SHOW_ID, "name": "S", "poster_path": None, "overview": "",
    "first_air_date": "", "genres": [], "vote_average": 0,
    "number_of_seasons": 1, "seasons": [], "status": "Ended",
    "origin_country": [], "created_by": [], "cast": [],
    "videos": {"results": []}, "episode_run_time": [45],
    "last_episode_to_air": {"season_number": 1, "episode_number": 4},
}


@pytest.fixture
def tv_details_cached(monkeypatch):
    """Patch the details cache source the helper actually reads."""
    import api.continue_watching as cw
    monkeypatch.setattr(cw, "show_details", lambda sid, **kw: dict(TV_STUB))
    cw._memo.clear()
    yield
    cw._memo.clear()


def test_tv_detail_hero_shows_progress(client, factory, monkeypatch,
                                       tv_details_cached):
    import routes.details as details
    monkeypatch.setattr(details, "fetch_tv_show_details",
                        lambda _id: dict(TV_STUB))
    u = factory.user()
    for ep in (1, 2):
        factory.watch(u, SHOW_ID, 1, ep)
    _login(client, u)
    html = client.get(f"/tv/{SHOW_ID}").data.decode()
    assert "data-tv-progress" in html
    assert "2 of 4" in html
    assert "50" in html


def test_tv_detail_no_progress_line_when_not_started(
        client, factory, monkeypatch, tv_details_cached):
    import routes.details as details
    monkeypatch.setattr(details, "fetch_tv_show_details",
                        lambda _id: dict(TV_STUB))
    u = factory.user()
    _login(client, u)
    html = client.get(f"/tv/{SHOW_ID}").data.decode()
    assert "data-tv-progress" not in html


def test_aired_progress_endpoint_requires_login(client, factory):
    r = client.get(f"/api/tv/{SHOW_ID}/aired-progress")
    assert r.status_code in (301, 302, 401)


def test_aired_progress_endpoint_returns_progress(
        client, factory, homepage_tmdb):
    u = factory.user()
    for ep in (1, 2):
        factory.watch(u, SHOW_ID, 1, ep)
    _login(client, u)
    r = client.get(f"/api/tv/{SHOW_ID}/aired-progress")
    assert r.status_code == 200
    assert r.get_json() == {"tv_progress": {"watched": 2, "aired": 4,
                                            "percent": 50.0}}
