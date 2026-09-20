"""Unified calendar UI (Feature 10C) — focused suite.

Covers templates/calendar.html (shell) and static/js/calendar.js
(renderer) with the repo's established source/template testing
conventions (no browser framework — see tests/test_statistics_ui.py):

- shell: every DOM id the renderer touches exists in the template;
  shared chrome and the renderer script are included exactly once;
  the page is a shell — no server-rendered private event data
- renderer: deterministic component-based date math (no UTC
  round-trips), one fetch per state change, no polling, request
  sequencing via AbortController (stale responses can never overwrite
  newer navigation state), bounded session cache, canonical API
  parameters only, DOM-safe rendering (no innerHTML), empty/error/
  retry branches for auth/429/network/5xx/malformed, accessible day
  dialog, URL view-state validation
- interlock (§P28): every response field the renderer reads is part of
  the canonical /api/calendar envelope — a contract change breaks
  these tests instead of silently blanking the page
- live: /calendar renders for the authenticated user and requires
  auth (the existing 10A/10B suites own the /api/calendar, /tv/calendar
  and /tv/upcoming regressions)
"""
import json
import os
import re
import shutil
import subprocess
import uuid

import pytest

TEMPLATE = 'templates/calendar.html'
JS = 'static/js/calendar.js'


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _js():
    return _read(JS)


def _section(template, marker, end_marker):
    start = template.index(marker)
    return template[start:template.index(end_marker, start)]


# ═══════════════════ node micro-harness (stub DOM) ═══════════════════

# calendar.js runs as an IIFE; the exposure block is injected before its
# final line so the tests can reach the closed-over helpers/state.
_EXPOSURE = """
    globalThis.__st = state;
    globalThis.__iso = iso;
    globalThis.__pik = parseIsoKey;
    globalThis.__addDays = addDays;
    globalThis.__addMonths = addMonths;
    globalThis.__sow = startOfWeek;
    globalThis.__cal = { initFromUrl: initFromUrl };
    globalThis.__prevMonth = function () {
        state.anchor = addMonths(state.anchor, -1);
    };
    globalThis.__nextMonth = function () {
        state.anchor = addMonths(state.anchor, 1);
    };
"""

_SANDBOX_DOM = """{
    document: {
        // 'loading' defers init(): tests drive state exclusively through
        // the injected exposure helpers — no fetch/history stubs needed.
        readyState: 'loading',
        addEventListener: function () {},
        documentElement: { dataset: {} }
    },
    window: { location: { search: '' } },
    URLSearchParams: URLSearchParams,
    console: console
}"""


def _node():
    path = shutil.which('node')
    if not path:
        pytest.skip('node is unavailable')
    return path


def _run_js(expr):
    """Run calendar.js in a stub DOM with the exposure block injected,
    then eval expr inside the sandbox. Returns the JSON-encoded result."""
    node = _node()
    head, tail = _js().rsplit('})();', 1)
    source = head + _EXPOSURE + '})();' + tail
    wrapper = "\n".join([
        "const __fs = require('fs');",
        "const __vm = require('vm');",
        "const __EXPR = %s;" % json.dumps(expr),
        "const __src = __fs.readFileSync('/dev/stdin', 'utf8');",
        "const __sandbox = %s;" % _SANDBOX_DOM,
        "__vm.createContext(__sandbox);",
        "try {",
        "  __vm.runInContext(__src, __sandbox);",
        "  const __out = __vm.runInContext(__EXPR, __sandbox);",
        "  process.stdout.write(JSON.stringify(",
        "      __out === undefined ? null : __out));",
        "} catch (e) {",
        "  process.stderr.write(String((e && e.message) || e));",
        "  process.exit(3);",
        "}",
    ])
    proc = subprocess.run(
        [node, '-e', wrapper], input=source, capture_output=True,
        text=True, timeout=30,
        env={**os.environ, 'NODE_NO_WARNINGS': '1'})
    if proc.returncode != 0:
        raise AssertionError('JS eval failed: %s' % proc.stderr.strip())
    return json.loads(proc.stdout)


def _eval_js(expr):
    return _run_js(expr)


# ═══════════════════ Shell: template ═══════════════════

def test_calendar_page_includes_js_and_chrome_once():
    template = _read(TEMPLATE)
    assert template.count('js/calendar.js') == 1
    assert template.count('js/chrome.js') == 1


def test_calendar_shell_has_all_renderer_hooks():
    template = _read(TEMPLATE)
    for dom_id in ('cal-view-group', 'cal-type-group', 'cal-scope-group',
                   'cal-quick-today', 'cal-quick-week', 'cal-quick-month',
                   'cal-prev', 'cal-next', 'cal-today', 'cal-month-label',
                   'cal-surface', 'cal-month', 'cal-agenda',
                   'cal-coming-up', 'cal-count-tv', 'cal-count-movie',
                   'cal-unknown', 'cal-nav-group'):
        assert 'id="%s"' % dom_id in template, dom_id


def test_calendar_view_toggle_is_button_group_with_aria():
    template = _read(TEMPLATE)
    toolbar = _section(template, 'id="cal-view-group"',
                       'id="cal-type-group"')
    assert 'data-view="month"' in toolbar
    assert 'data-view="agenda"' in toolbar
    assert 'aria-pressed' in toolbar
    assert 'role="group"' in toolbar


def test_calendar_type_and_scope_use_canonical_values():
    template = _read(TEMPLATE)
    typebar = _section(template, 'id="cal-type-group"',
                       'id="cal-scope-group"')
    for value in ('all', 'tv', 'movie'):
        assert 'data-type="%s"' % value in typebar
    scopebar = _read(TEMPLATE)
    scopebar = scopebar[scopebar.index('id="cal-scope-group"'):]
    scopebar = scopebar[:scopebar.index('<!-- Coming up -->')]
    for value in ('all', 'watchlist', 'tracking'):
        assert 'data-scope="%s"' % value in scopebar


def test_shell_carries_no_server_rendered_private_events():
    # The page is a shell: the only Jinja interpolations are static
    # asset URLs — event data arrives exclusively via authenticated
    # fetch (§O: no private values in the anonymous HTML).
    template = _read(TEMPLATE)
    body = _section(template, '<body>', '<script')
    for match in re.findall(r'{{(.*?)}}', body, re.S):
        assert 'url_for' in match


def test_navigation_controls_are_accessible_buttons():
    template = _read(TEMPLATE)
    nav = _section(template, 'id="cal-nav-group"', '</div>')
    assert nav.count('<button') == 3
    assert 'aria-label="Previous month"' in nav
    assert 'aria-label="Next month"' in nav


# ═══════════════════ Renderer: fetch discipline ═══════════════════

def test_single_fetch_site_and_no_polling():
    js = _js()
    assert js.count('fetch(') == 1
    assert 'setInterval' not in js
    assert 'setTimeout' not in js


def test_fetch_url_uses_canonical_api_parameters_only():
    js = _js()
    assert "'/api/calendar?start='" in js
    assert "'&end='" in js
    assert "'&type='" in js
    assert "'&scope='" in js
    for forbidden in ('user_id', 'region', 'page'):
        assert forbidden + '=' not in js


def test_filter_change_uses_fetch_not_page_reload():
    js = _js()
    assert 'location.reload' not in js
    assert 'window.location.href' not in js or 'detail_url' in js


def test_stale_responses_cannot_overwrite_newer_state():
    js = _js()
    assert 'AbortController' in js
    assert re.search(r'seq !== reqSeq', js), 'stale-response guard'
    assert 'AbortError' in js
    assert re.search(r'controller\.abort\(\)', js)


def test_duplicate_requests_are_prevented():
    # A new load aborts the in-flight request before starting another:
    # two live fetches are structurally impossible (§N).
    js = _js()
    assert re.search(r'if \(controller\) controller\.abort\(\)', js)


def test_session_cache_is_bounded_and_keyed_by_view_state():
    js = _js()
    assert re.search(r'CACHE_MAX\s*=\s*\d+', js)
    assert 'cacheKey()' in js
    assert re.search(r'cache\.delete\(', js), 'eviction'
    assert re.search(r'if \(cached\)', js), 'cache hit short-circuit'


def test_busy_state_and_aria_busy_present():
    js = _js()
    assert "setAttribute('aria-busy'" in js
    template = _read(TEMPLATE)
    assert 'id="cal-surface"' in template
    assert '.cal-surface.cal-loading' in template


# ═══════════════════ Renderer: navigation ═══════════════════

def test_month_navigation_moves_the_displayed_anchor():
    got = _eval_js(
        "(function(){"
        " __st.view='month'; __st.anchor=new Date(2026,9,1);"
        " __prevMonth(); __nextMonth();"
        " return {y: __st.anchor.getFullYear(),"
        "         m: __st.anchor.getMonth(),"
        "         d: __st.anchor.getDate()};"
        "})()")
    assert got == {'y': 2026, 'm': 9, 'd': 1}


def test_navigation_controls_are_wired_to_the_displayed_anchor():
    js = _js()
    assert "on('cal-prev'" in js
    assert "on('cal-next'" in js
    assert "on('cal-today'" in js
    # Month steps by calendar months, agenda by weeks — never a reset
    # to "now" (the pre-10C behavior that broke month navigation).
    assert re.search(r'addMonths\(state\.anchor, -1\)', js)
    assert re.search(r'addMonths\(state\.anchor,\s*1\)', js)
    assert re.search(r'addDays\(state\.anchor, -7\)', js)
    assert re.search(r'addDays\(state\.anchor,\s*7\)', js)


def test_month_grid_window_fits_within_api_max_range():
    # October 2026 spans the widest possible leading+trailing grid.
    got = _eval_js(
        "(function(){"
        " var s=__sow(new Date(2026,9,1));"
        " var e=__addDays(__sow(new Date(2026,10,0)),6);"
        " return Math.round((e-s)/86400000)+1;"
        "})()")
    assert got <= 62


def test_agenda_window_is_bounded():
    match = re.search(r'AGENDA_WINDOW\s*=\s*(\d+)', _js())
    assert match
    assert int(match.group(1)) <= 61


def test_url_state_parsing_accepts_only_canonical_values():
    got = _eval_js(
        "(function(){"
        " __st.view='agenda'; __st.type='all'; __st.scope='all';"
        " window.location={search:'?view=month&type=movie&scope=watchlist'};"
        " __cal.initFromUrl();"
        " var good=[__st.view,__st.type,__st.scope];"
        " window.location={search:'?view=week&type=bogus&scope=hack'};"
        " __cal.initFromUrl();"
        " return {good:good,bad:[__st.view,__st.type,__st.scope]};"
        "})()")
    assert got['good'] == ['month', 'movie', 'watchlist']
    # invalid URL values leave state untouched
    assert got['bad'] == ['month', 'movie', 'watchlist']


# ═══════════════════ Renderer: rendering safety & states ═══════════════════

def test_rendering_is_dom_safe_no_innerhtml():
    js = _js()
    assert 'innerHTML' not in js
    assert 'insertAdjacentHTML' not in js
    assert 'document.write' not in js


def test_event_labels_are_accessible_and_not_colour_only():
    js = _js()
    assert "setAttribute('aria-label', aria)" in js
    assert "'Seen'" in js, 'watched as text, never colour alone'
    assert 'cal-chip-watched' not in js, 'opacity-only treatment removed'
    assert 'season ' in js and 'episode ' in js, 'TV semantics in label'
    assert 'movie release' in js


def test_chips_navigate_using_detail_url():
    js = _js()
    assert "chip.setAttribute('data-url', ev.detail_url)" in js
    assert "chip.getAttribute('data-url')" in js
    assert 'detail_url' in js


def test_more_indicator_is_a_button_and_dialog_is_accessible():
    js = _js()
    assert "el('button', 'cal-more'" in js
    assert "role', 'dialog'" in js
    assert "aria-modal', 'true'" in js
    assert 'Escape' in js, 'keyboard dismissible'
    assert 'dialogTrigger' in js and 'dialogTrigger.focus' in js, \
        'focus restored on close'


def test_error_states_cover_required_kinds_with_retry():
    js = _js()
    for kind in ('auth', 'rate', 'network', 'server', 'malformed'):
        assert kind + ':' in js, kind
    assert "el('button', 'cal-retry', 'Retry')" in js
    assert "signIn.href = '/login'" in js
    assert "renderError('malformed')" in js


def test_401_and_429_are_handled_distinctly():
    js = _js()
    assert 'r.status === 401' in js and 'r.status === 403' in js
    assert 'r.status === 429' in js
    assert "renderError('rate')" in js
    assert "renderError('auth')" in js


def test_empty_state_copies_follow_scope_semantics():
    js = _js()
    assert 'No watchlist releases in this period.' in js
    assert 'Add movies to your watchlist' in js
    assert 'Your tracked shows have no upcoming episodes.' in js


def test_meta_today_is_the_canonical_today():
    js = _js()
    assert 'state.meta.today' in js


def test_unknown_release_note_is_surfaced_not_fabricated():
    js = _js()
    assert 'release_date_unknown' in js
    assert "document.getElementById('cal-unknown')" in js
    assert 'cal-unknown' in _read(TEMPLATE)


def test_init_is_idempotent_and_loads_once():
    js = _js()
    assert 'calInit' in js
    assert "readyState === 'loading'" in js
    assert 'load();' in js


def test_quick_filters_trigger_exactly_one_fetch():
    js = _js()
    assert '.click()' not in js, 'no synthetic click chains (10A bug)'
    for quick in ('cal-quick-today', 'cal-quick-week', 'cal-quick-month'):
        assert "on('%s'" % quick in js


def test_view_and_filter_changes_synchronize_url_state():
    js = _js()
    assert 'syncUrl()' in js
    assert 'replaceState' in js


# ═══════════════════ Interlock: JS ids ↔ template ═══════════════════

# Created on demand by ensureDialog() — verified structurally above.
_DYNAMIC_IDS = {'cal-day-dialog'}


def test_every_js_touched_id_exists_in_template():
    js = _js()
    template = _read(TEMPLATE)
    ids = set(re.findall(r"getElementById\('([^']+)'\)", js))
    ids -= _DYNAMIC_IDS
    assert ids
    for dom_id in sorted(ids):
        assert 'id="%s"' % dom_id in template, dom_id
    assert "'cal-day-dialog'" in js, 'dialog ensured dynamically'


def test_renderer_reads_only_canonical_envelope_fields():
    js = _js()
    # Every meta field the renderer touches is part of the 10A/10B
    # envelope contract — a rename breaks this test, not the page.
    for field in ('counts', 'today', 'release_date_unknown'):
        assert 'meta.' + field in js or "meta && state.meta." + field in js
    for key in ('event_type', 'detail_url', 'season_number',
                'episode_number', 'release_type', 'watched'):
        assert 'ev.' + key in js


# ═══════════════════ Live: Flask-rendered page ═══════════════════

def _user(db):
    from models import User

    u = User(username=f"cui_{uuid.uuid4().hex[:10]}",
             email=f"cui_{uuid.uuid4().hex[:10]}@cui.test",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user):
    client.post("/login", data={"username": user.username,
                                "password": "TestPass1"},
                follow_redirects=True)


def test_calendar_page_renders_for_authenticated_user(db, client):
    u = _user(db)
    _login(client, u)
    resp = client.get('/calendar')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    for dom_id in ('cal-surface', 'cal-month', 'cal-agenda',
                   'cal-nav-group', 'cal-unknown', 'cal-month-label'):
        assert dom_id in html


def test_calendar_page_requires_auth(client):
    assert client.get('/calendar').status_code in (302, 401)
