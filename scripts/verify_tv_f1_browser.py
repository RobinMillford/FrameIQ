"""Task F1 — browser verification of the UNIFIED TV write paths.

Boots the REAL Flask app on a throwaway SQLite temp DB (conftest pattern:
SKIP_SCHEMA_GUARD=1) and drives it with Playwright headless Chromium.

Unlike scripts/verify_tv_browser.py (Task E — hero/season-card read side),
this script exercises the WRITE controls end-to-end through the UI:

  F1-A  Banshee (TMDb 41727, real data 10/10/10/8 = 38 aired):
        seed S2E1 with rating+notes → click "Mark Season Watched" on the
        season-2 card → only the 9 missing aired episodes are inserted,
        the existing row survives byte-for-byte, future episodes (none
        aired beyond the anchor) are never touched.
  F1-B  "Mark All Watched" → 38/38 hero, every season card Completed,
        38 unique DB positions; second click inserts nothing (idempotent).
  F1-C  "Unmark Season" (season 4) → 30/38 everywhere; card not completed.
  F1-D  Reload + navigate away/back → identical state (persisted).
  F1-E  Running show (deterministic patched TMDb fixture 10/10/5→6):
        Mark All → 25/25 100% → S3E6 airs → 25/26 96.2% → watch it →
        26/26 100% — all through /progress and the hero line.
  F1-F  Responsive check 320/360/390/414/768/1024/1440 with updated counts.

Run:  .venv/bin/python scripts/verify_tv_f1_browser.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_f1_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "f1-browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

BANSHEE_ID = 41727
RUNNING_ID = 991888          # fake TMDb id far from real shows
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
            host="127.0.0.1", port=5002, debug=False, use_reloader=False),
        daemon=True)
    t.start()

    import time as _time
    import requests as _requests
    for _ in range(60):
        try:
            _requests.get("http://127.0.0.1:5002/login", timeout=2)
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

    # ── Deterministic running-show fixture (S1=10, S2=10, S3 partial) ──
    _real_fetch = api.tmdb_client.fetch_tv_show_details
    cw._memo.clear()

    RUNNING_SEASONS = {1: 10, 2: 10, 3: 12}

    def _running_details(anchor_ep):
        return {
            "id": RUNNING_ID, "name": "F1 Running Verify",
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
        if show_id != RUNNING_ID:
            return _real_fetch(show_id, **kw)
        return _running_details(5)

    api.tmdb_client.fetch_tv_show_details = _patched_fetch
    details_mod.fetch_tv_show_details = _patched_fetch
    tv_tracking_mod.fetch_tv_show_details = _patched_fetch

    with flask_app.app_context():
        user = User(username="f1verify", email="f1verify@example.com",
                    email_verified=True)
        user.set_password("F1verify1!")
        db.session.add(user)
        db.session.add(MediaItem(tmdb_id=RUNNING_ID, media_type="tv",
                                 title="F1 Running Verify"))
        db.session.commit()
        uid = user.id

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        base = "http://127.0.0.1:5002"
        page.on("dialog", lambda dialog: dialog.accept())

        # ── login ─────────────────────────────────────────────────────
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "f1verify")
        page.fill('input[name="password"]', "F1verify1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        def progress_payload(show_id):
            return page.evaluate(
                "fetch('/api/tv/%d/progress').then(r => r.json())"
                % show_id)["progress"]

        def db_positions(show_id):
            with flask_app.app_context():
                rows = db.session.query(
                    TVEpisodeWatch.season_number,
                    TVEpisodeWatch.episode_number,
                    TVEpisodeWatch.rating,
                    TVEpisodeWatch.notes,
                ).filter(
                    TVEpisodeWatch.user_id == uid,
                    TVEpisodeWatch.show_id == show_id,
                    TVEpisodeWatch.is_rewatch == False,  # noqa: E712
                ).all()
                return {(s, e): (r, n) for s, e, r, n in rows}

        # ══ F1-A: Mark Season Watched preserves existing metadata ════
        with flask_app.app_context():
            seed = TVEpisodeWatch(
                user_id=uid, show_id=BANSHEE_ID, season_number=2,
                episode_number=1, rating=4.5, notes="my note",
                watched_date=date(2026, 1, 15))
            db.session.add(seed)
            db.session.commit()
            seed_id = seed.id

        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        page.wait_for_selector("#seasons-list > div")
        season2_card = page.locator(
            "#seasons-list > div", has_text="Season 2").first
        season2_card.locator(
            "button:has-text('Mark Season Watched')").click()
        season2_card.locator("span:has-text('Completed')").wait_for(
            timeout=10000)

        positions = db_positions(BANSHEE_ID)
        s2 = {pos for pos in positions if pos[0] == 2}
        check("F1-A season 2: 10/10 watched via card button",
              len(s2) == 10, f"got {sorted(s2)}")
        check("F1-A existing S2E1 row preserved (same id, rating, notes)",
              (2, 1) in positions
              and positions[(2, 1)] == (4.5, "my note"))
        with flask_app.app_context():
            kept = db.session.get(TVEpisodeWatch, seed_id)
            check("F1-A seeded row not deleted/recreated",
                  kept is not None and kept.id == seed_id
                  and kept.rating == 4.5 and kept.notes == "my note"
                  and kept.watched_date.isoformat() == "2026-01-15")
            others = TVEpisodeWatch.query.filter(
                TVEpisodeWatch.user_id == uid,
                TVEpisodeWatch.show_id == BANSHEE_ID,
                TVEpisodeWatch.season_number != 2).count()
            check("F1-A other seasons untouched (10 total rows)",
                  len(positions) == 10 and others == 0,
                  f"rows={len(positions)} others={others}")

        # ══ F1-B: Mark All Watched → 38/38, idempotent ════════════════
        page.evaluate(
            "document.getElementById('mark-all-watched-btn').click()")
        page.wait_for_timeout(1500)

        hero = page.locator("[data-tv-progress]")
        hero.wait_for(timeout=10000)
        hero_text = hero.inner_text()
        check("F1-B hero: 100% watched", "100% watched" in hero_text)
        check("F1-B hero: 38 of 38 aired episodes",
              "38 of 38 aired episodes" in hero_text, hero_text[:120])
        positions = db_positions(BANSHEE_ID)
        check("F1-B DB: exactly 38 unique canonical positions",
              len(positions) == 38, f"got {len(positions)}")
        per_season = {}
        for (s, _e) in positions:
            per_season[s] = per_season.get(s, 0) + 1
        check("F1-B per-season 10/10/10/8",
              per_season == {1: 10, 2: 10, 3: 10, 4: 8}, f"{per_season}")
        completed = page.locator(
            "#seasons-list span:has-text('Completed')").count()
        check("F1-B every season card Completed", completed == 4,
              f"got {completed}")
        p = progress_payload(BANSHEE_ID)
        check("F1-B /progress canonical: 38/38 100%",
              p["total_episodes"] == 38 and p["watched_episodes"] == 38
              and p["progress_percentage"] == 100.0, f"{p}")

        # Idempotency through the UI.
        page.evaluate(
            "document.getElementById('mark-all-watched-btn').click()")
        page.wait_for_timeout(1500)
        check("F1-B repeated Mark All inserts nothing",
              len(db_positions(BANSHEE_ID)) == 38)

        # ══ F1-C: Unmark Season (season 4) ════════════════════════════
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        page.wait_for_selector("#seasons-list > div")
        season4_card = page.locator(
            "#seasons-list > div", has_text="Season 4").first
        season4_card.locator("button:has-text('Unmark Season')").click()
        page.wait_for_timeout(1500)

        positions = db_positions(BANSHEE_ID)
        check("F1-C season 4 rows deleted (30 remain)",
              len(positions) == 30
              and not any(pos[0] == 4 for pos in positions),
              f"got {len(positions)}")
        p = progress_payload(BANSHEE_ID)
        check("F1-C /progress canonical after unmark: 30/38 78.9%",
              p["total_episodes"] == 38 and p["watched_episodes"] == 30
              and p["progress_percentage"] == 78.9, f"{p}")

        # ══ F1-D: reload + navigate away/back persistence ════════════
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#seasons-list > div")
        hero = page.locator("[data-tv-progress]")
        hero.wait_for(timeout=10000)
        check("F1-D after reload: 30 of 38 persists",
              "30 of 38 aired episodes" in hero.inner_text())
        page.goto(f"{base}/", wait_until="domcontentloaded")
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        page.wait_for_selector("#seasons-list > div")
        hero = page.locator("[data-tv-progress]")
        hero.wait_for(timeout=10000)
        check("F1-D navigate away/back: 30 of 38 persists",
              "30 of 38 aired episodes" in hero.inner_text())

        # ══ F1-E: running show 25/25 → 25/26 → 26/26 ═════════════════
        page.goto(f"{base}/mark_as_viewed/{RUNNING_ID}/tv",
                  wait_until="domcontentloaded")
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        hero = page.locator("[data-tv-progress]")
        hero.wait_for(timeout=10000)
        check("F1-E running show after mark: 25 of 25 = 100%",
              "25 of 25 aired episodes" in hero.inner_text()
              and "100% watched" in hero.inner_text())

        # S3E6 airs mid-flow (calendar sync row, exactly like production).
        with flask_app.app_context():
            db.session.add(UpcomingEpisode(
                show_id=RUNNING_ID, show_name="F1 Running Verify",
                season_number=3, episode_number=6,
                air_date=date.today() - timedelta(days=1)))
            db.session.commit()
        page.reload(wait_until="domcontentloaded")
        hero = page.locator("[data-tv-progress]")
        hero.wait_for(timeout=10000)
        txt = hero.inner_text()
        check("F1-E new episode lowers percent (25 of 26)",
              "25 of 26 aired episodes" in txt and "100%" not in txt,
              txt[:120])
        p = progress_payload(RUNNING_ID)
        check("F1-E /progress: 25/26 96.2%",
              p["total_episodes"] == 26 and p["watched_episodes"] == 25
              and p["progress_percentage"] == 96.2, f"{p}")

        # Watch S3E6 through the watch-page finish button.
        page.goto(f"{base}/watch/tv/{RUNNING_ID}/3/6",
                  wait_until="domcontentloaded")
        page.wait_for_load_state("domcontentloaded")
        page.click("#mark-btn")
        page.wait_for_timeout(2000)
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        hero = page.locator("[data-tv-progress]")
        hero.wait_for(timeout=10000)
        txt = hero.inner_text()
        check("F1-E watching new episode restores 100% (26 of 26)",
              "26 of 26 aired episodes" in txt and "100% watched" in txt,
              txt[:120])

        # ══ F1-F: responsive regression (updated counts on screen) ═══
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
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
            check(f"F1-F {width}px: no horizontal overflow", overflow <= 0,
                  f"overflow={overflow}")
            check(f"F1-F {width}px: no clipped season rows", not clipped)

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
    print("ALL F1 BROWSER CHECKS PASSED")


if __name__ == "__main__":
    main()
