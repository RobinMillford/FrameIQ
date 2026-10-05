"""Baseline measurement of detail-page poster + hero layout (before/after).

Boot the real app on a temp SQLite DB and record, at each breakpoint, the
rendered width/height of the primary poster and the hero grid geometry for
both a movie and a TV detail page.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fd, _db_path = tempfile.mkstemp(prefix="frameiq_layout_", suffix=".db")
os.close(_fd)
os.environ.setdefault("SECRET_KEY", "layout-measure-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

MOVIE_ID = 278
TV_ID = 41727

VIEWPORTS = [(320, 568), (360, 640), (390, 844), (414, 846), (480, 960),
             (768, 1024), (1024, 768), (1280, 800), (1440, 900), (1920, 1080)]

PROBE = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const hero = document.querySelector('.hero-section');
  const hr = hero ? hero.getBoundingClientRect() : null;
  return {
    poster: {w: Math.round(r.width), h: Math.round(r.height)},
    posterTop: Math.round(r.top + window.scrollY),
    hero: hr ? {w: Math.round(hr.width), h: Math.round(hr.height)} : null,
    docW: document.documentElement.scrollWidth,
    winW: window.innerWidth,
    overflowX: document.documentElement.scrollWidth > window.innerWidth,
    naturalW: el.naturalWidth || null,
  };
}
"""


def main():
    from playwright.sync_api import sync_playwright
    from app import app as flask_app
    from models import db as _db

    flask_app.config.update(TESTING=False, WTF_CSRF_ENABLED=False,
                            RATELIMIT_ENABLED=False)
    with flask_app.app_context():
        _db.create_all()

    import threading
    import time as _time
    import requests as _requests
    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=5003,
                                     debug=False, use_reloader=False),
        daemon=True).start()
    for _ in range(60):
        try:
            _requests.get("http://127.0.0.1:5003/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: dev server did not start")
        sys.exit(2)

    from models import User
    with flask_app.app_context():
        u = User(username="lmeasure", email="lm@example.com",
                 email_verified=True)
        u.set_password("Lmeasure1!")
        _db.session.add(u)
        _db.session.commit()

    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        base = "http://127.0.0.1:5003"
        page.goto(f"{base}/login", wait_until="domcontentloaded")
        page.fill('input[name="username"]', "lmeasure")
        page.fill('input[name="password"]', "Lmeasure1!")
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded")

        for w, h in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            for kind, url, sel in [
                ("movie", f"{base}/movie/{MOVIE_ID}", ".movie-poster"),
                ("tv", f"{base}/tv/{TV_ID}", ".show-poster"),
            ]:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(250)
                d = page.evaluate(PROBE, sel)
                d["kind"] = kind
                d["viewport"] = f"{w}x{h}"
                out.append(d)
        browser.close()

    for d in out:
        print(json.dumps(d, sort_keys=True))
    with open("/tmp/layout_baseline.json", "w") as fh:
        json.dump(out, fh, indent=2)


if __name__ == "__main__":
    main()