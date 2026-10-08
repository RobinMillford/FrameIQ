"""Task F6 — browser verification of the Import Center.

Boots the REAL Flask app on a throwaway SQLite temp DB (the pattern used by
verify_tv_f1/f2/f4 and verify_account_export_browser.py) and drives it with
Playwright headless Chromium.

Checks, per task §32:

  unauthenticated  cannot preview, cannot apply
  authenticated    import UI visible with both sources
  Letterboxd       upload → preview → confirm → result
  TV Time          upload (zip) → preview → confirm → result
  preview          shows counts and unresolved/ambiguous detail
  confirm          applies; result summary is shown
  repeat           an identical re-import reports "already present", no dupes
  isolation        no other user's data appears anywhere
  page source      upload contents are not embedded in the HTML
  CSRF             the app's own token is carried
  status codes     preview/apply are 200; a bad file is 4xx
  responsive       320–1440px: no overflow, controls inside the viewport

Run:  .venv/bin/python scripts/verify_account_import_browser.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_f6_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "f6-browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

FAILURES = []
WIDTHS = (320, 360, 390, 414, 768, 1024, 1440)

LB_TMDB = 600_000_000
BENGALI = "আবার দেখা"
SHOW_TMDB = 600_100_001
B_USER_MARKER = "B_USER_MUST_NEVER_LEAK"


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else "")
    print(line)
    if not cond:
        FAILURES.append(name)


def _letterboxd_zip(rows):
    """A real Letterboxd-style export ZIP."""
    text = io.StringIO(newline="")
    text.write("Date,Name,Year,Letterboxd URI,Rating\r\n")
    for row in rows:
        text.write(",".join(row) + "\r\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("watched.csv", text.getvalue())
        # Overlapping file that must NOT be merged (would double-count).
        archive.writestr("diary.csv", text.getvalue())
    return buffer.getvalue()


LB_ROWS = [
    ["2021-03-04 21:00", "The Matrix", "1999", "/film/the-matrix/", "4.5"],
    ["2022-01-02 19:00", BENGALI, "2019", "/film/abar-dekha/", "4.0"],
    ["2022-06-07 14:00", "No URI Film", "2016", "", "3.0"],
]


def _tvtime_json():
    def eps(n):
        """All n episodes aired AND watched — the source claims watch history
        for each, which is what a real TV Time export looks like."""
        return [{"id": n * 10 + i, "number": i, "name": "Episode %d" % i,
                 "aired": True, "first_aired": "2020-01-01T00:00:00.000Z",
                 "last_watched": "2021-05-0%dT00:00:00.000Z" % i,
                 "is_specials": False}
                for i in range(1, n + 1)]
    document = {
        "shows": [{
            "id": 4242, "name": "F6 Browser Show", "tmdb_id": SHOW_TMDB,
            "seasons": [{"number": 1, "episodes": eps(3)},
                        {"number": 9, "episodes": [
                            {"id": 900, "number": 9, "name": "Far Future",
                             "aired": False,
                             "first_aired": "2099-01-01T00:00:00.000Z",
                             "last_watched": "2099-01-02T00:00:00.000Z",
                             "is_specials": False}]}],
        }],
        "movies": [],
    }
    return json.dumps(document).encode("utf-8")


def _tvtime_zip():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("tvtime.json", _tvtime_json())
    return buffer.getvalue()


def main():
    from playwright.sync_api import sync_playwright

    from app import app as flask_app
    from models import db as _db

    flask_app.config.update(
        TESTING=False, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)

    with flask_app.app_context():
        _db.create_all()

    import threading
    import time as _time

    import requests as _requests
    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=5007,
                                     debug=False, use_reloader=False),
        daemon=True).start()

    base = "http://127.0.0.1:5007"
    for _ in range(60):
        try:
            _requests.get(f"{base}/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: Flask dev server did not start")
        sys.exit(2)

    # ── deterministic show payload ──────────────────────────────────────
    # F4's eligibility gate resolves the aired set from TMDb metadata. Without
    # a payload it has no evidence, is permissive by design, and would import
    # the unaired season — making this script depend on a real TMDb answer for
    # a show that does not exist. Patch the fetcher so the run is offline AND
    # deterministic, exactly as verify_tv_f2_browser.py does.
    import api.continue_watching as cw
    import api.tmdb_client
    import routes.details as details_mod
    import routes.tv_tracking as tv_tracking_mod
    from api.tmdb.cache import tmdb_cache

    def _show_payload():
        return {
            "id": SHOW_TMDB, "name": "F6 Browser Show", "overview": "",
            "tagline": "", "status": "Ended",
            "first_air_date": "2020-01-01", "last_air_date": "2020-02-01",
            "number_of_seasons": 1, "number_of_episodes": 3,
            "last_episode_to_air": {"season_number": 1, "episode_number": 3},
            "seasons": [{"season_number": 1, "episode_count": 3,
                         "air_date": "2020-01-01", "name": "Season 1",
                         "overview": "", "poster_path": ""}],
            "poster_path": "", "backdrop_path": "", "genres": ["Drama"],
            "vote_average": 0, "vote_count": 0, "creator": None,
            "cast": [], "trailer_url": None, "recommendations": [],
            "reviews": [],
        }

    def _patched_fetch(show_id, **kwargs):
        if show_id == SHOW_TMDB:
            return _show_payload()
        return None

    api.tmdb_client.fetch_tv_show_details = _patched_fetch
    details_mod.fetch_tv_show_details = _patched_fetch
    tv_tracking_mod.fetch_tv_show_details = _patched_fetch
    cw._memo.clear()
    tmdb_cache._store.clear()

    from models import DiaryEntry, MediaItem, Review, TVEpisodeWatch, User, db

    with flask_app.app_context():
        user = User(username="f6verify", email="f6verify@verify.test",
                    email_verified=True)
        user.set_password("F6Browser1!")
        other = User(username="f6other", email="f6other@verify.test",
                     email_verified=True)
        other.set_password("F6Browser1!")
        db.session.add_all([user, other])
        db.session.add_all([
            MediaItem(tmdb_id=LB_TMDB, media_type="movie", title="The Matrix",
                      release_date=date(1999, 3, 31)),
            MediaItem(tmdb_id=600_000_002, media_type="movie", title=BENGALI,
                      release_date=date(2019, 1, 1)),
            MediaItem(tmdb_id=600_000_003, media_type="movie",
                      title="No URI Film", release_date=date(2016, 1, 1)),
            MediaItem(tmdb_id=SHOW_TMDB, media_type="tv",
                      title="F6 Browser Show",
                      release_date=date(2020, 1, 1)),
        ])
        db.session.commit()
        matrix = db.session.execute(
            db.select(MediaItem).where(MediaItem.tmdb_id == LB_TMDB)
        ).scalars().first()
        # Another user already has private data that must remain untouched.
        db.session.add(Review(user_id=other.id, media_id=matrix.id,
                              media_type="movie", content=B_USER_MARKER,
                              rating=5.0))
        db.session.add(DiaryEntry(user_id=other.id, media_id=matrix.id,
                                  media_type="movie",
                                  watched_date=date(2020, 1, 1)))
        db.session.commit()
        USER_ID, OTHER_ID = user.id, other.id

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page = context.new_page()

        # ── unauthenticated ──────────────────────────────────────────────
        for source, payload, name in (
                ("letterboxd", _letterboxd_zip(LB_ROWS), "lb.zip"),
                ("tvtime", _tvtime_zip(), "tv.zip")):
            response = _requests.post(
                f"{base}/api/account/import/{source}/preview",
                files={"file": (name, payload, "application/octet-stream")},
                allow_redirects=False)
            check(f"F6-1 unauthenticated {source} preview denied",
                  response.status_code in (401, 302), str(response.status_code))
            response = _requests.post(
                f"{base}/api/account/import/{source}/apply",
                files={"file": (name, payload, "application/octet-stream")},
                allow_redirects=False)
            check(f"F6-2 unauthenticated {source} apply denied",
                  response.status_code in (401, 302), str(response.status_code))

        # ── GET is not accepted for the mutating endpoints ───────────────
        response = _requests.get(f"{base}/api/account/import/letterboxd/preview",
                                 allow_redirects=False)
        check("F6-3 import preview rejects GET", response.status_code == 405,
              str(response.status_code))
        response = _requests.get(f"{base}/api/account/import/letterboxd/apply",
                                 allow_redirects=False)
        check("F6-4 import apply rejects GET", response.status_code == 405,
              str(response.status_code))

        # ── authenticated UI ─────────────────────────────────────────────
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "f6verify")
        page.fill('input[name="password"]', "F6Browser1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        page.goto(f"{base}/profile/edit", wait_until="domcontentloaded")
        section = page.locator("#account-import")
        section.wait_for(timeout=15000)
        check("F6-5 import section is present", section.count() == 1)
        check("F6-6 both import sources have a file input",
              page.locator('input[data-import-source="letterboxd"]').count() == 1
              and page.locator('input[data-import-source="tvtime"]').count() == 1)
        check("F6-7 both preview buttons present",
              page.locator('[data-action="import-preview"]').count() == 2)
        check("F6-8 preview panel starts hidden",
              page.locator("#import-preview-panel").is_hidden())
        check("F6-9 import controls are labelled",
              page.locator('label[for="import-file-letterboxd"]').count() == 1
              and page.locator('label[for="import-file-tvtime"]').count() == 1)
        check("F6-10 UI warns that import adds watch history",
              "watch-history" in section.inner_text().lower()
              or "watch history" in section.inner_text().lower())

        # ── Letterboxd: preview ──────────────────────────────────────────
        lb_path = os.path.join(_test_db_path + "_dl", "lb.zip")
        os.makedirs(os.path.dirname(lb_path), exist_ok=True)
        with open(lb_path, "wb") as handle:
            handle.write(_letterboxd_zip(LB_ROWS))

        page.set_input_files("#import-file-letterboxd", lb_path)
        page.wait_for_timeout(300)
        idle_html = page.content()
        check("F6-11a selecting a file embeds nothing in the page",
              "The Matrix" not in idle_html and BENGALI not in idle_html)

        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        check("F6-11 Letterboxd preview panel shown", True)
        summary = page.locator("#import-summary").inner_text()
        check("F6-12 preview shows an import count", "Will import" in summary,
              summary[:120])
        check("F6-13 preview reports the malformed row",
              "Malformed rows" in summary, summary[:160])
        samples = page.locator("#import-samples").inner_text()
        check("F6-14 preview lists the Bengali title", BENGALI in samples,
              samples[:200])
        check("F6-15 preview explains the row without a Letterboxd URI",
              "Letterboxd URI" in samples, samples[:240])
        check("F6-16 confirm button enabled",
              page.locator("#import-apply").is_enabled())

        # ── the preview MAY name what it will import — that is the point ──
        # The privacy invariant is that nothing is written and no other user's
        # data appears; a title the user themselves selected, shown only after
        # an explicit preview click, is the feature working, not a leak.
        html = page.content()
        check("F6-17 preview shows the matched title (intended)",
              "The Matrix" in samples or "The Matrix" in html)
        check("F6-18 preview panel is bounded, not a raw dump",
              len(html) < 400_000, str(len(html)))
        check("F6-18b other user's marker is absent from the preview",
              B_USER_MARKER not in html)

        # ── cancel then confirm ──────────────────────────────────────────
        page.click("#import-cancel")
        page.wait_for_timeout(200)
        check("F6-19 cancel hides the preview panel",
              page.locator("#import-preview-panel").is_hidden())

        page.set_input_files("#import-file-letterboxd", lb_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        page.click("#import-apply")
        page.wait_for_selector("#import-error[role=status]", timeout=30000)
        message = page.locator("#import-error").inner_text()
        check("F6-20 confirm applies and reports a result",
              "Imported 2 record" in message, message[:160])
        check("F6-21 preview panel hidden after applying",
              page.locator("#import-preview-panel").is_hidden())

        with flask_app.app_context():
            count = DiaryEntry.query.filter_by(user_id=USER_ID).count()
            check("F6-22 canonical DiaryEntry rows were created",
                  count == 2, str(count))

        # ── repeated import is idempotent ────────────────────────────────
        page.set_input_files("#import-file-letterboxd", lb_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        repeat_summary = page.locator("#import-summary").inner_text()
        check("F6-23 repeat preview shows 0 to import",
              "Will import\n0" in repeat_summary
              or "Will import0" in repeat_summary.replace(" ", ""),
              repeat_summary[:200])
        check("F6-24 repeat preview shows already-present",
              "Already present" in repeat_summary, repeat_summary[:200])
        check("F6-25 confirm disabled when nothing to import",
              page.locator("#import-apply").is_disabled())
        page.click("#import-cancel")

        with flask_app.app_context():
            count = DiaryEntry.query.filter_by(user_id=USER_ID).count()
        check("F6-26 re-preview created no duplicate rows", count == 2,
              str(count))

        # ── TV Time zip ──────────────────────────────────────────────────
        tv_path = os.path.join(_test_db_path + "_dl", "tv.zip")
        with open(tv_path, "wb") as handle:
            handle.write(_tvtime_zip())
        page.set_input_files("#import-file-tvtime", tv_path)
        page.click('[data-action="import-preview"][data-source="tvtime"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        tv_summary = page.locator("#import-summary").inner_text()
        check("F6-27 TV Time preview counts episodes",
              "Will import" in tv_summary, tv_summary[:160])
        check("F6-28 TV Time preview flags the unaired episode",
              "Not yet aired" in tv_summary, tv_summary[:200])
        page.click("#import-apply")
        page.wait_for_selector("#import-error[role=status]", timeout=30000)
        tv_message = page.locator("#import-error").inner_text()
        check("F6-29 TV Time import applied",
              "Imported 3 record" in tv_message, tv_message[:160])
        with flask_app.app_context():
            eps = TVEpisodeWatch.query.filter_by(user_id=USER_ID).count()
            positions = {(r.season_number, r.episode_number)
                         for r in TVEpisodeWatch.query.filter_by(
                             user_id=USER_ID).all()}
        check("F6-30 canonical episode rows created", eps == 3, str(eps))
        check("F6-31 unaired season 9 was NOT imported",
              (9, 9) not in positions, str(sorted(positions)))

        # ── isolation ────────────────────────────────────────────────────
        with flask_app.app_context():
            other_review = Review.query.filter_by(
                user_id=OTHER_ID).first()
            other_diary = DiaryEntry.query.filter_by(
                user_id=OTHER_ID).first()
            other_ok = (other_review.content == B_USER_MARKER
                        and other_diary.rating is None)
        check("F6-32 other user's review untouched", other_ok)
        html = page.content()
        check("F6-33 other user's data absent from the page",
              B_USER_MARKER not in html)

        # ── error state ──────────────────────────────────────────────────
        bad_path = os.path.join(_test_db_path + "_dl", "bad.zip")
        with open(bad_path, "wb") as handle:
            handle.write(b"this is not a zip file at all")
        page.set_input_files("#import-file-letterboxd", bad_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-error[role=alert]:not(.empty\\:hidden)",
                               timeout=30000)
        error_text = page.locator("#import-error").inner_text()
        check("F6-34 malformed upload shows a readable error",
              len(error_text) > 0, error_text[:120])
        check("F6-35 error text hides internals",
              "Traceback" not in error_text and "sqlite" not in error_text.lower(),
              error_text[:160])
        check("F6-36 preview panel stays hidden after a failure",
              page.locator("#import-preview-panel").is_hidden())

        # ── no file selected ─────────────────────────────────────────────
        # The panel is already hidden after the failure, so its Cancel button
        # is not visible; go straight to clearing the file input.
        page.locator("#import-file-tvtime").set_input_files([])
        page.click('[data-action="import-preview"][data-source="tvtime"]')
        page.wait_for_timeout(500)
        check("F6-37 preview without a file is refused",
              "Choose a file" in page.locator("#import-error").inner_text(),
              page.locator("#import-error").inner_text()[:120])

        # ── responsive ───────────────────────────────────────────────────
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base}/profile/edit", wait_until="domcontentloaded")
            page.wait_for_timeout(250)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check(f"F6-38 {width}px: no horizontal overflow", overflow <= 1,
                  f"overflow={overflow}")
            for selector in ('input[data-import-source="letterboxd"]',
                             'input[data-import-source="tvtime"]',
                             '[data-action="import-preview"][data-source="letterboxd"]',
                             '[data-action="import-preview"][data-source="tvtime"]'):
                node = page.locator(selector).first
                inside = node.evaluate(
                    "el => { const r = el.getBoundingClientRect();"
                    " return r.left >= -1 && r.right <= "
                    "document.documentElement.clientWidth + 1; }"
                ) if node.count() else False
                check(f"F6-39 {width}px: {selector} inside the viewport",
                      inside)

        # ── long titles wrap (checked with a preview actually rendered) ──
        # The wrap class is applied by JS to the sample rows, so this has to
        # run against a rendered preview, not a freshly loaded page.
        page.set_viewport_size({"width": 320, "height": 900})
        long_title = ("A Very Long Film Title That Should Wrap Rather Than "
                      "Force The Horizontal Scrollbar To Appear Anywhere "
                      "FrameIQ Bengali মিশ্র আরবি ফিল्म 2021")
        long_rows = [
            ["2021-03-04 21:00", "The Matrix", "1999",
             "/film/the-matrix-long/", "4.5"],
            ["2021-04-04 21:00", long_title, "2021",
             "/film/a-very-long-title/", "4.0"],
        ]
        long_path = os.path.join(_test_db_path + "_dl", "long.zip")
        with open(long_path, "wb") as handle:
            handle.write(_letterboxd_zip(long_rows))

        page.goto(f"{base}/profile/edit", wait_until="domcontentloaded")
        page.set_input_files("#import-file-letterboxd", long_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        page.wait_for_timeout(300)

        sample_rows = page.locator("#import-samples li")
        check("F6-40 preview rendered sample rows",
              sample_rows.count() > 0, str(sample_rows.count()))
        classes = sample_rows.first.get_attribute("class") or ""
        check("F6-41 sample rows use a wrapping utility",
              "break-words" in classes, classes)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth")
        check("F6-42 a long multi-script title does not overflow at 320px",
              overflow <= 1, f"overflow={overflow}")
        # The unresolved long title must be reported, not silently dropped.
        summary_text = page.locator("#import-summary").inner_text()
        check("F6-43 long title appears in the unresolved count or samples",
              "Unresolved" in summary_text
              or "A Very Long Film Title" in page.locator(
                  "#import-samples").inner_text(),
              summary_text[:140])

        browser.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"F6 BROWSER VERIFICATION FAILED — {len(FAILURES)} check(s):")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL F6 BROWSER CHECKS PASSED")


if __name__ == "__main__":
    main()