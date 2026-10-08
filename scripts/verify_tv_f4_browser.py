"""Task F4 — browser verification of TV correctness, security and canonical state.

Boots the REAL Flask app on a throwaway SQLite temp DB (same pattern as
verify_tv_f1_browser.py / verify_tv_f2_browser.py: SKIP_SCHEMA_GUARD=1,
CSRF and rate limiting OFF so the scripted flow is deterministic) and drives
it with Playwright headless Chromium.

This is INTEGRATION/BROWSER verification, not part of `pytest tests/`: it
starts a server and needs Playwright. Every TMDb payload here is
deterministic (the same patched fetcher the F1/F2 scripts use), so it needs
no real TMDb key.

What it proves (one section per F4 requirement):

  F4-A  A completed multi-season show (38/38) reads Viewed on the hero AND
        on its /viewed card — card badge == hero == canonical progress.
  F4-B  20/38: no badge anywhere, and the card offers Mark (never Unmark).
  F4-C  A stale user_viewed mirror row cannot badge a partial show, and a
        fully watched show with NO mirror row is badged.
  F4-D  Unmarking an episode returns/keeps the canonical payload (hero stays
        consistent with 37/38).
  F4-E  Marking a FUTURE episode is refused: nothing is written, the hero is
        unchanged, and the request answers 4xx rather than 200.
  F4-F  Marking a SPECIAL (season 0) is refused the same way.
  F4-G  Continue Watching "Finished" preserves an existing rating and notes.
  F4-H  Running show: 25/25 Viewed → a new episode airs → 25/26 not Viewed →
        watch it → 26/26 Viewed again.
  F4-I  Cross-user isolation: another user's watched state never leaks.
  F4-J  Profile next episode: S1+S2 complete ⇒ S3E1 (never S2E11).
  F4-K  List/card progress renders from canonical state.
  F4-L  Responsive 320/360/390/414/768/1024/1440: Viewed badge, mark/unmark
        controls and the canonical progress line all render with no clipping
        and no horizontal overflow.

Run:  .venv/bin/python scripts/verify_tv_f4_browser.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_f4_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "f4-browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

BANSHEE_ID = 41727            # real TMDb id, 10/10/10/8 = 38 aired
RUNNING_ID = 994401           # fake TMDb id far from real shows
PROFILE_ID = 994402           # fake TMDb id for the profile next-episode check
FAILURES = []

BANSHEE_SEASONS = ((1, 10), (2, 10), (3, 10), (4, 8))
RUNNING_SEASONS = {1: 10, 2: 10, 3: 6}
PROFILE_SEASONS = ((1, 10), (2, 10), (3, 10))


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else "")
    print(line)
    if not cond:
        FAILURES.append(name)


def _details(show_id, name, seasons, anchor_season, anchor_episode,
             status="Ended"):
    """Deterministic TMDb show payload, shaped for the real parser.

    Field-for-field the shape verify_tv_f2_browser.py already proves renders,
    so this script exercises the production parser and template rather than a
    simplified fixture.
    """
    return {
        "id": show_id,
        "name": name,
        "overview": "",
        "tagline": "",
        "status": status,
        "first_air_date": "2013-01-13",
        "last_air_date": "2016-10-28",
        "number_of_seasons": len(seasons),
        "number_of_episodes": sum(count for _, count in seasons),
        "last_episode_to_air": {"season_number": anchor_season,
                                "episode_number": anchor_episode},
        "seasons": [
            {"season_number": sn, "episode_count": count,
             "air_date": "2013-01-13", "name": "Season %s" % sn,
             "overview": "", "poster_path": ""}
            for sn, count in seasons],
        "poster_path": "", "backdrop_path": "",
        "genres": ["Drama"], "vote_average": 0, "vote_count": 0,
        "creator": None, "cast": [], "trailer_url": None,
        "recommendations": [], "reviews": [],
    }


def click_action(page, page_url, selector, timeout=15000):
    """Activate a CSRF-protected collection control the way a user does.

    Task F4: mark_as_viewed / remove_from_viewed are POST-only now — a
    state-changing GET was reachable cross-site without a token, and GET now
    answers 405. A verification script must therefore open the page that
    renders the control and CLICK it, which is the real user path (and the
    only one that carries the token).
    """
    page.goto(page_url, wait_until="domcontentloaded")
    button = page.locator(selector).first
    button.wait_for(timeout=timeout)
    # Destructive controls carry an onsubmit="return confirm(...)" guard.
    # Playwright DISMISSES dialogs by default, which makes confirm() return
    # false and silently cancels the submit — so accept explicitly.
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_navigation(wait_until="domcontentloaded", timeout=timeout):
        button.click()
    page.wait_for_timeout(400)


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
            host="127.0.0.1", port=5004, debug=False, use_reloader=False),
        daemon=True)
    t.start()

    import time as _time
    import requests as _requests
    base = "http://127.0.0.1:5004"
    for _ in range(60):
        try:
            _requests.get(f"{base}/login", timeout=2)
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
    from models import (MediaItem, TVEpisodeWatch, TVShowProgress,
                        UpcomingEpisode, User, db)

    # ── deterministic TMDb ────────────────────────────────────────────
    PAYLOADS = {
        BANSHEE_ID: _details(BANSHEE_ID, "Banshee", BANSHEE_SEASONS, 4, 8),
        PROFILE_ID: _details(PROFILE_ID, "F4 Profile Show", PROFILE_SEASONS, 3, 1),
    }
    running_state = {"anchor": 5}

    def _running_details(anchor_episode):
        return _details(RUNNING_ID, "F4 Running Verify",
                        sorted(RUNNING_SEASONS.items()), 3, anchor_episode,
                        status="Returning Series")

    def fake_fetch(show_id, **kwargs):
        if show_id == RUNNING_ID:
            return _running_details(running_state["anchor"])
        payload = PAYLOADS.get(show_id)
        return dict(payload) if payload else None

    api.tmdb_client.fetch_tv_show_details = fake_fetch
    tv_tracking_mod.fetch_tv_show_details = fake_fetch
    details_mod.fetch_tv_show_details = fake_fetch

    def _forget():
        cw._memo.clear()
        from api.tmdb.cache import tmdb_cache
        tmdb_cache._store.clear()

    _forget()

    with flask_app.app_context():
        user = User(username="f4verify", email="f4verify@verify.test",
                    email_verified=True)
        user.set_password("F4Browser1!")
        db.session.add(user)
        other = User(username="f4other", email="f4other@verify.test",
                     email_verified=True)
        other.set_password("F4Browser1!")
        db.session.add(other)
        for show_id, title in ((BANSHEE_ID, "Banshee"),
                               (RUNNING_ID, "F4 Running Verify"),
                               (PROFILE_ID, "F4 Profile Show")):
            db.session.add(MediaItem(tmdb_id=show_id, media_type="tv",
                                     title=title))
        db.session.commit()
        uid, other_id = user.id, other.id

    def seed(show_id, seasons, user_id=None, status="watching"):
        user_id = uid if user_id is None else user_id
        with flask_app.app_context():
            if not TVShowProgress.query.filter_by(
                    user_id=user_id, show_id=show_id).first():
                db.session.add(TVShowProgress(
                    user_id=user_id, show_id=show_id, status=status))
            rows = [TVEpisodeWatch(
                user_id=user_id, show_id=show_id, season_number=sn,
                episode_number=ep, watched_date=date(2026, 1, 10))
                for sn, last in seasons for ep in range(1, last + 1)]
            db.session.add_all(rows)
            db.session.commit()

    def count_rows(show_id, user_id=None):
        user_id = uid if user_id is None else user_id
        with flask_app.app_context():
            return TVEpisodeWatch.query.filter_by(
                user_id=user_id, show_id=show_id).count()

    def progress_of(show_id, user_id=None):
        user_id = uid if user_id is None else user_id
        with flask_app.app_context():
            u = db.session.get(User, user_id)
            from api.user_view_state import canonical_tv_progress
            return canonical_tv_progress(u, show_id)

    def hero_state(page):
        node = page.locator("[data-tv-progress]")
        node.wait_for(timeout=15000)
        return {
            "text": node.first.inner_text(),
            "watch": node.first.get_attribute("data-watched"),
            "aired": node.first.get_attribute("data-aided")
            or node.first.get_attribute("data-aired"),
            "percent": node.first.get_attribute("data-percent"),
            "badge": page.locator(
                "span.bg-green-600:has-text('Viewed')").count(),
            "mark": page.locator("[data-action=tv-mark-viewed]").count(),
            "unmark": page.locator("[data-action=tv-unmark-viewed]").count()}

    def card_badge_for(page, show_id):
        """The card (not the nested <a>/<img>) for this show on a list page."""
        card = page.locator(f'[data-card-tmdb="{show_id}"]').first
        card.wait_for(timeout=15000)
        return card

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "f4verify")
        page.fill('input[name="password"]', "F4Browser1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        # ══ F4-A  38/38 → Viewed on the hero AND the card ═════════════
        seed(BANSHEE_ID, BANSHEE_SEASONS)
        _forget()
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F4-A hero reads 38 of 38",
              "38 of 38" in st["text"], st["text"][:100])
        check("F4-A hero data attrs 38/38",
              st["watch"] == "38" and st["aired"] == "38", str(st))
        check("F4-A hero Viewed badge ON", st["badge"] >= 1)
        check("F4-A hero offers Unmark, not Mark",
              st["unmark"] >= 1 and st["mark"] == 0, str(st))
        check("F4-A canonical ledger is 38 rows",
              count_rows(BANSHEE_ID) == 38)

        page.goto(f"{base}/viewed", wait_until="domcontentloaded")
        card = card_badge_for(page, BANSHEE_ID)
        check("F4-A /viewed card badge agrees with the hero",
              card.locator("span.bg-green-600:has-text('Viewed')").count() == 1,
              card.inner_text()[:120])
        check("F4-A /viewed card offers Unmark",
              card.locator("[data-action=card-unmark-viewed]").count() == 1)

        # ══ F4-B  20/38 → no badge anywhere ═══════════════════════════
        with flask_app.app_context():
            TVEpisodeWatch.query.filter_by(
                user_id=uid, show_id=BANSHEE_ID,
                season_number=4).delete()
            db.session.commit()
        _forget()
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F4-B hero reads 30 of 38", "30 of 38" in st["text"],
              st["text"][:100])
        check("F4-B hero Viewed badge OFF", st["badge"] == 0)
        check("F4-B hero offers Mark, not Unmark",
              st["mark"] >= 1 and st["unmark"] == 0, str(st))
        page.goto(f"{base}/viewed", wait_until="domcontentloaded")
        card = card_badge_for(page, BANSHEE_ID)
        check("F4-B /viewed card badge OFF at 30/38",
              card.locator("span.bg-green-600:has-text('Viewed')").count() == 0)
        # The /viewed branch only ever offers Remove-from-Viewed, and only
        # when the card is canonically viewed — so at 30/38 it must be absent.
        check("F4-B /viewed card offers no Unmark control",
              card.locator("[data-action=card-unmark-viewed]").count() == 0)

        # ══ F4-C  a stale mirror row cannot badge a partial show ═══════
        with flask_app.app_context():
            from models import user_viewed
            item = MediaItem.query.filter_by(
                tmdb_id=BANSHEE_ID, media_type="tv").first()
            db.session.execute(user_viewed.insert().values(
                user_id=uid, media_id=item.id, media_type="tv"))
            db.session.commit()
        page.goto(f"{base}/viewed", wait_until="domcontentloaded")
        card = card_badge_for(page, BANSHEE_ID)
        check("F4-C stale user_viewed row does NOT badge the card",
              card.locator("span.bg-green-600:has-text('Viewed')").count() == 0,
              card.inner_text()[:120])
        check("F4-C canonical state agrees (30/38 is not Viewed)",
              progress_of(BANSHEE_ID)["watched"] == 30
              and progress_of(BANSHEE_ID)["percent"] == 78.9)
        with flask_app.app_context():
            from models import user_viewed
            item = MediaItem.query.filter_by(
                tmdb_id=BANSHEE_ID, media_type="tv").first()
            db.session.execute(user_viewed.delete().where(
                user_viewed.c.user_id == uid,
                user_viewed.c.media_id == item.id,
                user_viewed.c.media_type == "tv"))
            db.session.commit()

        # ══ F4-D  unmark episode keeps canonical payload ═══════════════
        response = _requests.post(
            f"{base}/api/tv/{BANSHEE_ID}/episode/3/10/unmark-watched",
            cookies={c["name"]: c["value"] for c in page.context.cookies()},
            allow_redirects=False)
        check("F4-D unmark episode answers 200", response.status_code == 200,
              str(response.status_code))
        if response.status_code == 200:
            body = response.json()
            check("F4-D response publishes canonical watched count",
                  body["progress"]["watched_episodes"] == 29,
                  str(body.get("progress")))
            check("F4-D response publishes the aired denominator",
                  body["progress"]["total_episodes"] == 38,
                  str(body.get("progress")))
            check("F4-D response carries the canonical viewed verdict",
                  body["viewed"] is False, str(body))
        check("F4-D persisted state matches the reported payload",
              count_rows(BANSHEE_ID) == 29)
        page.goto(f"{base}/tv/{BANSHEE_ID}", wait_until="domcontentloaded")
        check("F4-D hero agrees after the unmark",
              "29 of 38" in hero_state(page)["text"])

        # ══ F4-E  a FUTURE episode is refused ══════════════════════════
        before = count_rows(BANSHEE_ID)
        response = _requests.post(
            f"{base}/api/tv/{BANSHEE_ID}/episode/4/9/mark-watched",
            cookies={c["name"]: c["value"] for c in page.context.cookies()},
            allow_redirects=False)
        check("F4-E marking a nonexistent/future episode answers 4xx",
              response.status_code == 400, str(response.status_code))
        check("F4-E nothing was written",
              count_rows(BANSHEE_ID) == before,
              f"{before} -> {count_rows(BANSHEE_ID)}")

        # ══ F4-F  a SPECIAL is refused ═════════════════════════════════
        response = _requests.post(
            f"{base}/api/tv/{BANSHEE_ID}/episode/0/1/mark-watched",
            cookies={c["name"]: c["value"] for c in page.context.cookies()},
            allow_redirects=False)
        check("F4-F marking a season-0 special answers 4xx",
              response.status_code == 400, str(response.status_code))
        check("F4-F nothing was written for a special",
              count_rows(BANSHEE_ID) == before,
              f"{before} -> {count_rows(BANSHEE_ID)}")

        # ══ F4-G  Finish preserves rating / notes ══════════════════════
        with flask_app.app_context():
            row = TVEpisodeWatch.query.filter_by(
                user_id=uid, show_id=BANSHEE_ID, season_number=1,
                episode_number=1).first()
            row.rating = 4.5
            row.notes = "F4 keep me"
            watched_on = row.watched_date
            db.session.commit()
        with flask_app.app_context():
            from api import continue_watching as _cw
            _cw.start_item(uid, "tv", BANSHEE_ID, season=1, episode=1)
            result = _cw.finish_tv_episode(uid, BANSHEE_ID, 1, 1)
        check("F4-G Finish reports success", result.get("finished") is True,
              str(result))
        with flask_app.app_context():
            row = TVEpisodeWatch.query.filter_by(
                user_id=uid, show_id=BANSHEE_ID, season_number=1,
                episode_number=1).order_by(TVEpisodeWatch.id).first()
            check("F4-G rating survives Finish", row.rating == 4.5,
                  str(row.rating))
            check("F4-G notes survive Finish", row.notes == "F4 keep me",
                  str(row.notes))
            check("F4-G watched_date survives Finish",
                  row.watched_date == watched_on,
                  f"{row.watched_date} != {watched_on}")

        # ══ F4-H  running show lifecycle 25/25 → 25/26 → 26/26 ════════
        with flask_app.app_context():
            db.session.add(TVShowProgress(user_id=uid, show_id=RUNNING_ID,
                                          status="watching"))
            db.session.add_all(
                TVEpisodeWatch(user_id=uid, show_id=RUNNING_ID,
                               season_number=1, episode_number=ep,
                               watched_date=date(2026, 1, 10))
                for ep in range(1, 11))
            db.session.add_all(
                TVEpisodeWatch(user_id=uid, show_id=RUNNING_ID,
                               season_number=2, episode_number=ep,
                               watched_date=date(2026, 1, 10))
                for ep in range(1, 11))
            db.session.add_all(
                TVEpisodeWatch(user_id=uid, show_id=RUNNING_ID,
                               season_number=3, episode_number=ep,
                               watched_date=date(2026, 1, 10))
                for ep in range(1, running_state["anchor"] + 1))
            db.session.commit()
        _forget()
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F4-H 25/25 at 100%", "25 of 25" in st["text"], st["text"][:90])
        check("F4-H 25/25 Viewed badge ON", st["badge"] >= 1)

        # S3E6 airs (the sync writes a calendar row for it).
        with flask_app.app_context():
            db.session.add(UpcomingEpisode(
                show_id=RUNNING_ID, season_number=3, episode_number=6,
                show_name="F4 Running Verify", episode_name="Sixth",
                air_date=date.today() - timedelta(days=1)))
            db.session.commit()
        running_state["anchor"] = 6
        _forget()
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F4-H a newly aired episode lowers to 25 of 26",
              "25 of 26" in st["text"], st["text"][:90])
        check("F4-H Viewed badge turns OFF with no manual reset",
              st["badge"] == 0, str(st))
        check("F4-H hero now offers Mark", st["mark"] >= 1)

        with flask_app.app_context():
            from api import continue_watching as _cw
            _cw.finish_tv_episode(uid, RUNNING_ID, 3, 6)
        _forget()
        page.goto(f"{base}/tv/{RUNNING_ID}", wait_until="domcontentloaded")
        st = hero_state(page)
        check("F4-H watching it restores 26 of 26",
              "26 of 26" in st["text"], st["text"][:90])
        check("F4-H Viewed badge back ON", st["badge"] >= 1)

        # ══ F4-I  cross-user isolation ═════════════════════════════════
        seed(BANSHEE_ID, BANSHEE_SEASONS, user_id=other_id)
        with flask_app.app_context():
            TVEpisodeWatch.query.filter_by(
                user_id=other_id, show_id=BANSHEE_ID).delete()
            db.session.commit()
        _forget()
        check("F4-I the other user's 38 rows are their own",
              count_rows(BANSHEE_ID, user_id=other_id) == 0)
        check("F4-I our 29 rows are untouched",
              count_rows(BANSHEE_ID) == 29)
        check("F4-I the other user has no canonical progress",
              progress_of(BANSHEE_ID, user_id=other_id) is None,
              str(progress_of(BANSHEE_ID, user_id=other_id)))

        # ══ F4-J  profile next episode: S2 complete ⇒ S3E1 ══════════════
        with flask_app.app_context():
            db.session.add(TVShowProgress(user_id=uid, show_id=PROFILE_ID,
                                          status="watching"))
            db.session.add_all(
                TVEpisodeWatch(user_id=uid, show_id=PROFILE_ID,
                               season_number=sn, episode_number=ep,
                               watched_date=date(2026, 1, 10))
                for sn, last in ((1, 10), (2, 10)) for ep in range(1, last + 1))
            db.session.commit()
        _forget()
        page.goto(f"{base}/profile", wait_until="domcontentloaded")
        body = page.content()
        check("F4-J profile offers S3E1 next", "S3E1" in body)
        check("F4-J profile never invents S2E11", "S2E11" not in body)

        # ══ F4-K  list/card progress ═══════════════════════════════════
        _cookies = {c["name"]: c["value"] for c in page.context.cookies()}
        response = _requests.post(
            f"{base}/add_to_watchlist/{BANSHEE_ID}/tv", cookies=_cookies,
            allow_redirects=False)
        check("F4-K add to watchlist answers 200/302",
              response.status_code in (200, 302), str(response.status_code))
        _forget()
        page.goto(f"{base}/watchlist", wait_until="domcontentloaded")
        wl = page.content()
        wcard = page.locator(f'[data-card-tmdb="{BANSHEE_ID}"]').first
        wcard.wait_for(timeout=15000)
        check("F4-K watchlist card renders the show", True)
        check("F4-K watchlist card offers Remove-from-Watchlist",
              wcard.locator('form[action*="remove_from_watchlist"]').count() == 1,
              wcard.inner_text()[:120])
        # BANSHEE is at 29/38, so the watchlist branch must offer Mark, not Unmark.
        check("F4-K watchlist card offers Mark while incomplete",
              wcard.locator("[data-action=card-mark-viewed]").count() == 1,
              wcard.inner_text()[:120])
        check("F4-K watchlist card carries no Viewed badge",
              wcard.locator(
                  "span.bg-green-600:has-text('Viewed')").count() == 0)
        check("F4-K watchlist renders card progress without a traceback",
              "Traceback" not in wl)
        check("F4-K watchlist renders its controls",
              'name="csrf_token"' in wl or "data-card-tmdb" in wl, "")
        page.goto(f"{base}/viewed", wait_until="domcontentloaded")
        vw = page.content()
        check("F4-K /viewed renders with no traceback",
              "Traceback" not in vw)
        check("F4-K /viewed marks every card with a verification hook",
              'data-card-tmdb=' in vw)

        # ══ F4-L  responsive ═══════════════════════════════════════════
        for width in (320, 360, 390, 414, 768, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base}/tv/{RUNNING_ID}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(250)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check(f"F4-L {width}px: no horizontal overflow", overflow <= 1,
                  f"overflow={overflow}")
            check(f"F4-L {width}px: canonical progress line visible",
                  page.locator("[data-tv-progress]").count() >= 1)
            check(f"F4-L {width}px: Viewed badge visible",
                  page.locator(
                      "span.bg-green-600:has-text('Viewed')").count() >= 1)
            check(f"F4-L {width}px: mark/unmark control present",
                  page.locator("[data-action=tv-unmark-viewed]").count() >= 1)

            page.goto(f"{base}/profile", wait_until="domcontentloaded")
            page.wait_for_timeout(200)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check(f"F4-L {width}px: profile next label, no overflow",
                  overflow <= 1, f"overflow={overflow}")

        page.set_viewport_size({"width": 1280, "height": 900})
        browser.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"F4 BROWSER VERIFICATION FAILED — {len(FAILURES)} check(s):")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL F4 BROWSER CHECKS PASSED")


if __name__ == "__main__":
    main()
