"""PART 1/2/3 — reproduce + diagnose the detail-page modal page-flow bug.

Boots the REAL Flask app on a throwaway SQLite temp DB (conftest pattern:
SKIP_SCHEMA_GUARD=1) and drives it with Playwright headless Chromium.

For every required viewport this records, for BOTH "Add to List" and
"Log to Diary" on BOTH a movie and a TV detail page:

  * computed position of the modal root (static vs fixed)
  * the modal's bounding box vs the viewport box
  * whether the modal contributes to document height
  * window.scrollY before/after opening
  * z-index of the modal vs the header
  * the resolved backdrop opacity

Run:  .venv/bin/python scripts/repro_detail_modals.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fd, _db_path = tempfile.mkstemp(prefix="frameiq_modals_", suffix=".db")
os.close(_fd)
os.environ.setdefault("SECRET_KEY", "modal-repro-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

MOVIE_ID = 278      # The Shawshank Redemption
TV_ID = 41727       # Banshee

VIEWPORTS = [
    (320, 568), (360, 640), (390, 844), (414, 846), (480, 960),
    (768, 1024), (1024, 768), (1280, 800), (1440, 900), (1920, 1080),
]

PROBE = """
(mid) => {
  const el = document.getElementById(mid);
  if (!el) return {missing: true};
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const doc = document.documentElement;
  const before = {h: doc.scrollHeight};
  return {
    position: cs.position,
    display: cs.display,
    zIndex: cs.zIndex,
    opacity: cs.opacity,
    background: cs.backgroundColor,
    rect: {x: Math.round(r.x), y: Math.round(r.y),
           w: Math.round(r.width), h: Math.round(r.height)},
    viewport: {w: window.innerWidth, h: window.innerHeight},
    parentTag: el.parentElement ? el.parentElement.tagName : null,
    parentId: el.parentElement ? el.parentElement.id : null,
    parentClass: el.parentElement
      ? (el.parentElement.className || '').slice(0, 90) : null,
    scrollY: window.scrollY,
    docScrollHeight: doc.scrollHeight,
    bodyScrollHeight: document.body.scrollHeight,
    headerZ: (() => {
      const h = document.querySelector('.fi-header, .fi-nav-layer, header');
      return h ? getComputedStyle(h).zIndex : null;
    })(),
    twinModals: document.querySelectorAll('#' + mid).length,
    dialogBg: (() => {
      const d = el.querySelector('.modal-content, [role="dialog"]');
      return d ? getComputedStyle(d).backgroundColor : null;
    })(),
    selectBg: (() => {
      const s = el.querySelector('select');
      return s ? getComputedStyle(s).backgroundColor : null;
    })(),
  };
}
"""


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
        target=lambda: flask_app.run(host="127.0.0.1", port=5002,
                                     debug=False, use_reloader=False),
        daemon=True).start()
    for _ in range(60):
        try:
            _requests.get("http://127.0.0.1:5002/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: dev server did not start")
        sys.exit(2)

    from models import User

    with flask_app.app_context():
        u = User(username="mrepro", email="mrepro@example.com",
                 email_verified=True)
        u.set_password("Mrepro1!")
        _db.session.add(u)
        _db.session.commit()

    findings = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        base = "http://127.0.0.1:5002"
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "mrepro")
        page.fill('input[name="password"]', "Mrepro1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")
        print("logged in ->", page.url)

        for w, h in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            for label, url, trigger, mid in [
                ("movie/list", f"{base}/movie/{MOVIE_ID}",
                 "#open-list-modal", "list-modal"),
                ("movie/diary", f"{base}/movie/{MOVIE_ID}",
                 "#open-diary-modal", "diary-modal"),
                ("tv/list", f"{base}/tv/{TV_ID}",
                 "#open-list-modal", "list-modal"),
                ("tv/diary", f"{base}/tv/{TV_ID}",
                 "#open-diary-modal", "diary-modal"),
            ]:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(300)
                before_y = page.evaluate("window.scrollY")
                page.click(trigger)
                page.wait_for_timeout(500)
                data = page.evaluate(PROBE, mid)
                data["before_y"] = before_y
                data["viewport_name"] = f"{w}x{h}"
                data["case"] = label
                findings.append(data)
        browser.close()

    for f in findings:
        print(json.dumps(f, sort_keys=True))

    with open("/tmp/modal_repro.json", "w") as fh:
        json.dump(findings, fh, indent=2)
    print(f"\nwrote {len(findings)} findings -> /tmp/modal_repro.json")


if __name__ == "__main__":
    main()