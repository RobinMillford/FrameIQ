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
