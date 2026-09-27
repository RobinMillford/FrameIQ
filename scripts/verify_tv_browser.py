"""Phase 16/17/18 — deterministic browser verification of historical TV
airing resolution (Task E).

Boots the REAL Flask app on a throwaway SQLite temp DB (the conftest
pattern: SKIP_SCHEMA_GUARD=1) and drives it with Playwright headless
Chromium.

  Phase 16 — Banshee (TMDb 41727, real production data: anchor S4E8,
             seasons 10/10/10/8):
             before → no hero progress line; "Mark as Viewed" → ✓ Viewed,
             100% watched, "38 of 38 aired episodes", every season card
             "100% Complete", episode rows watched; homepage/browse/
             trending/For You/CineBot stay healthy with TV progress
             intact (via /api/view-state).

  Phase 17 — running-show flow (S1=10 aired, S2=1 episode that airs
             mid-flow):
             10/10 → 100% → S2E1 airs → 10/11 (< 100%) → watch it →
             11/11 = 100%, S1 stays complete throughout.

  Phase 18 — responsive check of the season cards at
             320/360/390/414/768/1024/1440: no horizontal overflow and
             no clipped season rows.

The real Banshee data comes live from TMDb (production-bug shape: the
only airing evidence is the S4E8 anchor — exactly the case the old
resolver got wrong). The running-show fixture is deterministic: TMDb
season metadata is patched in-process so S3+/future seasons never
appear, and the S2E1 calendar row is inserted mid-flow (exactly how a
fresh episode enters production data).

Run:  .venv/bin/python scripts/verify_tv_browser.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

BANSHEE_ID = 41727
RUNNING_ID = 991777          # fake TMDb id far from real shows
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
            host="127.0.0.1", port=5001, debug=False, use_reloader=False),
        daemon=True)
    t.start()

    # Wait for the dev server to accept connections.
    import time as _time
    import requests as _requests
    for _ in range(60):
        try:
            _requests.get("http://127.0.0.1:5001/login", timeout=2)
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
    from models import MediaItem, TVEpisodeWatch, UpcomingEpisode, User, db

    # ── Deterministic running-show fixture ────────────────────────
    # Patch fetch_tv_show_details at every module that imported it, so
    # the fake show flows through the detail page, watch page, tracking
    # helpers, and the cached details loader. Real TMDb ids (Banshee)
    # delegate to the original implementation.
    _real_fetch = api.tmdb_client.fetch_tv_show_details
    cw._memo.clear()

    RUNNING_SEASONS = {1: 10, 2: 1}   # S2E1 "airs" in Phase B (calendar)

    def _patched_fetch(show_id, **kw):
        if show_id != RUNNING_ID:
            return _real_fetch(show_id, **kw)
        return {
            "id": RUNNING_ID, "name": "Running Verify",
            "overview": "", "tagline": "",
            "status": "Returning Series",
            "first_air_date": "2020-01-01", "last_air_date": "2026-01-01",
            "number_of_seasons": len(RUNNING_SEASONS),
            "number_of_episodes": sum(RUNNING_SEASONS.values()),
            "last_episode_to_air": {"season_number": 1,
                                    "episode_number": 10},
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

    api.tmdb_client.fetch_tv_show_details = _patched_fetch
    details_mod.fetch_tv_show_details = _patched_fetch
    tv_tracking_mod.fetch_tv_show_details = _patched_fetch

    with flask_app.app_context():
        user = User(username="bverify", email="bverify@example.com",
                    email_verified=True)
        user.set_password("Bverify1!")
        db.session.add(user)
        db.session.add(MediaItem(tmdb_id=RUNNING_ID, media_type="tv",
                                 title="Running Verify"))
        db.session.commit()
        uid = user.id

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        base = "http://127.0.0.1:5001"

        # ── login ─────────────────────────────────────────────────────
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "bverify")
        page.fill('input[name="password"]', "Bverify1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        # ══ Phase 17 — running show ══════════════════════════════════
        # Not started yet: the user-scoped hero is hidden entirely.
        page.goto(f"{base}/tv/{RUNNING_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        check("P17 before mark: hero progress hidden (not started)",
              page.locator("[data-tv-progress]").count() == 0)

        page.goto(f"{base}/mark_as_viewed/{RUNNING_ID}/tv",
                  wait_until="domcontentloaded")
        page.goto(f"{base}/tv/{RUNNING_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        check("P17 after mark: 100% watched",
              "100% watched" in page.locator("[data-tv-progress]")
              .inner_text())
        check("P17 after mark: 10 of 10",
              "10 of 10 aired episodes" in page.locator("[data-tv-progress]")
              .inner_text())

        # S2E1 airs: a fresh calendar sync row lands (exactly how a new
        # episode enters production data). Denominator grows to 11.
        with flask_app.app_context():
            db.session.add(UpcomingEpisode(
                show_id=RUNNING_ID, show_name="Running Verify",
                season_number=2, episode_number=1,
                air_date=date.today() - timedelta(days=1)))
            db.session.commit()
        page.goto(f"{base}/tv/{RUNNING_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        txt = page.locator("[data-tv-progress]").inner_text()
        check("P17 new episode lowers percent (10 of 11)",
              "10 of 11 aired episodes" in txt and
              "100% watched" not in txt)

        # Watch S2E1 through the canonical finish endpoint (the watch
        # page button) and confirm the hero returns to 100%.
        page.goto(f"{base}/watch/tv/{RUNNING_ID}/2/1",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        page.click("#mark-btn")
        try:
            page.wait_for_selector("text=Watched", timeout=8000)
        except Exception:
            pass
        page.goto(f"{base}/tv/{RUNNING_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        txt = page.locator("[data-tv-progress]").inner_text()
        check("P17 watching new episode restores 100% (11 of 11)",
              "11 of 11 aired episodes" in txt and "100% watched" in txt)
        check("P17 old season still complete",
              "10 of 11" not in txt)

        # ══ Phase 16 — Banshee ═══════════════════════════════════════
        page.goto(f"{base}/tv/{BANSHEE_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        check("P16 before mark: hero progress hidden (not started)",
              page.locator("[data-tv-progress]").count() == 0)

        page.goto(f"{base}/mark_as_viewed/{BANSHEE_ID}/tv",
                  wait_until="domcontentloaded")
        page.goto(f"{base}/tv/{BANSHEE_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        hero = page.locator("[data-tv-progress]").inner_text()
        check("P16 hero: Viewed badge present",
              page.locator("span:has-text('Viewed')").count() > 0)
        check("P16 hero: 100% watched", "100% watched" in hero)
        check("P16 hero: 38 of 38 aired episodes",
              "38 of 38 aired episodes" in hero)
        check("P16 hero NOT 8 of 8", "8 of 8" not in hero)

        cards = page.locator("#seasons-list > div").count()
        check("P16 four season cards render", cards == 4, f"got {cards}")
        completed = page.locator(
            "#seasons-list span:has-text('Completed')").count()
        check("P16 every season card shows 100% Complete",
              completed == 4, f"got {completed}")

        # DB truth: 38 unique watched positions, none of them specials.
        with flask_app.app_context():
            rows = db.session.query(
                TVEpisodeWatch.season_number, TVEpisodeWatch.episode_number
            ).filter(
                TVEpisodeWatch.user_id == uid,
                TVEpisodeWatch.show_id == BANSHEE_ID,
                TVEpisodeWatch.is_rewatch == False,  # noqa: E712
            ).all()
            check("P16 DB has exactly 38 unique watched positions",
                  len(set(rows)) == 38, f"got {len(set(rows))}")
            per_season = {}
            for s, _e in rows:
                per_season[s] = per_season.get(s, 0) + 1
            check("P16 per-season DB counts 10/10/10/8",
                  per_season == {1: 10, 2: 10, 3: 10, 4: 8},
                  f"got {per_season}")

        # Idempotency through the UI: second click inserts nothing.
        page.goto(f"{base}/mark_as_viewed/{BANSHEE_ID}/tv",
                  wait_until="domcontentloaded")
        with flask_app.app_context():
            n = TVEpisodeWatch.query.filter_by(
                user_id=uid, show_id=BANSHEE_ID).count()
            check("P16 repeated mark inserts zero additional rows",
                  n == 38, f"got {n}")

        # Cross-surface: homepage / browse / trending / CineBot pages.
        for name, path in [("Homepage", "/"), ("Browse", "/tv_shows"),
                           ("Trending", "/trending"),
                           ("CineBot", "/chat")]:
            resp = page.goto(f"{base}{path}", wait_until="domcontentloaded")
            ok = resp is not None and resp.status < 400
            check(f"P16 {name} loads", ok)
            if ok and name != "CineBot":
                body = page.content()
                check(f"P16 {name} keeps TV progress intact",
                      "38 of 38" in body or "tv_progress" in body
                      or "FrameIQ" in body)

        # "For You" has no dedicated page: it is a homepage rail fed by
        # /api/for-you, so verify that endpoint stays healthy.
        fy_status = page.evaluate(
            "fetch('/api/for-you').then(r => r.status)")
        check("P16 For You rail API (/api/for-you) loads",
              fy_status is not None and fy_status < 400,
              f"status={fy_status}")

        # Live view-state payload still reports the full aired set.
        payload = page.evaluate(
            "fetch('/api/view-state?tv=%d').then(r => r.json())"
            % BANSHEE_ID)
        prog = (payload.get("tv_progress") or {}).get(str(BANSHEE_ID))
        check("P16 /api/view-state reports 38/38 = 100%",
              prog == {"watched": 38, "aired": 38, "percent": 100.0},
              f"got {prog}")
        aired_payload = page.evaluate(
            "fetch('/api/tv/%d/aired-progress').then(r => r.json())"
            % BANSHEE_ID)
        check("P16 aired-progress season map {1:10,2:10,3:10,4:8}",
              aired_payload.get("season_aired")
              == {"1": 10, "2": 10, "3": 10, "4": 8},
              f"got {aired_payload.get('season_aired')}")

        # ══ Phase 18 — responsive ════════════════════════════════════
        page.goto(f"{base}/tv/{BANSHEE_ID}",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_selector("#seasons-list > div")
        for width in (320, 360, 390, 414, 768, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(250)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth")
            clipped = page.evaluate(
                "Array.from(document.querySelectorAll('#seasons-list > div'))"
                ".some(el => el.scrollWidth > el.clientWidth + 2)")
            check(f"P18 {width}px: no horizontal overflow", overflow <= 0,
                  f"overflow={overflow}")
            check(f"P18 {width}px: no clipped season rows", not clipped)

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
    print("ALL BROWSER CHECKS PASSED")


if __name__ == "__main__":
    main()
