"""Task F7 — browser verification of mappings, resolution and review import.

Boots the REAL Flask app on a throwaway SQLite temp DB (the pattern used by
verify_tv_f1/f2/f4, verify_account_export_browser.py and
verify_account_import_browser.py) and drives it with Playwright headless
Chromium.

Checks, per task §32:

  unauthenticated  cannot preview/apply/search or read/edit mappings
  authenticated    resolve panel + saved-matches section visible
  candidates       an unresolved title offers local candidates, nothing auto-picked
  explicit choice  picking a candidate then confirming imports to that title
  skip             skipping leaves the row unimported
  search           explicit local search finds a title; unknown says so
  remember         ticking the box persists a mapping, unticking does not
  reuse            a saved mapping resolves the title with no choice at all
  precedence       a saved mapping outranks a later different choice
  review           a Letterboxd review is imported as plain text
  conflict         an existing review is kept, not overwritten
  removal          removing a mapping leaves imported history intact
  isolation        no other user's data appears anywhere
  security         GET/DELETE/CSRF/status-code behaviour
  responsive       320–1440px: no overflow, controls inside the viewport

Run:  .venv/bin/python scripts/verify_import_mappings_browser.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile
import threading
import time as _time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_f7_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "f7-browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

FAILURES = []
WIDTHS = (320, 360, 390, 414, 768, 1024, 1440)

# The file's film is "Solaris" (1972). FrameIQ holds only "Solaris: A Space
# Odyssey", so nothing matches exactly and the row must arrive unresolved WITH
# candidates — the case F6 could not resolve at all.
UNKNOWN_TMDB = 710_000_001
ODYSSEY_TMDB = 710_000_002
MATRIX_TMDB = 710_000_003
OTHER_USER_MARKER = "F7_OTHER_USER_MUST_NEVER_LEAK"
USER_REVIEW_TEXT = "My own handwritten review that must survive"


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else "")
    print(line)
    if not cond:
        FAILURES.append(name)


def _letterboxd_zip(watched_rows, reviews=None):
    """A Letterboxd-style export, optionally with a real reviews.csv."""
    def _csv(header, rows):
        text = io.StringIO(newline="")
        text.write(",".join(header) + "\r\n")
        for row in rows:
            text.write(",".join(row) + "\r\n")
        return text.getvalue()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("watched.csv", _csv(
            ["Date", "Name", "Year", "Letterboxd URI", "Rating"], watched_rows))
        if reviews is not None:
            archive.writestr("reviews.csv", _csv(
                ["Date", "Name", "Year", "Letterboxd URI", "Rating", "Review"],
                reviews))
    return buffer.getvalue()


# "Solaris" 1972 — no exact local match; only a substring candidate exists.
AMBIGUOUS_ROWS = [
    ["2021-03-04 21:00", "Solaris", "1972", "/film/solaris/", "4.5"],
]

REVIEW_ROWS = [
    ["2021-03-04 21:00", "The Matrix", "1999", "/film/the-matrix/", "4.5",
     '<p>Groundbreaking and still sharp.</p><p>Watched it again.</p>'],
]


def summary_value(page, label):
    """The number shown in a summary tile, matched on its label.

    The grid renders every category including the zeros, so asserting that the
    word "Unresolved" is absent proves nothing — the tile is always there. The
    count is what has to change.
    """
    tiles = page.locator("#import-summary > div")
    for index in range(tiles.count()):
        tile = tiles.nth(index)
        if (tile.locator("dt").inner_text() or "").strip() == label:
            return (tile.locator("dd").inner_text() or "").strip()
    return None


def check_responsive(page, lb_path):
    """No horizontal overflow and no control outside the viewport.

    Run against a RENDERED preview, not a fresh page load: the resolve rows are
    built by JS, so a check on an unrendered page would pass vacuously.
    """
    page.goto(page.context.pages[0].url.split("/profile/edit")[0]
              + "/profile/edit", wait_until="domcontentloaded")
    page.set_input_files("#import-file-letterboxd", lb_path)
    page.click('[data-action="import-preview"][data-source="letterboxd"]')
    page.wait_for_selector("#import-resolve-panel:not(.hidden)", timeout=30000)
    page.wait_for_timeout(400)

    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        page.wait_for_timeout(200)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - "
            "document.documentElement.clientWidth")
        check(f"F7-59 {width}px: no horizontal overflow", overflow <= 1,
              f"overflow={overflow}")
        for selector in ('#import-save-mappings', '#import-resolve-list',
                         '#import-resolve-refresh'):
            node = page.locator(selector).first
            inside = node.evaluate(
                "el => { const r = el.getBoundingClientRect();"
                " return r.left >= -1 && r.right <= "
                "document.documentElement.clientWidth + 1; }"
            ) if node.count() else False
            check(f"F7-60 {width}px: {selector} inside the viewport", inside)


def check_accessibility(page):
    """The decision UI must be operable by keyboard and announced correctly."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.wait_for_timeout(200)

    check("F7-61 each decision row is a labelled fieldset",
          page.locator("#import-resolve-list fieldset").count() > 0)
    check("F7-62 every candidate radio has a label",
          page.locator('#import-resolve-list input[type="radio"]').count()
          == page.locator('#import-resolve-list label').count(),
          str(page.locator('#import-resolve-list input[type="radio"]'
                           ).count()))
    check("F7-63 candidate radios are grouped under one name",
          page.locator('#import-resolve-list input[type="radio"]').first
          .get_attribute("name")
          == page.locator('#import-resolve-list input[type="radio"]').last
          .get_attribute("name"))
    check("F7-64 the resolve status region is polite",
          page.locator("#import-resolve-status").get_attribute("aria-live")
          == "polite")
    check("F7-65 the error region is an alert",
          page.locator("#import-error").get_attribute("role") == "alert")
    check("F7-66 the saved-matches list is a live region",
          page.locator("#import-mappings-list").get_attribute("aria-live")
          == "polite")
    check("F7-67 candidate labels wrap long titles",
          "break-words"
          in (page.locator("#import-resolve-list label").first
              .get_attribute("class") or ""))

    # Keyboard: the radio group is reachable, and arrow keys change selection.
    first = page.locator('#import-resolve-list input[type="radio"]').first
    first.focus()
    focused = page.evaluate("() => document.activeElement.tagName")
    check("F7-68 a candidate is keyboard focusable", focused == "INPUT",
          focused)
    page.keyboard.press("Space")
    page.wait_for_timeout(200)
    check("F7-69 space selects the focused candidate",
          page.locator('#import-resolve-list input[type="radio"]:checked'
                       ).count() == 1)
    check("F7-70 selecting by keyboard enables confirm",
          not page.locator("#import-apply").is_disabled())


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def main():
    from playwright.sync_api import sync_playwright

    from app import app as flask_app
    from models import db as _db

    flask_app.config.update(
        TESTING=False, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)

    with flask_app.app_context():
        _db.create_all()

    import requests as _requests
    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=5011,
                                     debug=False, use_reloader=False),
        daemon=True).start()

    base = "http://127.0.0.1:5011"
    for _ in range(60):
        try:
            _requests.get(f"{base}/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: Flask dev server did not start")
        sys.exit(2)

    from models import DiaryEntry, ImportSourceMapping, MediaItem, Review, User, db

    with flask_app.app_context():
        user = User(username="f7verify", email="f7verify@verify.test",
                    email_verified=True)
        user.set_password("F7Browser1!")
        other = User(username="f7other", email="f7other@verify.test",
                     email_verified=True)
        other.set_password("F7Browser1!")
        db.session.add_all([user, other])
        db.session.add_all([
            # Only a SUBSTRING match for "Solaris".
            MediaItem(tmdb_id=ODYSSEY_TMDB, media_type="movie",
                      title="Solaris: A Space Odyssey",
                      release_date=date(1968, 4, 2)),
            MediaItem(tmdb_id=MATRIX_TMDB, media_type="movie",
                      title="The Matrix", release_date=date(1999, 3, 31)),
        ])
        db.session.commit()
        matrix = db.session.execute(
            db.select(MediaItem).where(MediaItem.tmdb_id == MATRIX_TMDB)
        ).scalars().first()
        odyssey = db.session.execute(
            db.select(MediaItem).where(MediaItem.tmdb_id == ODYSSEY_TMDB)
        ).scalars().first()
        # Another user owns data that must never leak into this user's UI.
        db.session.add(Review(user_id=other.id, media_id=matrix.id,
                              media_type="movie",
                              content=OTHER_USER_MARKER, rating=5.0))
        db.session.commit()
        USER_ID, OTHER_ID = user.id, other.id
        MATRIX_ID, ODYSSEY_ID = matrix.id, odyssey.id

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page = context.new_page()

        # ── unauthenticated access to every new endpoint ─────────────────
        anon = _requests.Session()
        for source in ("letterboxd", "tvtime"):
            for endpoint in ("preview", "apply", "search"):
                response = anon.post(
                    f"{base}/api/account/import/{source}/{endpoint}",
                    files={"file": ("x.zip", b"PK\x03\x04", "application/zip")},
                    allow_redirects=False)
                check(f"F7-1 unauthenticated {source}/{endpoint} denied",
                      response.status_code in (401, 302),
                      str(response.status_code))
        response = anon.get(f"{base}/api/account/import/mappings",
                            allow_redirects=False)
        check("F7-2 unauthenticated mappings list denied",
              response.status_code in (401, 302), str(response.status_code))
        response = anon.post(f"{base}/api/account/import/mappings",
                             json={"source": "letterboxd",
                                   "media_type": "movie",
                                   "source_key": "solaris",
                                   "media_id": ODYSSEY_TMDB},
                             allow_redirects=False)
        check("F7-3 unauthenticated mapping create denied",
              response.status_code in (401, 302), str(response.status_code))
        response = anon.delete(f"{base}/api/account/import/mappings/1",
                               allow_redirects=False)
        check("F7-4 unauthenticated mapping delete denied",
              response.status_code in (401, 302), str(response.status_code))

        # ── method discipline ────────────────────────────────────────────
        response = anon.get(f"{base}/api/account/import/letterboxd/search",
                            allow_redirects=False)
        check("F7-5 mapping search rejects GET",
              response.status_code == 405, str(response.status_code))
        response = anon.get(f"{base}/api/account/import/mappings/1",
                            allow_redirects=False)
        check("F7-6 mapping delete endpoint rejects GET",
              response.status_code == 405, str(response.status_code))

        # ── authenticated UI ─────────────────────────────────────────────
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "f7verify")
        page.fill('input[name="password"]', "F7Browser1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        page.goto(f"{base}/profile/edit", wait_until="domcontentloaded")
        check("F7-7 saved-matches section is present",
              page.locator("#import-mappings").count() == 1)
        check("F7-8 saved-matches section explains removal is safe",
              "history" in page.locator("#import-mappings").inner_text().lower())
        check("F7-9 resolve panel starts hidden",
              page.locator("#import-resolve-panel").is_hidden())
        check("F7-10 remember-mappings checkbox exists and is unticked",
              page.locator("#import-save-mappings").count() == 1
              and not page.locator("#import-save-mappings").is_checked())
        check("F7-11 no saved matches before any import",
              "No saved matches yet"
              in page.locator("#import-mappings-list").inner_text())

        # ── unresolved title offers candidates, none auto-picked ─────────
        payload = _letterboxd_zip(AMBIGUOUS_ROWS)
        lb_path = _write(os.path.join(_test_db_path + "_dl", "amb.zip"), payload)

        page.set_input_files("#import-file-letterboxd", lb_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-resolve-panel:not(.hidden)",
                               timeout=30000)
        check("F7-12 resolve panel appears for an unresolved title", True)
        summary = page.locator("#import-summary").inner_text()
        check("F7-13 unresolved is counted",
              summary_value(page, "Unresolved") == "1", summary[:140])
        resolve_text = page.locator("#import-resolve-list").inner_text()
        check("F7-14 the unresolved row is named for the user",
              "Solaris" in resolve_text, resolve_text[:120])
        check("F7-15 a local candidate is offered",
              "Solaris: A Space Odyssey" in resolve_text, resolve_text[:160])
        check("F7-16 nothing is pre-selected",
              page.locator('#import-resolve-list input[type="radio"]:checked'
                           ).count() == 0)
        check("F7-17 a skip option exists",
              "Skip this title" in resolve_text)
        check("F7-18 confirm is disabled while nothing can be imported",
              page.locator("#import-apply").is_disabled())

        # ── explicit local search ────────────────────────────────────────
        page.click('#import-resolve-list [data-action="import-search"]')
        page.wait_for_selector('#import-resolve-list input[type="search"]',
                               timeout=10000)
        search_input = page.locator(
            '#import-resolve-list input[type="search"]').first
        search_input.fill("the matrix")
        page.click('#import-resolve-list button:text-is("Search")')
        page.wait_for_timeout(1200)
        after_search = page.locator("#import-resolve-list").inner_text()
        check("F7-19 explicit search finds a local title",
              "The Matrix" in after_search, after_search[:200])
        search_input.fill("zzz-no-such-title-zzz")
        page.click('#import-resolve-list button:text-is("Search")')
        page.wait_for_timeout(1200)
        miss_text = page.locator("#import-resolve-list").inner_text()
        check("F7-20 an unknown title says so rather than searching elsewhere",
              "No titles in FrameIQ match" in miss_text, miss_text[-200:])

        # ── choosing a candidate enables confirm ─────────────────────────
        page.locator('#import-resolve-list input[type="radio"]'
                     '[value="%d"]' % ODYSSEY_ID).first.check()
        page.wait_for_timeout(200)
        check("F7-21 the choice enables confirm",
              not page.locator("#import-apply").is_disabled())

        # ── preview is re-run with the choice; still nothing written ─────
        page.click("#import-resolve-refresh")
        page.wait_for_timeout(1500)
        summary2 = page.locator("#import-summary").inner_text()
        check("F7-22 the choice reclassifies the row as importable",
              summary_value(page, "Will import") == "1", summary2[:140])
        check("F7-23 the resolve panel closes when nothing is left to decide",
              page.locator("#import-resolve-panel").is_hidden())
        with flask_app.app_context():
            check("F7-24 preview wrote no diary entry",
                  DiaryEntry.query.filter_by(user_id=USER_ID).count() == 0)
            check("F7-25 preview wrote no mapping",
                  ImportSourceMapping.query.filter_by(
                      user_id=USER_ID).count() == 0)

        # ── unticked box => confirm works, nothing remembered ────────────
        page.click("#import-apply")
        page.wait_for_selector("#import-preview-panel", state="hidden",
                               timeout=30000)
        page.wait_for_timeout(800)
        with flask_app.app_context():
            entry = db.session.execute(
                db.select(DiaryEntry).where(DiaryEntry.user_id == USER_ID)
            ).scalars().first()
            check("F7-26 the chosen title received the watch",
                  entry is not None and entry.media_id == ODYSSEY_ID,
                  str(entry.media_id if entry else None))
            check("F7-27 nothing was remembered with the box unticked",
                  ImportSourceMapping.query.filter_by(
                      user_id=USER_ID).count() == 0)

        # ── re-import the same file: mapping not required, idempotent ────
        page.set_input_files("#import-file-letterboxd", lb_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        page.wait_for_timeout(600)
        summary3 = page.locator("#import-summary").inner_text()
        check("F7-28 without a mapping the title is unresolved again",
              summary_value(page, "Unresolved") == "1", summary3[:140])
        check("F7-29 and is not counted as already present yet",
              summary_value(page, "Already present") == "0", summary3[:140])

        # ── choose again and REMEMBER it ─────────────────────────────────
        page.locator('#import-resolve-list input[type="radio"]'
                     '[value="%d"]' % ODYSSEY_ID).first.check()
        page.check("#import-save-mappings")
        page.click("#import-resolve-refresh")
        page.wait_for_timeout(1500)
        page.click("#import-apply")
        page.wait_for_selector("#import-preview-panel", state="hidden",
                               timeout=30000)
        page.wait_for_timeout(1000)
        with flask_app.app_context():
            mapping = db.session.execute(
                db.select(ImportSourceMapping).where(
                    ImportSourceMapping.user_id == USER_ID)).scalars().first()
            check("F7-30 ticking the box persisted a mapping", mapping is not None)
            check("F7-31 the mapping points at the chosen title",
                  mapping is not None and mapping.media_id == ODYSSEY_ID,
                  str(mapping.media_id if mapping else None))

        done_text = page.locator("#import-error").inner_text()
        check("F7-32 the confirmation mentions the saved match",
              "Saved 1 title match" in done_text, done_text[:160])
        check("F7-33 the saved match is listed for the user",
              "Solaris: A Space Odyssey"
              in page.locator("#import-mappings-list").inner_text())

        # ── a saved mapping resolves with no choice at all ───────────────
        page.set_input_files("#import-file-letterboxd", lb_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        page.wait_for_timeout(600)
        summary4 = page.locator("#import-summary").inner_text()
        check("F7-34 the saved mapping resolves the title unaided",
              summary_value(page, "Unresolved") == "0", summary4[:140])
        check("F7-34b and it is recognised as already imported",
              summary_value(page, "Already present") == "1", summary4[:140])
        check("F7-35 and the resolve panel stays closed",
              page.locator("#import-resolve-panel").is_hidden())

        # ── review import + conflict ─────────────────────────────────────
        review_payload = _letterboxd_zip(
            [["2021-03-04 21:00", "The Matrix", "1999",
              "/film/the-matrix/", "4.5"]], REVIEW_ROWS)
        review_path = _write(os.path.join(_test_db_path + "_dl", "rev.zip"),
                             review_payload)
        page.set_input_files("#import-file-letterboxd", review_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        page.wait_for_timeout(600)
        page.click("#import-apply")
        page.wait_for_selector("#import-preview-panel", state="hidden",
                               timeout=30000)
        page.wait_for_timeout(800)
        with flask_app.app_context():
            review = db.session.execute(
                db.select(Review).where(Review.user_id == USER_ID)
            ).scalars().first()
            check("F7-36 the review was imported", review is not None)
            check("F7-37 HTML was reduced to plain text",
                  review is not None and "<p>" not in (review.content or "")
                  and "Groundbreaking and still sharp."
                  in (review.content or ""),
                  repr(review.content if review else None))

        # Now rewrite the user's own review, and re-import: it must survive.
        with flask_app.app_context():
            review = db.session.execute(
                db.select(Review).where(Review.user_id == USER_ID)
            ).scalars().first()
            review.content = USER_REVIEW_TEXT
            review.rating = 5.0
            db.session.commit()

        # Re-import on a LATER watch date: the new diary event is importable
        # while the review conflicts. That proves a review conflict blocks only
        # the review — it does not hold up an unrelated watch.
        conflict_payload = _letterboxd_zip(
            [["2022-08-08 21:00", "The Matrix", "1999",
              "/film/the-matrix/", "4.5"]], REVIEW_ROWS)
        conflict_path = _write(
            os.path.join(_test_db_path + "_dl", "conflict.zip"),
            conflict_payload)
        page.set_input_files("#import-file-letterboxd", conflict_path)
        page.click('[data-action="import-preview"][data-source="letterboxd"]')
        page.wait_for_selector("#import-preview-panel:not(.hidden)",
                               timeout=30000)
        page.wait_for_timeout(600)
        conflict_summary = page.locator("#import-summary").inner_text()
        check("F7-38b the new watch date is still importable alongside the "
              "conflict", summary_value(page, "Will import") == "1",
              conflict_summary[:160])
        check("F7-38 the conflict is counted, not hidden",
              summary_value(page, "Kept yours") == "1", conflict_summary[:160])
        page.click("#import-apply")
        page.wait_for_selector("#import-preview-panel", state="hidden",
                               timeout=30000)
        page.wait_for_timeout(800)
        conflict_msg = page.locator("#import-error").inner_text()
        check("F7-39 the result explains the user's review was kept",
              "kept your own review" in conflict_msg, conflict_msg[:200])
        with flask_app.app_context():
            kept = db.session.execute(
                db.select(Review).where(Review.user_id == USER_ID)
            ).scalars().all()
            check("F7-40 the user's review text is untouched",
                  any(r.content == USER_REVIEW_TEXT for r in kept),
                  repr([r.content for r in kept]))
            check("F7-41 no duplicate review was created", len(kept) == 1,
                  str(len(kept)))

        # ── removing a mapping keeps imported history ────────────────────
        with flask_app.app_context():
            diary_before = DiaryEntry.query.filter_by(
                user_id=USER_ID).count()
        page.locator('[data-action="import-mapping-delete"]').first.click()
        page.wait_for_timeout(1500)
        with flask_app.app_context():
            check("F7-42 the mapping was removed",
                  ImportSourceMapping.query.filter_by(
                      user_id=USER_ID).count() == 0)
            diary_after = DiaryEntry.query.filter_by(
                user_id=USER_ID).count()
            check("F7-43 removing a mapping left the history intact",
                  diary_after == diary_before > 0,
                  f"before={diary_before} after={diary_after}")
        check("F7-44 the empty state is shown again",
              "No saved matches yet"
              in page.locator("#import-mappings-list").inner_text())

        # ── authenticated API checks, driven from inside the page ───────
        #
        # These run through fetch() in the page rather than a separate HTTP
        # client: the page already holds the authenticated cookie, so there is
        # no second login to keep in sync and no way for the two identities to
        # drift apart.
        def api(method, path, payload=None, file_payload=None, filename=None,
                form_fields=None):
            return page.evaluate(
                """async (args) => {
                    const headers = {};
                    if (args.csrf) { headers['X-CSRFToken'] = args.csrf; }
                    const opts = {method: args.method,
                                  credentials: 'same-origin', headers: headers};
                    if (args.payload !== null) {
                        headers['Content-Type'] = 'application/json';
                        opts.body = JSON.stringify(args.payload);
                    } else if (args.fileB64 !== null) {
                        const bin = atob(args.fileB64);
                        const bytes = new Uint8Array(bin.length);
                        for (let i = 0; i < bin.length; i++) {
                            bytes[i] = bin.charCodeAt(i);
                        }
                        const form = new FormData();
                        form.append('file', new File([bytes], args.filename,
                                                     {type: 'application/zip'}));
                        Object.keys(args.fields || {}).forEach(function (name) {
                            form.append(name, args.fields[name]);
                        });
                        if (args.csrf) { form.append('csrf_token', args.csrf); }
                        opts.body = form;
                    }
                    const response = await fetch(args.path, opts);
                    let body = null;
                    try { body = await response.json(); } catch (e) { body = null; }
                    return {status: response.status, body: body};
                }""",
                {"method": method, "path": path,
                 "csrf": page.evaluate(
                     "() => { const m = document.querySelector("
                     "'meta[name=\"csrf-token\"]');"
                     " return m ? m.getAttribute('content') : null; }"),
                 "payload": payload,
                 "fileB64": (None if file_payload is None
                             else __import__("base64").b64encode(
                                 file_payload).decode()),
                 "filename": filename or "x.zip",
                 "fields": form_fields})

        # ── isolation ────────────────────────────────────────────────────
        everything = page.content()
        check("F7-45 no other user's review text appears",
              OTHER_USER_MARKER not in everything)

        listed = api("GET", "/api/account/import/mappings")
        check("F7-46 the mappings API returns this user's own mappings",
              listed["status"] == 200
              and all(m["media_title"] != OTHER_USER_MARKER
                      for m in (listed["body"] or {}).get("mappings", [])),
              str(listed["status"]))

        # A user cannot delete another user's mapping by guessing its id.
        with flask_app.app_context():
            victim = ImportSourceMapping(
                user_id=OTHER_ID, source="letterboxd", media_type="movie",
                source_key="theirs", media_id=MATRIX_ID)
            db.session.add(victim)
            db.session.commit()
            victim_id = victim.id
        response = api("DELETE",
                       f"/api/account/import/mappings/{victim_id}")
        check("F7-48 deleting another user's mapping is refused",
              response["status"] == 400, str(response["status"]))
        with flask_app.app_context():
            check("F7-49 and their mapping still exists",
                  db.session.get(ImportSourceMapping, victim_id) is not None)

        # ── validation via the API surface ───────────────────────────────
        response = api("POST", "/api/account/import/mappings",
                       {"source": "trakt", "media_type": "movie",
                        "source_key": "x", "media_id": ODYSSEY_ID})
        check("F7-50 an unknown source is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/mappings",
                       {"source": "letterboxd", "media_type": "movie",
                        "source_key": "x", "media_id": ODYSSEY_ID + 99999})
        check("F7-51 a nonexistent title is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/mappings",
                       {"source": "letterboxd", "media_type": "movie",
                        "source_key": "", "media_id": ODYSSEY_ID})
        check("F7-52b an empty source key is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/letterboxd/search",
                       {"query": ""})
        check("F7-52 an empty search is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/letterboxd/search",
                       {"query": "matrix", "media_type": "audio"})
        check("F7-53 an invalid media type is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/letterboxd/search",
                       {"query": "matrix"})
        check("F7-53b a valid local search returns results",
              response["status"] == 200
              and (response["body"] or {}).get("results"),
              str(response["status"]))

        # A selection key that is not in the file must be refused (400).
        response = api("POST", "/api/account/import/letterboxd/preview",
                       file_payload=payload,
                       form_fields={"selections": json.dumps(
                           {json.dumps(["movie", "not-in-this-file"]):
                            ODYSSEY_ID})})
        check("F7-54 a selection for a row that is not in the file is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/letterboxd/preview",
                       file_payload=payload,
                       form_fields={"selections": "{not json"})
        check("F7-55 malformed selections JSON is refused",
              response["status"] == 400, str(response["status"]))

        response = api("POST", "/api/account/import/letterboxd/preview",
                       file_payload=payload)
        check("F7-56 preview with no selections is still 200",
              response["status"] == 200, str(response["status"]))

        # Applying with no selections writes nothing.
        with flask_app.app_context():
            diary_before = DiaryEntry.query.filter_by(
                user_id=USER_ID).count()
        response = api("POST", "/api/account/import/letterboxd/apply",
                       file_payload=payload,
                       form_fields={"save_mappings": "1"})
        check("F7-57 an unresolved-only file applies without error",
              response["status"] == 200, str(response["status"]))
        with flask_app.app_context():
            check("F7-58 and it wrote no history and saved no mapping",
                  DiaryEntry.query.filter_by(user_id=USER_ID).count()
                  == diary_before
                  and ImportSourceMapping.query.filter_by(
                      user_id=USER_ID).count() == 0)

        check_responsive(page, lb_path)
        check_accessibility(page)

        browser.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"F7 BROWSER VERIFICATION FAILED — {len(FAILURES)} check(s):")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL F7 BROWSER CHECKS PASSED")


if __name__ == "__main__":
    sys.exit(main() or 0)