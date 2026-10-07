"""Task F2 — browser verification of the DERIVED TV Viewed state.

Boots the REAL Flask app on a throwaway SQLite temp DB (conftest pattern:
SKIP_SCHEMA_GUARD=1) and drives it with Playwright headless Chromium.
Companion to scripts/verify_tv_f1_browser.py (write-path unification);
this script exercises the VIEWED verdict surface end-to-end:

  F2-A  Canonical badge on: seeded 38/38 Banshee → hero progress line
        (38 of 38), green Viewed badge visible, Unmark action offered,
        Mark-as-Viewed NOT offered.
  F2-B  Symmetric unmark through GET /remove_from_viewed/<id>/tv →
        ALL 38 canonical rows cleared (spec §43 option A — same
        explicit-removal semantics as unmark-season), hero progress
        line disappears, badge gone, Mark-as-Viewed offered again,
        /progress zero-pads (never the recomputed-from-nothing stored
        counters).
  F2-C  Running show lifecycle (deterministic patched TMDb fixture):
        25/25 → badge and 100%; S3E6 airs (calendar row) → reload →
        25 of 26, badge OFF without any manual reset; watch the new
        episode → 26 of 26 → badge back ON.
  F2-D  Legacy stored-counter reconciliation: a tracking row claiming
        8/8 watched with ZERO TVEpisodeWatch rows renders NO hero
        progress line, NO Viewed badge, and /unfinished-shows publishes
        the canonical 0/0 — the stale counters never leak.
  F2-E  Cross-surface: homepage renders; the canonical Viewed-list
        surface /viewed shows the show (canonical ledger feeds the TV
        half) while the tracked-but-not-started legacy show does NOT.
        /profile renders the TV Progress section with no stale 100%.
  F2-F  Responsive: 320/360/390/414/768/1024/1440 — hero line visible
        and readable at every width, no horizontal overflow; badge
        visible at desktop width.

Run:  .venv/bin/python scripts/verify_tv_f2_browser.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_f2_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "f2-browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

BANSHEE_ID = 41727            # real TMDb show: 10/10/10/8 = 38 aired
RUNNING_ID = 993302           # fake TMDb id far from real shows
LEGACY_ID = 993301            # fake TMDb id far from real shows
FAILURES = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else "")
    print(line)
    if not cond:
        FAILURES.append(name)


def main():
    from playwright.sync_api import sync_playwright

    from app import app as flask_app
    from models import db as _db

    flask_app.config.update(
        TESTING=False, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)

    with flask_app.app_context():
        _db.create_all()

    import threading
    t = threading.Thread(
        target=lambda: flask_app.run(
            host="127.0.0.1", port=5003, debug=False, use_reloader=False),
        daemon=True)
    t.start()

    import time as _time
    import requests as _requests
    for _ in range(60):
        try:
            _requests.get("http://127.0.0.1:5003/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: Flask dev server did not start")
        sys.exit(2)

    import api.continue_watching as cw
    import api.tmdb_client
    import routes.details as details_mod
    import routes.tv_tracking as tv_tracking_mod
    from datetime import date, timedelta
    from models import (MediaItem, TVEpisodeWatch, TVShowProgress,
                        UpcomingEpisode, User, db)

    # ── Deterministic fixture for the fake running show ─────────────
    _real_fetch = api.tmdb_client.fetch_tv_show_details
    cw._memo.clear()

    RUNNING_SEASONS = {1: 10, 2: 10, 3: 12}

    def _running_details(anchor_ep):
        return {
            "id": RUNNING_ID, "name": "F2 Running Verify",
            "overview": "", "tagline": "",
            "status": "Returning Series",
            "first_air_date": "2020-01-01", "last_air_date": "2026-01-01",
            "number_of_seasons": 3,
            "number_of_episodes": sum(RUNNING_SEASONS.values()),
            "last_episode_to_air": {"season_number": 3,
                                    "episode_number": anchor_ep},
            "seasons": [
                {"season_number": sn, "episode_count": ec,
                 "air_date": "2020-01-01", "name": f"Season {sn}",
                 "overview": "", "poster_path": ""}
                for sn, ec in sorted(RUNNING_SEASONS.items())],
            "poster_path": "", "backdrop_path": "",
            "genres": ["Drama"], "vote_average": 0, "vote_count": 0,
            "creator": None, "cast": [], "trailer_url": None,
            "recommendations": [], "reviews": [],
        }

    def _patched_fetch(show_id, **kw):
        if show_id == RUNNING_ID:
            return _running_details(5)      # S1..S2 full, S3E1..E5 aired
        if show_id == LEGACY_ID:
            payload = dict(_running_details(0))
            payload.update({
                "id": LEGACY_ID, "name": "F2 Browser Show",
                "status": "Ended", "number_of_seasons": 1,
                "number_of_episodes": 10,
                "last_episode_to_air": {"season_number": 1,
                                        "episode_number": 10},
                "seasons": [{"season_number": 1, "episode_count": 10,
                             "air_date": "2020-01-01", "name": "Season 1",
                             "overview": "", "poster_path": ""}],
            })
            return payload
        return _real_fetch(show_id, **kw)

    api.tmdb_client.fetch_tv_show_details = _patched_fetch
    details_mod.fetch_tv_show_details = _patched_fetch
    tv_tracking_mod.fetch_tv_show_details = _patched_fetch

    with flask_app.app_context():
        if not User.query.filter_by(username="f2verify").first():
            user = User(username="f2verify", email="f2verify@example.com",
                        email_verified=True)
            user.set_password("F2verify1!")
            db.session.add(user)
        for tmdb_id in (BANSHEE_ID, RUNNING_ID, LEGACY_ID):
            if not MediaItem.query.filter_by(
                    tmdb_id=tmdb_id, media_type="tv").first():
                db.session.add(MediaItem(
                    tmdb_id=tmdb_id, media_type="tv",
                    title="F2 Browser Show"))
        db.session.commit()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        base = "http://127.0.0.1:5003"
        page.on("dialog", lambda dialog: dialog.accept())

        # ── login ─────────────────────────────────────────────────────
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "f2verify")
        page.fill('input[name="password"]', "F2verify1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        def seed_s1s2(uid, show_id):
            """Canonical reality: S1E1-E10 + S2E1-E10 watched."""
            with flask_app.app_context():
                if TVEpisodeWatch.query.filter_by(
                        user_id=uid, show_id=show_id).count():
                    return
                rows = []
                for sn in (1, 2):
                    for ep in range(1, 11):
                        rows.append(TVEpisodeWatch(
                            user_id=uid, show_id=show_id, season_number=sn,
                            episode_number=ep,
                            watched_date=date(2026, 1, 10)))
                db.session.add_all(rows)
                db.session.commit()

        def seed_stale_counter(uid, show_id):
            """Pre-F1 tracking row claiming 8/8 watched; zero ledger."""
            with flask_app.app_context():
                if not TVShowProgress.query.filter_by(
                        user_id=uid, show_id=show_id).first():
                    db.session.add(TVShowProgress(
                        user_id=uid, show_id=show_id, status="watching",
                        total_episodes=8, watched_episodes=8,
                        watched_seasons=1, total_seasons=1))
                    db.session.commit()

        def db_watched_count(uid, show_id):
            with flask_app.app_context():
                return TVEpisodeWatch.query.filter_by(
                    user_id=uid, show_id=show_id).count()

        def hero_state(page, timeout=15000):
            hero = page.locator("[data-tv-progress]")
            hero.wait_for(timeout=timeout)
            return {"text": hero.inner_text(),
                    "watch": hero.get_attribute("data-watched"),
                    "aired": hero.get_attribute("data-aired"),
                    "percent": hero.get_attribute("data-percent"),
                    "count": page.locator("[data-tv-progress]").count(),
                    # Scoped to the green status span: the nav also has
                    # a text-exact "Viewed" link that must not count.
                    "badge": page.locator(
                        "span.bg-green-600:has-text('Viewed')").count(),
                    "mark": page.locator(
                        "a:has-text('Mark as Viewed')").count(),
                    "unmark": page.locator(
                        "a:has-text('Unmark Viewed')").count()}

        def badge_count(page):
            return page.locator(
                "span.bg-green-600:has-text('Viewed')").count()

        uid = None
        with flask_app.app_context():
            uid = User.query.filter_by(username="f2verify").first().id

        # ══ F2-A: canonical badge ON at 38/38 ═════════════════════════
        seed_s1s2(uid, BANSHEE_ID)
        with flask_app.app_context():
            # A F1-marked user also holds a tracking row (the write core
            # creates it); seed it so F2-B exercises the real zero-pad
            # path instead of the no-row {'progress': None} case.
            if not TVShowProgress.query.filter_by(
                    user_id=uid, show_id=BANSHEE_ID).first():
                db.session.add(TVShowProgress(
                    user_id=uid, show_id=BANSHEE_ID, status="watching"))
                db.session.commit()
            for sn, last in ((3, 10), (4, 8)):
                exists = TVEpisodeWatch.query.filter_by(
                    user_id=uid, show_id=BANSHEE_ID, season_number=sn
                ).first()
                if exists:
                    continue
                db.session.add_all(
                    TVEpisodeWatch(user_id=uid, show_id=BANSHEE_ID,
                                   season_number=sn, episode_number=ep,
                                   watched_date=date(2026, 1, 10))
                    for ep in range(1, last + 1))
            db.session.commit()

        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F2-A hero 38 of 38 aired",
              "38 of 38" in st["text"], st["text"][:120])
        check("F2-A hero data attrs 38/38/100",
              st["watch"] == "38" and st["aired"] == "38"
              and st["percent"] in ("100", "100.0"), f"{st}")
        check("F2-A Viewed badge visible ON", st["badge"] >= 1)
        check("F2-A Unmark action offered", st["unmark"] >= 1)
        check("F2-A Mark-as-Viewed NOT offered", st["mark"] == 0)
        check("F2-A canonical ledger: 38 rows",
              db_watched_count(uid, BANSHEE_ID) == 38)

        # ══ F2-B: symmetric unmark → ALL rows cleared (§43-A) ═════════
        page.goto(f"{base}/remove_from_viewed/{BANSHEE_ID}/tv",
                  wait_until="domcontentloaded")
        page.wait_for_timeout(400)
        check("F2-B unmark clears ALL 38 canonical rows (option A)",
              db_watched_count(uid, BANSHEE_ID) == 0,
              f"got {db_watched_count(uid, BANSHEE_ID)}")
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        check("F2-B hero progress line gone",
              page.locator("[data-tv-progress]").count() == 0)
        check("F2-B Viewed badge OFF", badge_count(page) == 0)
        check("F2-B Mark-as-Viewed offered again",
              page.locator("a:has-text('Mark as Viewed')").count() >= 1)
        check("F2-B Unmark action gone",
              page.locator("a:has-text('Unmark Viewed')").count() == 0)
        p = page.evaluate(
            "fetch('/api/tv/%d/progress').then(r => r.json())"
            % BANSHEE_ID)["progress"]
        check("F2-B /progress zero-pads after unmark",
              p["watched_episodes"] == 0 and p["total_episodes"] == 0
              and p["progress_percentage"] == 0, f"{p}")

        # ══ F2-C: running show lifecycle ══════════════════════════════
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        with flask_app.app_context():
            if not TVShowProgress.query.filter_by(
                    user_id=uid, show_id=RUNNING_ID).first():
                db.session.add(TVShowProgress(
                    user_id=uid, show_id=RUNNING_ID, status="watching"))
                db.session.commit()
            db_watched_count_snapshot = TVEpisodeWatch.query.filter_by(
                user_id=uid, show_id=RUNNING_ID).count()
        if db_watched_count_snapshot == 0:
            with flask_app.app_context():
                db.session.add_all(
                    TVEpisodeWatch(user_id=uid, show_id=RUNNING_ID,
                                   season_number=sn, episode_number=ep,
                                   watched_date=date(2026, 2, 1))
                    for sn, last in ((1, 10), (2, 10), (3, 5))
                    for ep in range(1, last + 1))
                db.session.commit()
        page.reload(wait_until="domcontentloaded")
        st = hero_state(page)
        check("F2-C 25/25 badge ON (100%)",
              "25 of 25" in st["text"] and "100%" in st["text"]
              and st["badge"] >= 1, st["text"][:120])

        # S3E6 airs mid-flow (calendar sync row, exactly like production).
        with flask_app.app_context():
            exists = UpcomingEpisode.query.filter_by(
                show_id=RUNNING_ID, season_number=3,
                episode_number=6).first()
            if not exists:
                db.session.add(UpcomingEpisode(
                    show_id=RUNNING_ID, show_name="F2 Running Verify",
                    season_number=3, episode_number=6,
                    air_date=date.today() - timedelta(days=1)))
                db.session.commit()
        page.reload(wait_until="domcontentloaded")
        st = hero_state(page)
        check("F2-C new episode lowers to 25 of 26, badge OFF",
              "25 of 26" in st["text"] and st["badge"] == 0,
              st["text"][:120])
        p = page.evaluate(
            "fetch('/api/tv/%d/progress').then(r => r.json())"
            % RUNNING_ID)["progress"]
        check("F2-C /progress 25/26 96.2%",
              p["watched_episodes"] == 25 and p["total_episodes"] == 26
              and p["progress_percentage"] == 96.2, f"{p}")

        # Watch S3E6 through the watch-page finish button.
        page.goto(f"{base}/watch/tv/{RUNNING_ID}/3/6",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        page.click("#mark-btn")
        page.wait_for_timeout(2000)
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F2-C badge back ON at 26 of 26",
              "26 of 26" in st["text"] and st["badge"] >= 1,
              st["text"][:120])

        # ══ F2-D: legacy stored-counter never leaks ═══════════════════
        page.goto(f"{base}/tv/{LEGACY_ID}", wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        check("F2-D no hero progress before any watch",
              page.locator("[data-tv-progress]").count() == 0)
        check("F2-D no Viewed badge", badge_count(page) == 0)
        seed_stale_counter(uid, LEGACY_ID)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(300)
        check("F2-D stale 8/8 row still renders no hero",
              page.locator("[data-tv-progress]").count() == 0)
        check("F2-D stale 8/8 row still renders no badge",
              badge_count(page) == 0)
        shelf = page.evaluate(
            "fetch('/api/tv/unfinished-shows').then(r => r.json())")
        card = next((s for s in shelf["shows"]
                     if s["show_id"] == LEGACY_ID), None)
        check("F2-D shelf publishes canonical 0/0",
              card is not None and card["watched_episodes"] == 0
              and card["total_episodes"] == 0
              and card["progress_percent"] == 0,
              f"{card}")
        p = page.evaluate(
            "fetch('/api/tv/%d/progress').then(r => r.json())"
            % LEGACY_ID)["progress"]
        check("F2-D /progress zero-pads stale counters",
              p["watched_episodes"] == 0 and p["total_episodes"] == 0
              and p["progress_percentage"] == 0, f"{p}")

        # ══ F2-E: cross-surface agreement ═════════════════════════════
        # Re-seed S1+S2 (20 rows): Banshee is again partially watched,
        # which is what the /viewed and profile surfaces must reflect.
        seed_s1s2(uid, BANSHEE_ID)
        page.goto(f"{base}/", wait_until="domcontentloaded")
        check("F2-E homepage renders", page.locator(
            "body").count() == 1)
        page.goto(f"{base}/viewed", wait_until="domcontentloaded")
        viewed_html = page.content()
        check("F2-E /viewed lists the partially-watched show "
              "(canonical ledger feeds the TV half)",
              f"/tv/{BANSHEE_ID}" in viewed_html
              and "F2 Browser Show" in viewed_html)
        check("F2-E /viewed does NOT leak tracked-not-started legacy show",
              f"/tv/{LEGACY_ID}" not in viewed_html)
        resp = page.goto(f"{base}/profile", wait_until="domcontentloaded")
        check("F2-E profile renders 200", resp.status == 200)
        profile_html = page.content()
        check("F2-E profile TV Progress section renders",
              "Your TV Progress" in profile_html)
        # The Banshee row (20/38) must read the canonical 20% — scoped to
        # that row's <li> block, because the RUNNING show legitimately
        # completed 26/26 in F2-C and its own row IS 100%.
        import re as _re
        rows = _re.findall(
            r"<li class=\"surface-secondary.*?</li>", profile_html,
            _re.S)
        banshee_rows = [r for r in rows if f"/tv/{BANSHEE_ID}" in r]
        check("F2-E profile Banshee row renders", len(banshee_rows) == 1,
              f"rows={len(rows)} banshee={len(banshee_rows)}")
        if banshee_rows:
            # Profile halves-up the canonical percent (52.6 → 53), the
            # same deterministic UI math pinned by test_profile_tv_stats.
            check("F2-E profile Banshee row canonical 53% watched",
                  "53% watched" in banshee_rows[0]
                  and "100% watched" not in banshee_rows[0],
                  banshee_rows[0][:220])

        # ══ F2-F: responsive width sweep on the live badge page ═══════
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        st = hero_state(page)   # 20/38 partial state from re-seed
        check("F2-F pre-sweep hero present (20 of 38)",
              "20 of 38" in st["text"])
        for width in (320, 360, 390, 414, 768, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(250)
            hero = page.locator("[data-tv-progress]")
            visible = hero.count() > 0 and hero.is_visible()
            txt = hero.inner_text()
            # Hero percent renders as {{ percent|int }}% → "52% watched".
            hero_ok = (visible and "20 of 38" in txt
                       and "52% watched" in txt)
            check(f"F2-F {width}px: hero visible with 20 of 38 · 52% "
                  f"(canonical)", hero_ok,
                  f"visible={visible} txt={txt[:80]!r}")
            if width == 1440:
                check("F2-F 1440px: partial state keeps badge OFF",
                      badge_count(page) == 0)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth")
            check(f"F2-F {width}px: no horizontal overflow", overflow <= 0,
                  f"overflow={overflow}")

        browser.close()

    with flask_app.app_context():
        _db.session.remove()
        _db.drop_all()
    try:
        os.remove(_test_db_path)
    except OSError:
        pass

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {FAILURES}")
        sys.exit(1)
    print("ALL F2 BROWSER CHECKS PASSED")


if __name__ == "__main__":
    main()
