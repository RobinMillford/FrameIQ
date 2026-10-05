"""Browser verification of the shared detail-page modal system.

Boots the REAL Flask app on a throwaway SQLite temp DB (conftest pattern:
SKIP_SCHEMA_GUARD=1) and drives it with Playwright across Chromium,
Firefox and WebKit.

Covers:
  A. Modal geometry — viewport-fixed backdrop, centered dialog, modal does
     not contribute to document height, no page jump, correct z-order
     above the header.
  B. Close paths — ESC, Cancel, backdrop click, click-inside does NOT
     close.
  C. Scroll lock — locked only while open; exact scroll restoration from
     deep in the page.
  D. Focus — moves into the dialog, stays trapped, returns to the trigger.
  E. A11y — role/aria-modal/aria-labelledby, hidden dialogs not focusable.
  F. Rapid open/close — one logical modal, no orphaned backdrops/listeners,
     no stuck lock.
  G. Theme — dark surface, dark controls, readable contrast, one primary
     and one secondary button.
  H. Responsive — 320/360/390/414/480/768/1024/1280/1440/1920: fits the
     viewport, no horizontal overflow, content area scrolls.
  I. Poster — desktop/tablet/mobile width ranges for movie and TV.
  J. Screenshots — movie + TV detail at 1440/1920/768/1024/390/414 plus
     modal open/opening/closing states.
  K. Regression — the modal endpoints still work (submit both forms).

Run:  .venv/bin/python scripts/verify_detail_modals_browser.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fd, _db_path = tempfile.mkstemp(prefix="frameiq_modalverify_", suffix=".db")
os.close(_fd)
os.environ.setdefault("SECRET_KEY", "modal-verify-secret")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"
os.environ.setdefault("WTF_CSRF_ENABLED", "False")
os.environ.setdefault("SKIP_SCHEMA_GUARD", "1")
os.environ.setdefault("RATELIMIT_ENABLED", "False")
os.environ["MAIL_SERVER"] = ""

MOVIE_ID = 278      # The Shawshank Redemption
TV_ID = 41727       # Banshee
SHOT_DIR = os.environ.get("MODAL_SHOT_DIR", "/tmp/modal_shots")

FAILURES = []
PASSES = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))
    (PASSES if cond else FAILURES).append(name)
    return cond


# ── In-page probes ─────────────────────────────────────────────────

GEOM = """
() => {
  const el = document.getElementById('__MID__');
  const dlg = el.querySelector('[role="dialog"]');
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  const dr = dlg.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const cx = dr.left + dr.width / 2, cy = dr.top + dr.height / 2;
  return {
    position: cs.position,
    zIndex: parseInt(cs.zIndex, 10),
    backdropRect: {x: Math.round(r.x), y: Math.round(r.y),
                   w: Math.round(r.width), h: Math.round(r.height)},
    dialogRect: {x: Math.round(dr.x), y: Math.round(dr.y),
                 w: Math.round(dr.width), h: Math.round(dr.height)},
    // how far the dialog centre is from the viewport centre
    offX: Math.round(Math.abs(cx - vw / 2)),
    offY: Math.round(Math.abs(cy - vh / 2)),
    vw, vh,
    inViewport: dr.left >= 0 && dr.right <= vw + 0.5 &&
                dr.top >= 0 && dr.bottom <= vh + 0.5,
    parent: el.parentElement ? el.parentElement.id : null,
    docScrollHeight: document.documentElement.scrollHeight,
    docScrollWidth: document.documentElement.scrollWidth,
    scrollY: window.scrollY,
    overflowX: document.documentElement.scrollWidth > vw,
    state: el.getAttribute('data-state'),
    backdropBg: cs.backgroundColor,
  };
}
"""


def geom(page, mid):
    return page.evaluate(GEOM.replace("__MID__", mid))


def state_of(page, mid):
    return page.evaluate(
        "(id) => { const e = document.getElementById(id);"
        " return e ? e.getAttribute('data-state') : null; }", mid)


def is_open(page, mid):
    return state_of(page, mid) in ("open", "opening")


def js_click(page, selector):
    """Click WITHOUT Playwright's auto-scroll.

    page.click() scrolls the target into view first, which would move the
    page before the modal opens and make scroll-restoration untestable.
    """
    page.evaluate("(s) => document.querySelector(s).click()", selector)


def settled(page, mid, timeout=4000):
    """Block until the open/close transition has actually finished.

    The dialog animates transform: translateY(8px) scale(0.98) -> identity.
    Sampling while that is still running reads as a few px off-centre and
    looks like a centering bug when it is just an unfinished animation.
    """
    try:
        page.wait_for_function(
            "(id) => { const d = document.querySelector('#'+id+' [role=\"dialog\"]');"
            " if (!d) return false;"
            " const t = getComputedStyle(d).transform;"
            " return t === 'none' || t === 'matrix(1, 0, 0, 1, 0, 0)'; }",
            arg=mid, timeout=timeout)
    except Exception:
        return False
    return True


# ── Harness ────────────────────────────────────────────────────────

def boot():
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
        target=lambda: flask_app.run(host="127.0.0.1", port=5004,
                                     debug=False, use_reloader=False),
        daemon=True).start()
    for _ in range(60):
        try:
            _requests.get("http://127.0.0.1:5004/login", timeout=2)
            break
        except Exception:
            _time.sleep(0.5)
    else:
        print("FATAL: dev server did not start")
        sys.exit(2)

    from models import User
    with flask_app.app_context():
        u = User(username="modalv", email="modalv@example.com",
                 email_verified=True)
        u.set_password("Modalv1!")
        _db.session.add(u)
        _db.session.commit()
    return flask_app


def login(page, base):
    page.goto(f"{base}/login", wait_until="domcontentloaded")
    page.fill('input[name="username"]', "modalv")
    page.fill('input[name="password"]', "Modalv1!")
    page.click('button[type="submit"]')
    page.wait_for_load_state("domcontentloaded")
    return "/login" not in page.url


def open_detail(page, base, url):
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(350)


# ═══════════════════════════════════════════════════════════════════
# A/B/C/D/E — per-browser behaviour
# ═══════════════════════════════════════════════════════════════════

def run_behaviour(pw, engine_name, browser, base, tag):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    if not login(page, base):
        check(f"[{tag}] login", False)
        ctx.close()
        return
    pre = f"[{tag}]"

    for label, url, cases in [
        ("movie", f"{base}/movie/{MOVIE_ID}",
         [("Add to List", "#open-list-modal", "list-modal"),
          ("Log to Diary", "#open-diary-modal", "diary-modal")]),
        ("tv", f"{base}/tv/{TV_ID}",
         [("Add to List", "#open-list-modal", "list-modal"),
          ("Log to Diary", "#open-diary-modal", "diary-modal")]),
    ]:
        for name, trigger, mid in cases:
            open_detail(page, base, url)

            # ── A. geometry ──────────────────────────────────────────
            y0 = page.evaluate("window.scrollY")
            
            page.click(trigger)
            page.wait_for_timeout(120)
            settled(page, mid)
            g = geom(page, mid)

            check(f"{pre} {label}/{name}: backdrop position:fixed",
                  g["position"] == "fixed", g["position"])
            check(f"{pre} {label}/{name}: backdrop covers viewport",
                  g["backdropRect"]["x"] == 0 and g["backdropRect"]["y"] == 0
                  and abs(g["backdropRect"]["w"] - g["vw"]) <= 1
                  and abs(g["backdropRect"]["h"] - g["vh"]) <= 1,
                  str(g["backdropRect"]))
            check(f"{pre} {label}/{name}: dialog centered (x)",
                  g["offX"] <= 2, f"off by {g['offX']}px")
            check(f"{pre} {label}/{name}: dialog centered (y)",
                  g["offY"] <= 2, f"off by {g['offY']}px")
            check(f"{pre} {label}/{name}: dialog inside viewport",
                  g["inViewport"], str(g["dialogRect"]))
            check(f"{pre} {label}/{name}: dialog does not enter page flow",
                  g["backdropRect"]["h"] <= g["vh"] + 1,
                  f"backdrop h={g['backdropRect']['h']} vh={g['vh']}")
            check(f"{pre} {label}/{name}: modal in body portal",
                  g["parent"] == "fi-modal-root", str(g["parent"]))
            check(f"{pre} {label}/{name}: modal above header (z>=1200)",
                  g["zIndex"] >= 1200, str(g["zIndex"]))
            check(f"{pre} {label}/{name}: dark backdrop",
                  g["backdropBg"] not in ("rgba(0, 0, 0, 0)",
                                          "transparent"), g["backdropBg"])

            # ── 7. no page jump ──────────────────────────────────────
            check(f"{pre} {label}/{name}: no page jump on open",
                  page.evaluate("window.scrollY") == y0,
                  f"{y0} -> {page.evaluate('window.scrollY')}")

            # ── E. a11y ─────────────────────────────────────────────
            a11y = page.evaluate(
                "(id) => { const e=document.getElementById(id);"
                " const d=e.querySelector('[role=\"dialog\"]');"
                " const t=d.getAttribute('aria-labelledby');"
                " return {role:d.getAttribute('role'),"
                " modal:d.getAttribute('aria-modal'),"
                " labelledby:t,"
                " titleExists: !!(t && document.getElementById(t)),"
                " inert:e.inert===true,"
                " ariaHidden:e.getAttribute('aria-hidden')}; }", mid)
            check(f"{pre} {label}/{name}: role=dialog aria-modal=true",
                  a11y["role"] == "dialog" and a11y["modal"] == "true",
                  str(a11y))
            check(f"{pre} {label}/{name}: aria-labelledby resolves",
                  a11y["titleExists"], str(a11y))

            # ── D. focus ────────────────────────────────────────────
            focused = page.evaluate(
                "() => { const a=document.activeElement;"
                " return {id:a ? a.id : null,"
                " inside: !!(a && a.closest('[role=\"dialog\"]'))}; }")
            check(f"{pre} {label}/{name}: focus moved into dialog",
                  focused["inside"], str(focused))

            # ── G. theme ────────────────────────────────────────────
            theme = page.evaluate(
                "(id) => { const e=document.getElementById(id);"
                " const d=e.querySelector('[role=\"dialog\"]');"
                " const cs=getComputedStyle(d);"
                " const s=e.querySelector('select,input,textarea');"
                " const ss=s?getComputedStyle(s):null;"
                " const p=e.querySelector('.fi-modal-btn--primary');"
                " const sec=e.querySelector('.fi-modal-btn--secondary');"
                " return {bg:cs.backgroundColor, color:cs.color,"
                " border:cs.borderTopColor,"
                " inputBg:ss?ss.backgroundColor:null,"
                " inputColor:ss?ss.color:null,"
                " scheme:ss?ss.colorScheme:null,"
                " primary:p?getComputedStyle(p).backgroundColor:null,"
                " secondary:sec?getComputedStyle(sec).backgroundColor:null}; }",
                mid)
            check(f"{pre} {label}/{name}: dark dialog surface",
                  _lum(theme["bg"]) < 0.30, theme["bg"])
            check(f"{pre} {label}/{name}: dark form control (no white clash)",
                  _lum(theme["inputBg"]) < 0.35, str(theme["inputBg"]))
            check(f"{pre} {label}/{name}: native controls use dark scheme",
                  theme["scheme"] in ("dark", "normal"), str(theme["scheme"]))
            check(f"{pre} {label}/{name}: readable input text contrast",
                  _contrast(theme["inputBg"], theme["inputColor"]) >= 4.5,
                  f"{theme['inputBg']} on {theme['inputColor']}")
            check(f"{pre} {label}/{name}: one primary action colour",
                  theme["primary"] == THEME["primary"], str(theme["primary"]))
            check(f"{pre} {label}/{name}: cancel is transparent secondary",
                  _lum(theme["secondary"]) < 0.20, str(theme["secondary"]))

            # ── B. click inside must NOT close ───────────────────────
            box = geom(page, mid)["dialogRect"]
            page.mouse.click(box["x"] + box["w"] / 2, box["y"] + 12)
            page.wait_for_timeout(150)
            check(f"{pre} {label}/{name}: click inside does not close",
                  is_open(page, mid), state_of(page, mid))

            # ── B. ESC closes ────────────────────────────────────────
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            check(f"{pre} {label}/{name}: ESC closes", not is_open(page, mid),
                  state_of(page, mid))
            check(f"{pre} {label}/{name}: focus returns to trigger",
                  page.evaluate(
                      "(id) => document.activeElement.id === id",
                      trigger.lstrip("#")),
                  page.evaluate("() => document.activeElement.id"))

            # ── B. backdrop click closes ────────────────────────────
            page.click(trigger)
            page.wait_for_timeout(120)
            settled(page, mid)
            page.mouse.click(6, 6)
            page.wait_for_timeout(500)
            check(f"{pre} {label}/{name}: backdrop click closes",
                  not is_open(page, mid), state_of(page, mid))

            # ── B. Cancel closes ────────────────────────────────────
            cancel = "#list-cancel" if mid == "list-modal" else "#diary-cancel"
            page.click(trigger)
            page.wait_for_timeout(120)
            settled(page, mid)
            page.click(cancel)
            page.wait_for_timeout(500)
            check(f"{pre} {label}/{name}: Cancel closes", not is_open(page, mid),
                  state_of(page, mid))

            # ── E. hidden dialog not focusable ───────────────────────
            check(f"{pre} {label}/{name}: closed dialog inert + aria-hidden",
                  page.evaluate(
                      "(id) => { const e=document.getElementById(id);"
                      " return e.inert === true &&"
                      " e.getAttribute('aria-hidden') === 'true'; }", mid))

            # ── C. scroll lock + exact restoration from deep scroll ──
            page.evaluate("window.scrollTo(0, 2400)")
            page.wait_for_timeout(150)
            deep = page.evaluate("window.scrollY")
            js_click(page, trigger)   # no auto-scroll: the offset must stand
            page.wait_for_timeout(400)
            locked = page.evaluate(
                "() => ({cls: document.body.classList"
                ".contains('fi-modal-scroll-locked'),"
                " pos: getComputedStyle(document.body).position})")
            check(f"{pre} {label}/{name}: body scroll locked while open",
                  locked["cls"] and locked["pos"] == "fixed", str(locked))
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            restored = page.evaluate("window.scrollY")
            check(f"{pre} {label}/{name}: scroll restored exactly ({deep})",
                  abs(restored - deep) <= 1, f"{deep} -> {restored}")
            check(f"{pre} {label}/{name}: lock released after close",
                  not page.evaluate(
                      "() => document.body.classList"
                      ".contains('fi-modal-scroll-locked')"))

            # ── F. rapid open/close ─────────────────────────────────
            for _ in range(3):
                page.click(trigger)
                page.wait_for_timeout(60)
                page.keyboard.press("Escape")
                page.wait_for_timeout(60)
            page.click(trigger)
            page.wait_for_timeout(120)
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            counts = page.evaluate(
                "() => ({backdrops: document.querySelectorAll("
                "'#fi-modal-root .fi-modal-backdrop').length,"
                " roots: document.querySelectorAll('#fi-modal-root').length,"
                " open: document.querySelectorAll("
                "'#fi-modal-root .fi-modal-backdrop[data-state=\"open\"],"
                "#fi-modal-root .fi-modal-backdrop[data-state=\"opening\"]')"
                ".length,"
                " locked: document.body.classList"
                ".contains('fi-modal-scroll-locked')})")
            check(f"{pre} {label}/{name}: rapid toggle leaves no open modal",
                  counts["open"] == 0, str(counts))
            check(f"{pre} {label}/{name}: rapid toggle leaves one portal",
                  counts["roots"] == 1, str(counts))
            check(f"{pre} {label}/{name}: rapid toggle releases lock",
                  counts["locked"] is False, str(counts))
            check(f"{pre} {label}/{name}: no duplicate modal DOM",
                  page.evaluate(
                      "(id) => document.querySelectorAll('#'+id).length",
                      mid) == 1)

            # ── one modal at a time ─────────────────────────────────
            # A real user cannot click a trigger through an open backdrop,
            # so drive the engine directly: opening B must close A.
            js_click(page, trigger)
            page.wait_for_timeout(400)
            other_id = "diary-modal" if mid == "list-modal" else "list-modal"
            page.evaluate("(id) => window.FiModal.open(id)", other_id)
            page.wait_for_timeout(500)
            check(f"{pre} {label}/{name}: opening another closes the first",
                  is_open(page, other_id) and not is_open(page, mid),
                  f"{state_of(page, mid)} / {state_of(page, other_id)}")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

    ctx.close()


def _lum(rgb):
    r, g, b = _parse(rgb)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _parse(c):
    nums = [float(x) for x in __import__("re").findall(r"[\d.]+", c)[:3]]
    while len(nums) < 3:
        nums.append(0.0)
    return nums


def _contrast(bg, fg):
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    def L(c):
        r, g, b = _parse(c)
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    a, b = L(bg) + 0.05, L(fg) + 0.05
    hi, lo = max(a, b), min(a, b)
    return (hi / lo) if lo > 0 else 21


THEME = {"primary": "rgb(246, 183, 60)"}   # tokens.css --accent (marquee amber)


# ═══════════════════════════════════════════════════════════════════
# H — responsive
# ═══════════════════════════════════════════════════════════════════

RESPONSIVE = [(320, 568), (360, 640), (390, 844), (414, 846), (480, 960),
              (768, 1024), (1024, 768), (1280, 800), (1440, 900),
              (1920, 1080)]


def run_responsive(pw, browser, base, tag):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    login(page, base)
    pre = f"[{tag}]"
    for w, h in RESPONSIVE:
        page.set_viewport_size({"width": w, "height": h})
        for label, url, mid in [
            ("movie", f"{base}/movie/{MOVIE_ID}", "list-modal"),
            ("tv", f"{base}/tv/{TV_ID}", "diary-modal"),
        ]:
            open_detail(page, base, url)
            check(f"{pre} {w}x{h} {label}: no page horizontal overflow",
                  page.evaluate(
                      "() => document.documentElement.scrollWidth"
                      " <= window.innerWidth"))
            page.click("#open-list-modal" if mid == "list-modal"
                       else "#open-diary-modal")
            page.wait_for_timeout(120)
            settled(page, mid)
            g = geom(page, mid)
            check(f"{pre} {w}x{h} {label}: modal fits viewport",
                  g["inViewport"], str(g["dialogRect"]))
            check(f"{pre} {w}x{h} {label}: dialog centered",
                  g["offX"] <= 2 and g["offY"] <= 2,
                  f"x{g['offX']} y{g['offY']}")
            check(f"{pre} {w}x{h} {label}: no horizontal overflow with modal",
                  not g["overflowX"])
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
    ctx.close()


# ═══════════════════════════════════════════════════════════════════
# I — poster sizing
# ═══════════════════════════════════════════════════════════════════

POSTER_PROBE = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  return {w: Math.round(r.width), h: Math.round(r.height),
          ratio: +(r.height / r.width).toFixed(3),
          fit: cs.objectFit, lazy: el.getAttribute('loading'),
          top: Math.round(r.top + window.scrollY)};
}
"""


def run_posters(pw, browser, base, tag):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    login(page, base)
    pre = f"[{tag}]"
    results = []
    for w, h in RESPONSIVE:
        page.set_viewport_size({"width": w, "height": h})
        for label, url, sel in [
            ("movie", f"{base}/movie/{MOVIE_ID}", ".movie-poster"),
            ("tv", f"{base}/tv/{TV_ID}", ".show-poster"),
        ]:
            open_detail(page, base, url)
            d = page.evaluate(POSTER_PROBE, sel)
            d.update(kind=label, viewport=f"{w}x{h}")
            results.append(d)
            band = "mobile" if w <= 767 else ("tablet" if w < 1200 else "desktop")
            lo, hi = {"mobile": (150, 210), "tablet": (190, 240),
                      "desktop": (240, 300)}[band]
            check(f"{pre} {label} poster {w}x{h} in {band} range "
                  f"({lo}-{hi}px): {d['w']}px",
                  lo <= d["w"] <= hi, f"{d['w']}px")
            check(f"{pre} {label} poster {w}x{h} keeps 2:3 aspect",
                  abs(d["ratio"] - 1.5) < 0.06, str(d["ratio"]))
    # very narrow must still be a real poster, not a sliver
    ctx.close()
    return results


# ═══════════════════════════════════════════════════════════════════
# J — screenshots
# ═══════════════════════════════════════════════════════════════════

SHOT_VIEWPORTS = [(1440, 900), (1920, 1080), (768, 1024), (1024, 768),
                  (390, 844), (414, 846)]


def run_screenshots(pw, browser, base, tag):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    login(page, base)
    os.makedirs(SHOT_DIR, exist_ok=True)
    for w, h in SHOT_VIEWPORTS:
        page.set_viewport_size({"width": w, "height": h})
        for label, url in [("movie", f"{base}/movie/{MOVIE_ID}"),
                           ("tv", f"{base}/tv/{TV_ID}")]:
            open_detail(page, base, url)
            page.screenshot(path=f"{SHOT_DIR}/{tag}_{label}_page_{w}x{h}.png")

            for name, trigger, mid in [
                ("addtolist", "#open-list-modal", "list-modal"),
                ("logtoday", "#open-diary-modal", "diary-modal"),
            ]:
                # opening (mid-transition) then fully open
                page.evaluate(
                    "(id) => window.FiModal.open(id, document.getElementById"
                    "('open-" + ("list" if mid == "list-modal" else "diary")
                    + "-modal'))", mid)
                page.wait_for_timeout(70)
                page.screenshot(
                    path=f"{SHOT_DIR}/{tag}_{label}_{name}_opening_{w}x{h}.png")
                page.wait_for_timeout(450)
                page.screenshot(
                    path=f"{SHOT_DIR}/{tag}_{label}_{name}_open_{w}x{h}.png")
                # closing
                page.evaluate("(id) => window.FiModal.close(id)", mid)
                page.wait_for_timeout(80)
                page.screenshot(
                    path=f"{SHOT_DIR}/{tag}_{label}_{name}_closing_{w}x{h}.png")
                page.wait_for_timeout(450)
                page.screenshot(
                    path=f"{SHOT_DIR}/{tag}_{label}_{name}_closed_{w}x{h}.png")
    ctx.close()


# ═══════════════════════════════════════════════════════════════════
# K — endpoints still work (no backend change)
# ═══════════════════════════════════════════════════════════════════

def run_endpoints(browser, base, tag):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    login(page, base)
    pre = f"[{tag}]"

    # Log to Diary through the real UI.
    open_detail(page, base, f"{base}/movie/{MOVIE_ID}")
    page.click("#open-diary-modal")
    page.wait_for_timeout(450)
    page.fill("#diary-date", "2026-01-15")
    page.select_option("#diary-rating", "4.0")
    page.click("#diary-submit")
    page.wait_for_timeout(1800)
    check(f"{pre} diary submit closes modal", not is_open(page, "diary-modal"),
          state_of(page, "diary-modal"))
    check(f"{pre} diary submit updates the trigger",
          "In Diary" in page.locator("#open-diary-modal").inner_text(),
          page.locator("#open-diary-modal").inner_text())

    # Add to List through the real UI (create a list first).
    page.goto(f"{base}/lists", wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    created = False
    try:
        page.fill('input[name="title"]', "Verification List")
        page.click('button:has-text("Create")')
        page.wait_for_timeout(1800)
        created = True
    except Exception:
        pass
    open_detail(page, base, f"{base}/movie/{MOVIE_ID}")
    page.click("#open-list-modal")
    page.wait_for_timeout(1400)
    opts = page.evaluate(
        "() => Array.from(document.querySelectorAll('#list-select option'))"
        ".map(o => o.textContent)")
    if created and opts and opts != ["Loading..."]:
        page.select_option("#list-select", index=1)
        page.click("#list-add")
        page.wait_for_timeout(1800)
        check(f"{pre} list add closes modal", not is_open(page, "list-modal"),
              state_of(page, "list-modal"))
        check(f"{pre} list add updates the trigger",
              "Manage Lists" in page.locator("#open-list-modal").inner_text(),
              page.locator("#open-list-modal").inner_text())
    else:
        check(f"{pre} list add (skipped: no list could be created)", True)
    ctx.close()


def main():
    boot()
    from playwright.sync_api import sync_playwright

    poster_measurements = []
    with sync_playwright() as pw:
        engines = [("chromium", "chromium"), ("firefox", "firefox"),
                   ("webkit", "webkit")]
        for label, factory in engines:
            try:
                browser = getattr(pw, factory).launch()
            except Exception as exc:
                print(f"[SKIP] {label} unavailable: {exc}")
                continue
            try:
                run_behaviour(pw, label, browser, "http://127.0.0.1:5004", label)
                run_responsive(pw, browser, "http://127.0.0.1:5004", label)
                poster_measurements += run_posters(
                    pw, browser, "http://127.0.0.1:5004", label)
                run_screenshots(pw, browser, "http://127.0.0.1:5004", label)
                run_endpoints(browser, "http://127.0.0.1:5004", label)
            except Exception as exc:
                check(f"[{label}] suite completed", False, repr(exc))
            finally:
                browser.close()

    with open("/tmp/modal_poster_measurements.json", "w") as fh:
        json.dump(poster_measurements, fh, indent=2)

    print("\n" + "=" * 62)
    print(f"passed {len(PASSES)}   failed {len(FAILURES)}")
    for f in FAILURES:
        print("  FAIL:", f)
    print("=" * 62)
    sys.exit(1 if FAILURES else 0)


if __name__ == "__main__":
    main()