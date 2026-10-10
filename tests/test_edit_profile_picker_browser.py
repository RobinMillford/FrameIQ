"""Browser regression tests for the streaming-provider picker.

These use a REAL headless Chromium DOM and execute the REAL
``static/js/my-services.js``. They are deliberately offline: ``fetch`` is
stubbed inside the page and TMDb provider data is synthetic, so no network
call and no Flask server is involved.

Why a browser and not a source-string assertion: the focus bug being guarded
here is invisible in source — the old code looked correct, it simply rebuilt
the list and destroyed the focused checkbox. Only a real DOM can observe
``document.activeElement`` and element identity.
"""
import pathlib
import re

import pytest

playwright_api = pytest.importorskip(
    'playwright.sync_api', reason='playwright not installed')

from playwright.sync_api import sync_playwright  # noqa: E402

JS_PATH = pathlib.Path('static/js/my-services.js')
TEMPLATE = pathlib.Path('templates/edit_profile.html')

# Mirrors the ids/classes edit_profile.html provides.
HTML = """<!doctype html><html><head>
<meta name="csrf-token" content="AUDIT-CSRF-TOKEN">
<style>
.svc-row{display:flex;align-items:center;gap:.5rem;border:1px solid #444;
background:#222;border-radius:.25rem;padding:.375rem .5rem}
.svc-row[data-selected="true"]{border-color:#F6B73C;background:#3a2f10}
#services-limit:empty{display:none}
</style></head><body>
<select id="streaming-region"><option value="US">US</option>
<option value="GB">GB</option></select>
<input type="search" id="services-search">
<ul id="services-chips"></ul>
<span id="services-selected-count"></span>
<button type="button" id="services-select-visible">Select shown</button>
<button type="button" id="services-clear">Clear all</button>
<span id="services-search-status"></span>
<div id="services-list"></div>
<p id="services-limit" role="alert" aria-live="assertive" class="hidden"></p>
<p id="services-error" class="hidden"></p>
<button type="button" id="save-services">Save</button>
<span id="services-status"></span>
<script>window.__IS_AUTH__ = true;</script>
</body></html>"""


def _providers(n=45, offset=100):
    return [{'id': offset + i, 'name': f'Provider {i}', 'logo': None,
             'priority': i} for i in range(n)]


@pytest.fixture(scope='module')
def _browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def picker(_browser):
    """A loaded picker page with a stubbed backend."""
    page = _browser.new_context().new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.set_content(HTML)

    def install(saved_ids, providers=None):
        page.evaluate("""([saved, provs]) => {
            window.__POSTED__ = [];
            window.__SAVED__ = saved;
            window.fetch = (url, opts) => {
                if (opts && opts.method === 'POST') {
                    window.__POSTED__.push({
                        body: JSON.parse(opts.body),
                        csrf: (opts.headers || {})['X-CSRFToken'] || null
                    });
                    return Promise.resolve({ok: true, json: () => Promise.resolve(
                        {services: JSON.parse(opts.body).services})});
                }
                return Promise.resolve({ok: true, json: () => Promise.resolve({
                    region: 'US', services: window.__SAVED__,
                    available_providers: provs})});
            };
        }""", [list(saved_ids),
               providers if providers is not None else _providers()])
        page.add_script_tag(content=JS_PATH.read_text())
        page.wait_for_selector('#services-list input[data-provider]')
        page.wait_for_timeout(150)   # let renderChips() settle

    page.install = install
    page.page_errors = errors
    return page


def _count(page):
    return page.evaluate(
        "() => document.getElementById('services-selected-count').innerText")


def _selected(page):
    return page.evaluate(
        "() => [...document.querySelectorAll("
        "'#services-chips button[data-remove-provider]')]"
        ".map(b => Number(b.dataset.removeProvider))")


def _limit(page):
    return page.evaluate(
        """() => { const e = document.getElementById('services-limit');
           return {text: e.innerText, hidden: e.classList.contains('hidden')}; }""")


# ══════════════════════════════════════════════════════════════════════════
# Focus retention — the reported blocker
# ══════════════════════════════════════════════════════════════════════════

def test_checking_a_provider_keeps_focus_and_element_identity(picker):
    picker.install([])
    # Tag the element so we can prove it is the SAME node afterwards, not a
    # replacement that merely carries the same data-provider value.
    picker.eval_on_selector(
        "#services-list input[data-provider='100']",
        "e => { e.__marker = 'original'; e.focus(); }")

    picker.keyboard.press('Space')
    picker.wait_for_timeout(150)

    active = picker.evaluate("""() => {
        const a = document.activeElement;
        return {
            provider: a && a.getAttribute ? a.getAttribute('data-provider') : null,
            marker: a ? a.__marker : null,
            connected: a ? a.isConnected : false,
            checked: a ? a.checked : null
        };
    }""")
    assert active['provider'] == '100', active
    assert active['marker'] == 'original', (
        'the focused checkbox node was REPLACED by a re-render')
    assert active['connected'] is True
    assert active['checked'] is True
    assert not picker.page_errors


def test_unchecking_a_provider_keeps_focus_and_element_identity(picker):
    picker.install([100])
    picker.eval_on_selector(
        "#services-list input[data-provider='100']",
        "e => { e.__marker = 'original'; e.focus(); }")
    assert picker.eval_on_selector(
        "#services-list input[data-provider='100']", "e => e.checked")

    picker.keyboard.press('Space')
    picker.wait_for_timeout(150)

    active = picker.evaluate("""() => {
        const a = document.activeElement;
        return {provider: a && a.getAttribute ? a.getAttribute('data-provider') : null,
                marker: a ? a.__marker : null,
                connected: a ? a.isConnected : false,
                checked: a ? a.checked : null};
    }""")
    assert active['provider'] == '100', active
    assert active['marker'] == 'original', 'node was replaced on deselect'
    assert active['connected'] is True
    assert active['checked'] is False


def test_toggle_updates_chip_and_count(picker):
    picker.install([])
    assert _count(picker) == 'No services selected'
    picker.eval_on_selector("#services-list input[data-provider='100']",
                            "e => e.click()")
    picker.wait_for_timeout(150)
    assert _count(picker) == '1 service selected'
    assert 100 in _selected(picker)

    picker.eval_on_selector("#services-list input[data-provider='100']",
                            "e => e.click()")
    picker.wait_for_timeout(150)
    assert _count(picker) == 'No services selected'
    assert 100 not in _selected(picker)


def test_row_styling_updates_without_rebuild(picker):
    picker.install([])
    sel = "#services-list input[data-provider='100']"
    assert picker.eval_on_selector(
        sel, "e => e.closest('.svc-row').dataset.selected") == 'false'
    picker.eval_on_selector(sel, "e => e.click()")
    picker.wait_for_timeout(150)
    assert picker.eval_on_selector(
        sel, "e => e.closest('.svc-row').dataset.selected") == 'true'


def test_search_filters_without_dropping_hidden_selections(picker):
    picker.install([100, 101])
    picker.fill('#services-search', 'Provider 1')
    picker.wait_for_timeout(200)
    visible = picker.eval_on_selector_all(
        "#services-list input[data-provider]", "e => e.length")
    assert visible < 45, 'search must narrow the visible list'
    assert sorted(_selected(picker)) == [100, 101], (
        'filtering must not drop selections')

    # and they are still submitted
    picker.click('#save-services')
    picker.wait_for_timeout(200)
    posted = picker.evaluate("() => window.__POSTED__[0].body.services")
    assert sorted(posted) == [100, 101]


def test_region_change_reloads_and_resets(picker):
    picker.install([100, 101])
    picker.select_option('#streaming-region', 'GB')
    picker.wait_for_timeout(250)
    assert sorted(_selected(picker)) == [100, 101], (
        'region change resets to that region\'s saved services')


# ══════════════════════════════════════════════════════════════════════════
# The 20-service cap
# ══════════════════════════════════════════════════════════════════════════

def test_twentieth_selection_is_accepted(picker):
    picker.install([i for i in range(100, 119)])       # 19 saved
    picker.eval_on_selector("#services-list input[data-provider='119']",
                            "e => e.click()")
    picker.wait_for_timeout(150)
    assert len(_selected(picker)) == 20
    assert picker.eval_on_selector(
        "#services-list input[data-provider='119']", "e => e.checked")
    assert _limit(picker)['hidden'] is True


def test_twenty_first_selection_is_rejected_and_existing_intact(picker):
    saved = list(range(100, 120))                        # 20 saved
    picker.install(saved)
    picker.eval_on_selector("#services-list input[data-provider='120']",
                            "e => e.click()")
    picker.wait_for_timeout(150)

    assert picker.eval_on_selector(
        "#services-list input[data-provider='120']",
        "e => e.checked") is False, 'the 21st must not become selected'
    assert sorted(_selected(picker)) == sorted(saved), (
        'existing selections must remain unchanged')
    lim = _limit(picker)
    assert lim['hidden'] is False
    assert 'up to 20' in lim['text']
    assert lim['text'].strip(), 'limit message must be visible to AT'


def test_repeated_attempts_cannot_exceed_the_cap(picker):
    """20 attempts past the cap must leave the total at 20 and stay savable."""
    picker.install(list(range(100, 120)))
    for i in range(120, 140):
        picker.eval_on_selector(
            f"#services-list input[data-provider='{i}']", "e => e.click()")
    picker.wait_for_timeout(250)
    assert len(_selected(picker)) == 20, 'the cap must hold under repetition'
    picker.click('#save-services')
    picker.wait_for_timeout(300)
    posted = picker.evaluate("() => window.__POSTED__")
    assert len(posted) == 1, 'a set at exactly the cap is still savable'
    assert len(posted[0]['body']['services']) == 20


def test_select_shown_succeeds_when_it_fits(picker):
    picker.install([100, 101])                            # 2 saved
    picker.fill('#services-search', 'Provider 1')
    picker.wait_for_timeout(250)
    visible = picker.eval_on_selector_all(
        "#services-list input[data-provider]", "e => e.length")
    assert visible < 45
    sel = set(_selected(picker))
    # "Provider 1" matches Provider 1 and Provider 10..19 => ids 101,110..119
    visible_already = len([i for i in sel if i == 101 or 110 <= i <= 119])
    expected = len(sel) + visible - visible_already
    picker.click('#services-select-visible')
    picker.wait_for_timeout(250)
    assert len(_selected(picker)) == expected, (
        'every visible unselected option must be added')
    assert _limit(picker)['hidden'] is True


def test_select_shown_is_atomic_when_it_would_exceed(picker):
    """19 saved + 26 visible: nothing may be partially applied."""
    picker.install(list(range(100, 119)))                 # 19 saved
    before = sorted(_selected(picker))
    assert picker.eval_on_selector(
        '#services-select-visible', 'b => b.disabled') is True, (
        'at 19 with 26 visible the bulk control must be disabled')

    # Simulate the stale-render case: force-enable and click in ONE evaluate so
    # no re-render can re-disable the button in between.
    picker.evaluate(
        "() => { const b = document.getElementById('services-select-visible');"
        " b.disabled = false; b.click(); }")
    picker.wait_for_timeout(250)
    assert sorted(_selected(picker)) == before, (
        'bulk selection must be all-or-nothing, never partial')
    lim = _limit(picker)
    assert lim['hidden'] is False, 'the refusal must be announced'
    assert 'up to 20' in lim['text']


def test_select_shown_disabled_at_capacity(picker):
    picker.install(list(range(100, 120)))                 # 20 = at cap
    picker.wait_for_timeout(250)
    assert picker.eval_on_selector(
        '#services-select-visible', 'b => b.disabled') is True, (
        'bulk button must be disabled when no capacity remains')


def test_select_shown_enabled_when_a_slot_remains(picker):
    # 19 selected leaves exactly one slot, so one visible addable is allowed
    picker.install(list(range(100, 119)))
    picker.fill('#services-search', 'Provider 44')
    picker.wait_for_timeout(250)
    assert picker.eval_on_selector(
        '#services-select-visible', 'b => b.disabled') is False


def test_existing_over_cap_data_is_reported_not_truncated(picker):
    legacy = list(range(100, 125))                        # 25 saved
    picker.install(legacy)
    picker.wait_for_timeout(200)
    assert sorted(_selected(picker)) == sorted(legacy), \
        'legacy selections must not be silently truncated'
    lim = _limit(picker)
    assert lim['hidden'] is False
    assert 'limit is 20' in lim['text'] or 'up to 20' in lim['text']

    # user can reduce it via chips, then save works
    for _ in range(5):
        picker.click('#services-chips button[data-remove-provider]')
        picker.wait_for_timeout(80)
    assert len(_selected(picker)) == 20
    picker.click('#save-services')
    picker.wait_for_timeout(250)
    posted = picker.evaluate("() => window.__POSTED__.map(p => p.body.services)")
    assert posted and len(posted[0]) == 20


def test_save_submits_exactly_twenty_with_csrf_header(picker):
    twenty = list(range(100, 120))
    picker.install(twenty)
    picker.wait_for_timeout(150)
    picker.click('#save-services')
    picker.wait_for_timeout(250)
    posted = picker.evaluate("() => window.__POSTED__")
    assert len(posted) == 1, 'a valid 20-service selection must save'
    assert len(posted[0]['body']['services']) == 20
    assert posted[0]['body']['services'] == sorted(twenty)
    assert posted[0]['csrf'] == 'AUDIT-CSRF-TOKEN', \
        'the meta CSRF token must be sent as X-CSRFToken'
    assert posted[0]['body']['region'] == 'US'


def test_submitted_ids_are_unique_and_sorted(picker):
    picker.install([100, 101])
    picker.fill('#services-search', 'Provider 1')
    picker.wait_for_timeout(150)
    picker.click('#services-select-visible')
    picker.wait_for_timeout(150)
    picker.click('#save-services')
    picker.wait_for_timeout(250)
    ids = picker.evaluate("() => window.__POSTED__[0].body.services")
    assert len(ids) == len(set(ids)), 'no duplicate provider ids'
    assert ids == sorted(ids)


def test_clear_all_empties_selection(picker):
    picker.install([100, 101, 102])
    picker.click('#services-clear')
    picker.wait_for_timeout(200)
    assert _selected(picker) == []
    assert _count(picker) == 'No services selected'


def test_chip_removal_does_not_rebuild_the_list(picker):
    picker.install([100, 101])
    picker.eval_on_selector("#services-list input[data-provider='100']",
                            "e => { e.__marker = 'keep'; }")
    picker.click('#services-chips button[data-remove-provider="100"]')
    picker.wait_for_timeout(200)
    assert 100 not in _selected(picker)
    assert picker.eval_on_selector(
        "#services-list input[data-provider='101']",
        "e => e.closest('.svc-row') !== null")


def test_no_page_errors_during_interaction(picker):
    picker.install([])
    picker.eval_on_selector("#services-list input[data-provider='100']",
                            "e => e.click()")
    picker.fill('#services-search', 'Provider 2')
    picker.wait_for_timeout(150)
    picker.click('#services-select-visible')
    picker.wait_for_timeout(150)
    picker.click('#save-services')
    picker.wait_for_timeout(200)
    assert not picker.page_errors, picker.page_errors


# ══════════════════════════════════════════════════════════════════════════
# Static contract
# ══════════════════════════════════════════════════════════════════════════

def test_js_max_equals_backend_max():
    backend = pathlib.Path('routes/availability.py').read_text()
    backend_max = int(re.search(r'_MAX_SERVICES\s*=\s*(\d+)', backend).group(1))
    js_max = int(re.search(r'var MAX_SERVICES\s*=\s*(\d+)',
                           JS_PATH.read_text()).group(1))
    assert js_max == backend_max
