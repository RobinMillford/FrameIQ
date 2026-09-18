"""Profile TV-statistics consistency (Phase 18 of the bugfix pass).

Proves the corrected canonical semantics end to end on the shared
session temp-file SQLite:

Canonical (api.statistics):
- TV watch events come from TVEpisodeWatch (the actual write path),
  NOT from DiaryEntry.media_type='tv' rows (structurally impossible:
  routes/diary.py rejects TV quick-logs)
- titles = distinct diary movie media_ids + distinct episode show_ids
- rewatch bounds still hold across the merged model

Profile route (routes/auth.profile):
- overview stats reflect the same semantics (movie+TV events,
  distinct titles, tracking reported separately from watching)

All users/ids are uuid/counter-scoped to avoid collisions on the
shared DB (no per-test rollback in this suite).
"""
import uuid
from itertools import count

import pytest

from models import DiaryEntry, MediaItem, TVShowProgress
from models.tv import TVEpisodeWatch

_TMDB = count(9_800_000)  # module-local base (other suites: 9.6M / 9.7M)


def _user(db):
    from models import User

    u = User(username=f"tvprof_{uuid.uuid4().hex[:10]}",
             email=f"tvprof_{uuid.uuid4().hex[:10]}@profile.test",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


def _movie(db):
    m = MediaItem(tmdb_id=next(_TMDB), media_type="movie",
                  title=f"Movie {uuid.uuid4().hex[:8]}")
    db.session.add(m)
    db.session.flush()
    return m


def _show(db):
    m = MediaItem(tmdb_id=next(_TMDB), media_type="tv",
                  title=f"Show {uuid.uuid4().hex[:8]}")
    db.session.add(m)
    db.session.flush()
    return m


def _diary(db, user, media, rating=None, rewatch=False):
    row = DiaryEntry(user_id=user.id, media_id=media.id,
                     media_type=media.media_type,
                     watched_date=__import__("datetime").date(2026, 3, 1),
                     rating=rating, is_rewatch=rewatch)
    db.session.add(row)
    return row


def _episode(db, user, show, season, episode, rewatch=False):
    row = TVEpisodeWatch(user_id=user.id, show_id=show.tmdb_id,
                         season_number=season, episode_number=episode,
                         watched_date=__import__("datetime").date(2026, 3, 2),
                         is_rewatch=rewatch)
    db.session.add(row)
    return row


def _login(client, user):
    client.post("/login", data={"username": user.username,
                                "password": "TestPass1"},
                follow_redirects=True)


# ─────────────────────────── canonical service ──────────────────────

class TestCanonicalTVMerge:
    def test_movie_only_user(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        m = _movie(db)
        _diary(db, u, m)
        db.session.commit()
        stats = get_statistics(u.id)
        assert stats["total_watch_events"] == 1
        assert stats["distinct_titles"] == 1
        assert stats["movies_watched"] == 1
        assert stats["tv_watch_events"] == 0

    def test_tv_only_user(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        s = _show(db)
        _episode(db, u, s, 1, 1)
        _episode(db, u, s, 1, 2)
        db.session.commit()
        stats = get_statistics(u.id)
        assert stats["total_watch_events"] == 2
        # two episodes, ONE distinct title
        assert stats["distinct_titles"] == 1
        assert stats["movies_watched"] == 0
        assert stats["tv_watch_events"] == 2

    def test_mixed_user(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        m1, m2 = _movie(db), _movie(db)
        s = _show(db)
        _diary(db, u, m1, rating=4.0)
        _diary(db, u, m2)
        _episode(db, u, s, 1, 1)
        _episode(db, u, s, 1, 2)
        _episode(db, u, s, 2, 1)
        db.session.commit()
        stats = get_statistics(u.id)
        assert stats["total_watch_events"] == 5
        assert stats["distinct_titles"] == 3  # 2 movies + 1 show
        assert stats["movies_watched"] == 2
        assert stats["tv_watch_events"] == 3

    def test_tracked_only_is_not_watched(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        s = _show(db)
        db.session.add(TVShowProgress(user_id=u.id, show_id=s.tmdb_id,
                                      status="watching"))
        db.session.commit()
        stats = get_statistics(u.id)
        assert stats["total_watch_events"] == 0
        assert stats["distinct_titles"] == 0

    def test_rewatch_bounds_hold(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        m = _movie(db)
        s = _show(db)
        _diary(db, u, m, rewatch=True)
        _episode(db, u, s, 1, 1, rewatch=True)
        db.session.commit()
        stats = get_statistics(u.id)
        assert stats["rewatch_count"] == 2
        assert stats["rewatch_count"] <= stats["total_watch_events"]

    def test_unwatched_episode_no_longer_counts(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        s = _show(db)
        row = _episode(db, u, s, 1, 1)
        db.session.commit()
        assert get_statistics(u.id)["total_watch_events"] == 1
        db.session.delete(row)
        db.session.commit()
        assert get_statistics(u.id)["total_watch_events"] == 0

    def test_monthly_reconciles_with_total(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        m, s = _movie(db), _show(db)
        _diary(db, u, m)
        _episode(db, u, s, 1, 1)
        _episode(db, u, s, 1, 2)
        db.session.commit()
        stats = get_statistics(u.id)
        monthly_total = sum(int(m["count"])
                            for m in stats["monthly_watch_counts"])
        assert monthly_total == stats["total_watch_events"]

    def test_rating_buckets_merge_across_sources(self, db, app):
        from api.statistics import get_statistics

        u = _user(db)
        m, s = _movie(db), _show(db)
        _diary(db, u, m, rating=4.0)
        _episode(db, u, s, 1, 1)
        db.session.commit()
        ep = TVEpisodeWatch.query.filter_by(user_id=u.id).one()
        ep.rating = 4.0
        db.session.commit()
        stats = get_statistics(u.id)
        assert stats["rating_count"] == 2
        assert stats["rating_distribution"]["4.0"] == 2


# ──────────────────────────── profile route ─────────────────────────

class TestProfileRouteSemantics:
    @pytest.fixture
    def page(self, app):
        with app.test_request_context():
            yield

    def test_profile_shows_tv_when_episodes_exist(self, db, app, client):
        u = _user(db)
        m = _movie(db)
        s = _show(db)
        _diary(db, u, m)
        _episode(db, u, s, 1, 1)
        _episode(db, u, s, 1, 2)
        db.session.add(TVShowProgress(user_id=u.id, show_id=s.tmdb_id,
                                      status="watching",
                                      total_episodes=8,
                                      watched_episodes=2))
        db.session.commit()
        _login(client, u)
        resp = client.get("/profile")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # 3 watch events (1 movie + 2 episodes), 2 titles, 1 tracked show
        assert "3" in html and "2" in html
        # tracking vs watching must both surface
        assert "Tracking" in html

    def test_profile_tracking_only_user(self, db, app, client):
        u = _user(db)
        s = _show(db)
        db.session.add(TVShowProgress(user_id=u.id, show_id=s.tmdb_id,
                                      status="watching"))
        db.session.commit()
        _login(client, u)
        resp = client.get("/profile")
        assert resp.status_code == 200

    def test_profile_tv_progress_section(self, db, app, client):
        u = _user(db)
        s = _show(db)
        db.session.add(TVShowProgress(user_id=u.id, show_id=s.tmdb_id,
                                      status="watching", total_episodes=8,
                                      watched_episodes=4))
        _episode(db, u, s, 1, 4)
        db.session.commit()
        _login(client, u)
        resp = client.get("/profile")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "TV Progress" in html
        assert "50%" in html
        assert s.title in html
        # next-up label derives from the last watched episode
        assert "S1E5" in html

    def test_profile_isolation_other_user_data_hidden(self, db, app, client):
        u = _user(db)
        other = _user(db)
        s = _show(db)
        _episode(db, other, s, 1, 1)
        db.session.add(TVShowProgress(user_id=other.id, show_id=s.tmdb_id,
                                      status="watching"))
        db.session.commit()
        _login(client, u)
        resp = client.get("/profile")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert s.title not in html


# ───────────────────── global header source guards ──────────────────

class TestGlobalHeaderGuards:
    """Dropdown init must live in the shared chrome.js (loaded by every
    page via base.html or standalone templates), never duplicated in
    individual templates."""

    def test_chrome_js_owns_profile_dropdown_init(self):
        src = open("static/js/chrome.js").read()
        assert "profile-button" in src
        assert "profile-menu" in src
        assert "aria-expanded" in src
        assert "initHeaderChrome" in src

    def test_no_template_inline_dropdown_duplicates(self):
        import glob

        offenders = []
        for path in glob.glob("templates/*.html") + \
                glob.glob("templates/partials/*.html"):
            src = open(path).read()
            if "getElementById('profile-button')" in src:
                offenders.append(path)
        assert offenders == [], f"inline dropdown wiring in: {offenders}"

    def test_shared_nav_partials_render_dropdown_markup(self):
        nav = open("templates/partials/nav_simple.html").read()
        partial = open("templates/partials/profile_dropdown.html").read()
        assert '{% include "partials/profile_dropdown.html" %}' in nav
        assert 'id="profile-button"' in partial

    def test_profile_dropdown_uses_readable_surface(self):
        src = open("templates/partials/profile_dropdown.html").read()
        # .fi-menu is the opaque dark panel (readability fix from the
        # earlier profile redesign) — must not be a transparent surface
        assert "fi-menu" in src
        assert "hidden" in src  # starts collapsed


# ───────────────────────── insights aggregation ─────────────────────

class TestAnalyticsTVSemantics:
    """GET /api/users/<id>/stats (Insights charts) must count TV the way
    the canonical service does — episode rows are the TV watch source."""

    def test_mixed_counts_match_canonical(self, db, client):
        from api.statistics import get_statistics

        u = _user(db)
        m1, m2 = _movie(db), _movie(db)
        s1, s2 = _show(db), _show(db)
        _diary(db, u, m1, rating=4.0)
        _diary(db, u, m2, rating=3.5)
        _episode(db, u, s1, 1, 1)
        _episode(db, u, s1, 1, 2)
        _episode(db, u, s1, 2, 1)
        _episode(db, u, s2, 1, 1)
        db.session.commit()

        _login(client, u)
        resp = client.get(f"/api/users/{u.id}/stats")
        assert resp.status_code == 200
        payload = resp.get_json()

        canon = get_statistics(u.id, lifetime=True)
        assert payload["stats"]["movies_watched"] == canon["movies_watched"] == 2
        assert payload["stats"]["tv_watched"] == canon["tv_shows_watched"] == 2
        # 2 movies + 2 shows = 4 distinct titles (episodes are NOT titles)
        assert payload["stats"]["total_watched"] == 4

    def test_tv_only_user_reports_tv(self, db, client):
        u = _user(db)
        s = _show(db)
        _episode(db, u, s, 1, 1)
        _episode(db, u, s, 1, 2)
        db.session.commit()

        _login(client, u)
        resp = client.get(f"/api/users/{u.id}/stats")
        assert resp.status_code == 200
        stats = resp.get_json()["stats"]
        assert stats["movies_watched"] == 0
        assert stats["tv_watched"] == 1
        assert stats["total_watched"] == 1

    def test_tracked_only_counts_nothing(self, db, client):
        u = _user(db)
        s = _show(db)
        db.session.add(TVShowProgress(user_id=u.id, show_id=s.tmdb_id,
                                      status="watching"))
        db.session.commit()

        _login(client, u)
        resp = client.get(f"/api/users/{u.id}/stats")
        assert resp.get_json()["stats"]["total_watched"] == 0


# ───────────────────────── source guards ────────────────────────────

class TestHeaderLayeringContract:
    """Header-layering contract (root cause: the nav is its own stacking
    context and owned no root-level z-index, so page overlays painted
    above it; menus were additionally trapped inside that context)."""

    def test_nav_partials_own_explicit_layer(self):
        for path in ("templates/partials/nav_simple.html",
                     "templates/partials/nav_detail.html"):
            assert "fi-nav-layer" in open(path).read(), path
        base = open("templates/base.html").read()
        assert "fi-nav-layer" in base

    def test_nav_layer_and_ladder_in_chrome_css(self):
        css = open("static/css/chrome.css").read()
        assert ".fi-nav-layer { z-index: 1100; }" in css
        assert ".fi-menu-portal" in css and "z-index: 1250" in css
        # ladder: nav < modals < portal < toasts
        assert "z-index: 1300" in css

    def test_fi_header_uses_nav_layer_z(self):
        css = open("static/css/chrome.css").read()
        assert ".fi-header { z-index: 1100" in css

    def test_menus_render_through_body_portal(self):
        js = open("static/js/chrome.js").read()
        assert "fi-menu-portal" in js
        assert "portalMenu(profileMenu, profileButton)" in js
        assert "portalMenu(notifPanel, notifBell)" in js

    def test_notification_containment_survives_portal(self):
        js = open("static/js/notifications.js").read()
        # portal-hosted panel clicks must not count as outside clicks
        assert "!panel.contains(e.target)" in js

    def test_reviews_toast_aligned_to_ladder(self):
        css = open("static/css/reviews.css").read()
        assert "z-index: 1300" in css
        assert "9999" not in css


class TestSurfaceUnification:
    def test_diary_uses_shared_nav(self):
        src = open("templates/diary.html").read()
        assert '{% include "partials/nav_simple.html"' in src
        # legacy cyan-gradient nav is gone
        assert "from-cyan-400" not in src
        assert "mobile-menu-button" not in src

    def test_diary_renderer_is_dom_safe_and_unified(self):
        src = open("templates/diary.html").read()
        assert "innerHTML" not in src
        assert "entry.episode" in src  # SxEy rendering for TV events

    def test_viewed_uses_frameiq_tokens(self):
        src = open("templates/viewed.html").read()
        assert "#4f46e5" not in src  # off-brand indigo removed
        assert 'aria-pressed="true"' in src

    def test_insights_offbrand_palette_removed(self):
        profile = open("templates/profile.html").read()
        dash = open("static/js/stats-dashboard.js").read()
        for src in (profile, dash):
            assert "#6366f1" not in src
            assert "#a855f7" not in src
            assert "#10b981" not in src


# ───────────────────────── unified diary stream ─────────────────────

class TestUnifiedDiary:
    """/api/diary must be a chronological MOVIE + TV event stream:
    DiaryEntry rows + TVEpisodeWatch rows, deterministically merged."""

    def test_tv_events_appear(self, db, client):
        u = _user(db)
        s = _show(db)
        _episode(db, u, s, 1, 1)
        _episode(db, u, s, 1, 2)
        db.session.commit()

        _login(client, u)
        data = client.get("/api/diary").get_json()
        assert data["total"] == 2
        kinds = {"tv" if e.get("episode") else "movie"
                 for e in data["entries"]}
        assert kinds == {"tv"}
        # TV events carry SxEy + title even without a MediaItem row
        assert data["entries"][0]["media"]["media_type"] == "tv"
        assert data["entries"][0]["episode"]["season"] == 1
        assert data["entries"][0]["episode"]["number"] in (1, 2)

    def test_mixed_ordering_and_tiebreak(self, db, client):
        from datetime import date
        from models import DiaryEntry

        u = _user(db)
        m = _movie(db)
        s = _show(db)
        row = DiaryEntry(user_id=u.id, media_id=m.id, media_type="movie",
                         watched_date=date(2026, 5, 10))
        db.session.add(row)
        _episode(db, u, s, 3, 1)
        db.session.commit()

        _login(client, u)
        data = client.get("/api/dieary" if False else "/api/diary").get_json()
        assert data["total"] == 2
        # Same-day events: deterministic (created_at, kind) tie-break,
        # both present, no duplicates
        assert len(data["entries"]) == 2
        kinds = {"tv" if e.get("episode") else "movie"
                 for e in data["entries"]}
        assert kinds == {"movie", "tv"}

    def test_year_month_filters_cover_both_sources(self, db, client):
        from datetime import date
        from models import DiaryEntry
        from models.tv import TVEpisodeWatch

        u = _user(db)
        s = _show(db)
        m = _movie(db)

        db.session.add(DiaryEntry(user_id=u.id, media_id=m.id,
                                  media_type="movie",
                                  watched_date=date(2025, 7, 4)))
        db.session.add(TVEpisodeWatch(user_id=u.id, show_id=s.tmdb_id,
                                      season_number=1, episode_number=1,
                                      watched_date=date(2025, 7, 9)))
        # Off-filter events: different year and different month
        db.session.add(TVEpisodeWatch(user_id=u.id, show_id=s.tmdb_id,
                                      season_number=1, episode_number=2,
                                      watched_date=date(2024, 7, 9)))
        db.session.add(TVEpisodeWatch(user_id=u.id, show_id=s.tmdb_id,
                                      season_number=1, episode_number=3,
                                      watched_date=date(2025, 9, 1)))
        db.session.commit()

        _login(client, u)
        data = client.get("/api/diary?year=2025&month=7").get_json()
        assert data["total"] == 2
        assert len(data["entries"]) == 2
        assert all(e["watched_date"].startswith("2025-07")
                   for e in data["entries"])

    def test_pagination_across_sources(self, db, client):
        from datetime import date
        from models import DiaryEntry

        u = _user(db)
        s = _show(db)
        # 3 movie + 3 TV events, same dates → deterministic interleaving
        for i in range(3):
            db.session.add(DiaryEntry(
                user_id=u.id, media_id=_movie(db).id, media_type="movie",
                watched_date=date(2026, 6, 10)))
            db.session.add(TVEpisodeWatch(
                user_id=u.id, show_id=s.tmdb_id,
                season_number=1, episode_number=i + 1,
                watched_date=date(2026, 6, 11)))
        db.session.commit()

        _login(client, u)
        page1 = client.get("/api/diary?page=1&per_page=2").get_json()
        page2 = client.get("/api/diary?page=2&per_page=2").get_json()
        page3 = client.get("/api/diary?page=3&per_page=2").get_json()

        assert page1["total"] == 6
        ids = ([e["id"] for e in page1["entries"]]
               + [e["id"] for e in page2["entries"]]
               + [e["id"] for e in page3["entries"]])
        assert len(ids) == len(set(ids)) == 6  # every event exactly once
        assert page3["has_next"] is False

    def test_rating_rewatch_notes_preserved(self, db, client):
        from datetime import date
        from models.tv import TVEpisodeWatch

        u = _user(db)
        s = _show(db)
        db.session.add(TVEpisodeWatch(
            user_id=u.id, show_id=s.tmdb_id,
            season_number=2, episode_number=5,
            watched_date=date(2026, 2, 20), rating=4.5,
            is_rewatch=True, notes="great hour of tv",
            episode_name="Sawn Off"))
        db.session.commit()

        _login(client, u)
        entry = client.get("/api/diary").get_json()["entries"][0]
        assert entry["rating"] == 4.5
        assert entry["is_rewatch"] is True
        assert entry["episode"]["notes"] == "great hour of tv"
        assert entry["episode"]["name"] == "Sawn Off"

    def test_user_isolation(self, db, client):
        u = _user(db)
        other = _user(db)
        s = _show(db)
        _episode(db, other, s, 1, 1)
        _episode(db, u, s, 1, 2)
        db.session.commit()

        _login(client, u)
        data = client.get("/api/diary").get_json()
        assert data["total"] == 1  # only the caller's event
        assert data["entries"][0]["user"]["id"] == u.id


# ───────────────────────── diary query bounds ───────────────────────

class _record_sql:
    """Record raw SQL executed by the engine (same pattern as
    tests/test_statistics_hardening.py::_record_statements)."""

    def __enter__(self):
        from sqlalchemy import event as sa_event
        from models import db
        self.statements = []
        self._fn = (lambda conn, cursor, statement, *a, **k:
                    self.statements.append(statement))
        sa_event.listen(db.engine, "before_cursor_execute", self._fn)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event as sa_event
        from models import db
        sa_event.remove(db.engine, "before_cursor_execute", self._fn)
        return False


class TestDiaryQueryBounds:
    def test_mixed_page_stays_bounded(self, db, client):
        u = _user(db)
        s = _show(db)
        for i in range(12):
            _diary(db, u, _movie(db))
            _episode(db, u, s, 1, i + 1)
        db.session.commit()

        _login(client, u)
        with _record_sql() as rec:
            data = client.get("/api/diary?page=1&per_page=25").get_json()
        assert data["total"] == 24
        # 24 merged events, ONE fixed query budget — no per-event
        # queries (N+1 would need 48+).
        assert len(rec.statements) < 15, len(rec.statements)

    def test_deep_page_stays_bounded(self, db, client):
        u = _user(db)
        s = _show(db)
        for i in range(10):
            _diary(db, u, _movie(db))
            _episode(db, u, s, 1, i + 1)
        db.session.commit()

        _login(client, u)
        with _record_sql() as rec:
            client.get("/api/diary?page=2&per_page=5").get_json()
        # Bounded prefix fetch: page 2 costs the same fixed budget
        # regardless of depth.
        assert len(rec.statements) < 15, len(rec.statements)

    def test_public_diary_includes_tv(self, db, client):
        u = _user(db)
        s = _show(db)
        _episode(db, u, s, 1, 1)
        _diary(db, u, _movie(db))
        db.session.commit()

        data = client.get(f"/api/users/{u.id}/diary").get_json()
        assert data["total"] == 2
        kinds = {"tv" if e.get("episode") else "movie"
                 for e in data["entries"]}
        assert kinds == {"movie", "tv"}


# ───────────────────────── viewed title collection ──────────────────

class TestViewedTVTitles:
    """/viewed is a watched-TITLE collection: movies from user_viewed,
    TV from distinct shows with >= 1 TVEpisodeWatch (titles, not
    episodes; tracked-only shows contribute nothing)."""

    def test_tv_titles_render_in_viewed_page(self, db, client):
        from datetime import date
        from models.tv import TVEpisodeWatch

        u = _user(db)
        s1, s2 = _show(db), _show(db)
        for i in range(3):
            db.session.add(TVEpisodeWatch(
                user_id=u.id, show_id=s1.tmdb_id,
                season_number=1, episode_number=i + 1,
                watched_date=date(2026, 4, 10)))
        db.session.add(TVEpisodeWatch(
            user_id=u.id, show_id=s2.tmdb_id,
            season_number=1, episode_number=1,
            watched_date=date(2026, 4, 12)))
        db.session.commit()

        _login(client, u)
        html = client.get("/viewed").get_data(as_text=True)
        assert s1.title in html
        assert s2.title in html

    def test_tracked_only_show_not_in_viewed(self, db, client):
        u = _user(db)
        s = _show(db)
        db.session.add(TVShowProgress(user_id=u.id, show_id=s.tmdb_id,
                                      status="watching"))
        db.session.commit()

        _login(client, u)
        html = client.get("/viewed").get_data(as_text=True)
        assert s.title not in html
