"""Task F5 — browser verification of the account export flow.

Boots the REAL Flask app on a throwaway SQLite temp DB (the pattern used by
verify_tv_f1_browser.py / verify_tv_f2_browser.py: SKIP_SCHEMA_GUARD=1, CSRF
and rate limiting off so the scripted flow is deterministic) and drives it
with Playwright headless Chromium.

This is INTEGRATION/BROWSER verification, not part of `pytest tests/`: it
starts a server and needs Playwright.

Checks, per task §68/§69:

  1  authenticated account page shows export controls
  2  JSON download starts and completes
  3  downloaded JSON parses
  4  JSON contains the expected top-level sections
  5  CSV bundle download starts and completes
  6  the bundle is a readable ZIP with parseable CSVs
  7  the fixture user's data is present and matches
  8  the OTHER user's distinctive data appears nowhere
  9  the controls are reachable and usable at every required width
  10 no horizontal overflow at any width
  11 an unauthenticated request for the export is denied
  12 the page never embeds export payload contents in its HTML

Run:  .venv/bin/python scripts/verify_account_export_browser.py
"""
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_test_db_fd, _test_db_path = tempfile.mkstemp(
    prefix="frameiq_f5_browser_", suffix=".db")
os.close(_test_db_fd)
os.environ.setdefault("SECRET_KEY", "f5-browser-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

FAILURES = []

# Distinctive markers — if one of these shows up in the wrong export, the leak
# is unmissable. Never log real credentials.
BENGALI_TITLE = "আবার দেখা"
EMOJI = "🎬🍿"
USER_A_MARKER = "A_PRIVATE_EPISODE_NOTE"
USER_B_MARKER = "B_USER_MUST_NEVER_LEAK"

JSON_URL = "/api/account/export/json"
CSV_URL = "/api/account/export/csv"
WIDTHS = (320, 360, 390, 414, 768, 1024, 1440)


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
    import time as _time

    import requests as _requests
    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=5006,
                                     debug=False, use_reloader=False),
        daemon=True).start()

    base = "http://127.0.0.1:5006"
    for _ in range(60):
        try:
            _requests.get(f"{base}/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: Flask dev server did not start")
        sys.exit(2)

    from models import (ChatConversation, ChatMessage, ContinueWatchingItem,
                        DiaryEntry, MediaItem, Notification, Review,
                        SmartList, Tag,
                        TVEpisodeWatch, User, UserFollow, UserList,
                        UserListItem, UserMediaTag, UserStreamingService, db)

    with flask_app.app_context():
        user = User(username="f5verify", email="f5verify@verify.test",
                    email_verified=True, first_name="Amin",
                    bio="আমি সিনেমা দেখি %s" % EMOJI, streaming_region="BD")
        user.set_password("F5Browser1!")
        other = User(username="f5other", email="f5other@verify.test",
                     email_verified=True)
        other.set_password("F5Browser1!")
        db.session.add_all([user, other])

        movie = MediaItem(tmdb_id=9101, media_type="movie", title=BENGALI_TITLE)
        show = MediaItem(tmdb_id=9102, media_type="tv", title="F5 TV Show")
        db.session.add_all([movie, show])
        db.session.commit()

        db.session.add(DiaryEntry(user_id=user.id, media_id=movie.id,
                                  media_type="movie",
                                  watched_date=date(2026, 1, 1), rating=4.5))
        db.session.add(DiaryEntry(user_id=other.id, media_id=movie.id,
                                  media_type="movie",
                                  watched_date=date(2026, 2, 2)))
        db.session.add(TVEpisodeWatch(
            user_id=user.id, show_id=9102, season_number=2,
            episode_number=11, watched_date=date(2026, 1, 5), rating=4.5,
            notes=USER_A_MARKER))
        db.session.add(TVEpisodeWatch(
            user_id=other.id, show_id=9102, season_number=9,
            episode_number=9, watched_date=date(2026, 2, 2),
            notes=USER_B_MARKER))
        db.session.add(Review(user_id=user.id, media_id=movie.id,
                              media_type="movie", content="Solid %s" % EMOJI,
                              rating=4.0, title="My take"))
        user_list = UserList(user_id=user.id, title="F5 Ranked list",
                             list_type="ranked", slug="f5-ranked")
        db.session.add(user_list)
        db.session.commit()
        db.session.add(UserListItem(list_id=user_list.id, media_id=movie.id,
                                    media_type="movie", position=1))
        tag = Tag(name="favourite")
        db.session.add(tag)
        db.session.commit()
        db.session.add(UserMediaTag(user_id=user.id, media_id=9101,
                                    media_type="movie", tag_id=tag.id))
        db.session.add(UserStreamingService(
            user_id=user.id, provider_id=8, region="BD"))
        db.session.add(SmartList(
            user_id=user.id, name="F5 smart", scope="diary",
            filters_json='{"genre": ["Drama"]}'))
        db.session.add(UserFollow(follower_id=user.id,
                                  following_id=other.id))
        db.session.add(Notification(
            user_id=user.id, type="new_episode", title="Ep", show_id=9102,
            season=2, episode=11))
        db.session.add(ContinueWatchingItem(
            user_id=user.id, media_type="movie", tmdb_id=9101,
            title=BENGALI_TITLE))
        conversation = ChatConversation(user_id=user.id, title="F5 chat")
        db.session.add(conversation)
        db.session.commit()
        db.session.add(ChatMessage(conversation_id=conversation.id, role="user",
                                   content="a question"))
        db.session.commit()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page = context.new_page()

        # ── 11  unauthenticated access is denied ────────────────────────────
        response = _requests.get(f"{base}{JSON_URL}", allow_redirects=False)
        check("F5-1 unauthenticated JSON export is denied",
              response.status_code in (401, 302), str(response.status_code))
        if response.status_code == 302:
            check("F5-2 redirect target is the login page",
                  "/login" in response.headers.get("Location", ""),
                  response.headers.get("Location", ""))
        response = _requests.get(f"{base}{CSV_URL}", allow_redirects=False)
        check("F5-3 unauthenticated CSV export is denied",
              response.status_code in (401, 302), str(response.status_code))

        # ── 1  login and find the controls ──────────────────────────────────
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "f5verify")
        page.fill('input[name="password"]', "F5Browser1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        page.goto(f"{base}/profile/edit", wait_until="domcontentloaded")
        section = page.locator("#account-export")
        section.wait_for(timeout=15000)
        check("F5-4 account page shows the export section",
              section.count() == 1)
        json_link = page.locator('[data-action="export-json"]')
        csv_link = page.locator('[data-action="export-csv"]')
        check("F5-5 Download JSON control present", json_link.count() == 1)
        check("F5-6 Download CSV bundle control present", csv_link.count() == 1)
        check("F5-7 both controls are real links to the export endpoints",
              json_link.get_attribute("href") == JSON_URL
              and csv_link.get_attribute("href") == CSV_URL)
        check("F5-8 controls are keyboard reachable links",
              json_link.evaluate("el => el.tagName") == "A"
              and csv_link.evaluate("el => el.tagName") == "A")
        check("F5-9 controls have accessible names",
              bool(json_link.inner_text().strip())
              and bool(csv_link.inner_text().strip()))
        check("F5-10 heading is labelled by its section",
              page.locator("#account-export-heading").count() == 1)
        check("F5-11 status region announces politely",
              page.locator("#export-status[aria-live=polite]").count() == 1)

        # ── 12  the page never embeds export payload ────────────────────────
        html = page.content()
        for forbidden in (USER_A_MARKER, USER_B_MARKER, BENGALI_TITLE):
            check(f"F5-12 page HTML omits {forbidden!r}",
                  forbidden not in html)

        # ── 2/3/4  JSON download ────────────────────────────────────────────
        with page.expect_download(timeout=30000) as info:
            json_link.click()
        download = info.value
        json_path = os.path.join(_test_db_path + "_dl", download.suggested_filename)
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        download.save_as(json_path)
        check("F5-13 JSON download started",
              download.suggested_filename.startswith("frameiq-export-")
              and download.suggested_filename.endswith(".json"),
              download.suggested_filename)

        raw = open(json_path, "rb").read()
        try:
            export = json.loads(raw.decode("utf-8"))
            parsed = True
        except Exception as exc:  # noqa: BLE001
            export = {}
            parsed = False
            check("F5-14 downloaded JSON parses", False, str(exc))
        if parsed:
            check("F5-14 downloaded JSON parses", True)
            check("F5-15 format + version present",
                  export.get("format") == "frameiq-export"
                  and export.get("version") == 1)
            for section_name in ("account", "profile", "watch_history",
                                 "tv_history", "watchlist", "lists",
                                 "list_items", "reviews", "ratings", "tags",
                                 "smart_lists", "streaming_services",
                                 "notifications", "social_data",
                                 "derived", "counts"):
                check(f"F5-16 section {section_name!r} present",
                      section_name in export)

            # ── 7  fixture data matches ───────────────────────────────────
            check("F5-17 account identifies the signed-in user",
                  export["account"]["username"] == "f5verify",
                  export["account"].get("username", ""))
            check("F5-18 Bengali + emoji preserved in JSON",
                  BENGALI_TITLE in raw.decode("utf-8") and EMOJI in raw.decode("utf-8"))
            check("F5-19 movie history exported",
                  len(export["watch_history"]["movies"]) == 1)
            check("F5-20 TV episode history exported with S2E11",
                  any(e["season_number"] == 2 and e["episode_number"] == 11
                      for e in export["tv_history"]["episodes"]))
            check("F5-21 review exported",
                  len(export["reviews"]) == 1
                  and export["reviews"][0]["review_title"] == "My take",
                  str(export.get("reviews"))[:120])
            check("F5-22 list exported as ranked",
                  len(export["lists"]) == 1
                  and export["lists"][0]["list_type"] == "ranked")
            check("F5-23 list item exported with its position",
                  len(export["list_items"]) == 1
                  and export["list_items"][0]["position"] == 1)
            check("F5-24 tag exported",
                  len(export["tags"]) == 1
                  and export["tags"][0]["tag"] == "favourite")
            check("F5-25 smart list exported",
                  len(export["smart_lists"]) == 1)
            check("F5-26 streaming service + region exported",
                  len(export["streaming_services"]) == 1
                  and export["streaming_services"][0]["region"] == "BD")
            check("F5-27 following relationship exported",
                  len(export["social_data"]["following"]) == 1)
            check("F5-28 episode notes preserved",
                  any(e["notes"] == USER_A_MARKER
                      for e in export["tv_history"]["episodes"]))
            check("F5-29 chat conversation exported",
                  len(export["activity_state"]["chat_conversations"]) >= 1)
            check("F5-30 password hash absent",
                  "password_hash" not in raw.decode("utf-8"))

            # ── 8  no other user's data ──────────────────────────────────
            body = raw.decode("utf-8")
            check("F5-31 JSON omits the other user's episode notes",
                  USER_B_MARKER not in body)
            # The followed user's PUBLIC username is an intended relationship
            # reference (docs/export-format.md §9). Their private data must not.
            for private in ("f5other@verify.test", USER_B_MARKER):
                check(f"F5-31 JSON omits other user's private {private!r}",
                      private not in body)
            followed = [row["followed_username"]
                        for row in export["social_data"]["following"]]
            check("F5-31 followed username appears ONLY as a follow reference",
                  followed == ["f5other"], str(followed))
            check("F5-32 no S9E9 in our history",
                  not any(e["season_number"] == 9
                          for e in export["tv_history"]["episodes"]))

        # ── 5/6  CSV bundle download ───────────────────────────────────────
        with page.expect_download(timeout=30000) as info:
            csv_link.click()
        download = info.value
        zip_path = os.path.join(_test_db_path + "_dl",
                                download.suggested_filename)
        download.save_as(zip_path)
        check("F5-33 CSV bundle download started",
              download.suggested_filename.startswith("frameiq-export-")
              and download.suggested_filename.endswith(".zip"),
              download.suggested_filename)

        blob = open(zip_path, "rb").read()
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            check("F5-34 bundle opens and is a valid ZIP",
                  archive.testzip() is None)
            check("F5-35 bundle contains README.txt", "README.txt" in names)
            check("F5-36 bundle contains the expected CSV set",
                  {"movies.csv", "tv_episodes.csv", "lists.csv",
                   "list_items.csv", "reviews.csv", "ratings.csv",
                   "tags.csv", "watchlist.csv"} <= set(names),
                  str(sorted(names)))
            check("F5-37 no traversal or absolute paths in the archive",
                  all(".." not in n and not n.startswith("/") and "/" not in n
                      for n in names))

            import csv as _csv
            parse_ok = True
            for name in names:
                if not name.endswith(".csv"):
                    continue
                text = archive.read(name).decode("utf-8")
                rows = list(_csv.reader(io.StringIO(text)))
                if not rows:
                    parse_ok = False
            check("F5-38 every CSV in the bundle is readable", parse_ok)

            movies_text = archive.read("movies.csv").decode("utf-8")
            check("F5-39 movies.csv carries the Bengali title",
                  BENGALI_TITLE in movies_text)
            episodes_text = archive.read("tv_episodes.csv").decode("utf-8")
            check("F5-40 tv_episodes.csv carries S2E11 + notes",
                  ",2,11," in episodes_text and USER_A_MARKER in episodes_text)
            readme = archive.read("README.txt").decode("utf-8")
            check("F5-41 README documents format, version and canonical source",
                  "frameiq-export" in readme and "Version: 1" in readme
                  and "TVEpisodeWatch" in readme)

            # ── 8 (bundle)  no other user's data ────────────────────────
            for name in names:
                text = archive.read(name).decode("utf-8")
                check(f"F5-42 bundle {name} omits other user's notes",
                      USER_B_MARKER not in text)
                check(f"F5-42 bundle {name} omits other user's email",
                      "f5other@verify.test" not in text)
            # social.csv carries the followed username as the portable
            # relationship reference; nothing else may carry it.
            social_text = archive.read("social.csv").decode("utf-8")
            check("F5-42 social.csv references the followed username only",
                  "f5other" in social_text
                  and "f5other@verify.test" not in social_text)
            other_files = [n for n in names
                           if n not in ("social.csv", "README.txt")]
            check("F5-42 no non-social CSV mentions the other user",
                  all("f5other" not in archive.read(n).decode("utf-8")
                      for n in other_files))

        # ── 61  response headers (via a real authenticated request) ────────
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        response = _requests.get(f"{base}{JSON_URL}", cookies=cookies)
        check("F5-43 JSON response is no-store",
              "no-store" in response.headers.get("Cache-Control", ""),
              response.headers.get("Cache-Control", ""))
        check("F5-44 JSON response is an attachment",
              "attachment" in response.headers.get("Content-Disposition", ""))
        check("F5-45 JSON response Content-Type is application/json",
              response.headers.get("Content-Type", "").startswith(
                  "application/json"),
              response.headers.get("Content-Type", ""))
        response = _requests.get(f"{base}{CSV_URL}", cookies=cookies)
        check("F5-46 ZIP response Content-Type is application/zip",
              response.headers.get("Content-Type", "").startswith(
                  "application/zip"),
              response.headers.get("Content-Type", ""))
        check("F5-47 ZIP response is no-store",
              "no-store" in response.headers.get("Cache-Control", ""))

        # ── 9/10  responsive ───────────────────────────────────────────────
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{base}/profile/edit", wait_until="domcontentloaded")
            page.wait_for_timeout(250)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - "
                "document.documentElement.clientWidth")
            check(f"F5-48 {width}px: no horizontal overflow", overflow <= 1,
                  f"overflow={overflow}")
            for selector in ("[data-action=export-json]",
                             "[data-action=export-csv]"):
                node = page.locator(selector).first
                visible = node.count() > 0 and node.is_visible()
                in_viewport = node.evaluate(
                    "el => { const r = el.getBoundingClientRect();"
                    " return r.left >= 0 && r.right <= "
                    "document.documentElement.clientWidth + 1; }"
                ) if node.count() else False
                check(f"F5-49 {width}px: {selector} visible and within "
                      f"viewport", visible and in_viewport)

        browser.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"F5 BROWSER VERIFICATION FAILED — {len(FAILURES)} check(s):")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL F5 BROWSER CHECKS PASSED")


if __name__ == "__main__":
    main()