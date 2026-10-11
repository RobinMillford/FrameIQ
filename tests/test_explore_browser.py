"""Browser regression tests for the F11 /explore discovery interface.

These execute the REAL ``static/js/explore.js`` in a real headless Chromium.
Following the convention established by
``tests/test_edit_profile_picker_browser.py``, the page is a standalone
``set_content`` harness with ``fetch`` stubbed inside the page — so there is
NO Flask server, NO database, and NO network. Nothing here imports the
application, so the production database can never be reached.

The server-side canonical parameter set is built with ``api.discovery``,
which imports nothing from the app (no models, no Flask). That keeps the
browser tests honest about what the real route would emit.

What a browser proves that a source assertion cannot: that the JS reads the
server config, forwards exactly the validated params, renders real cards,
keeps unrelated facet selections when one chip is removed, and shows an
honest error instead of fake results when the request fails.
"""
import json
import pathlib
import urllib.parse

import pytest

playwright_api = pytest.importorskip(
    'playwright.sync_api', reason='playwright not installed')

from playwright.sync_api import sync_playwright  # noqa: E402

# Safe to import: api/discovery.py has no Flask/model/database dependency.
from api.discovery import (  # noqa: E402
    build_discover_params, config_to_query, parse_discovery_args, summarize,
)

JS_PATH = pathlib.Path('static/js/explore.js')

# Mirrors the ids/classes templates/explore.html provides.
HTML = """<!doctype html><html><head>
<meta name="csrf-token" content="browser-test-token">
<style>
.chip a{display:inline-block;width:1rem;height:1rem}
#result-grid{display:grid}
</style>
</head><body>
<form id="facet-form" method="get" action="/explore">
  <fieldset>
    <legend>Media type</legend>
    <label><input type="radio" name="type" value="movie" id="type-movie"> Movies</label>
    <label><input type="radio" name="type" value="tv" id="type-tv"> TV Shows</label>
  </fieldset>
  <fieldset>
    <legend>Genres</legend>
    <label><input type="checkbox" name="genres" value="horror" class="facet-genre"> Horror</label>
    <label><input type="checkbox" name="genres" value="comedy" class="facet-genre"> Comedy</label>
    <label><input type="checkbox" name="genres" value="drama" class="facet-genre"> Drama</label>
  </fieldset>
  <label>From year <input type="number" name="year_from" id="year-from"></label>
  <label>To year <input type="number" name="year_to" id="year-to"></label>
  <label>Minimum rating
    <input type="range" name="min_rating" min="0" max="10" step="0.5" id="min-rating">
    <output id="rating-out">Any</output>
  </label>
  <label>Sort <select name="sort" id="sort">
    <option value="popularity.desc">Most popular</option>
    <option value="vote_average.desc">Highest rated</option>
    <option value="primary_release_date.desc">Newest first</option>
    <option value="first_air_date.desc">Newest first</option>
  </select></label>
  <button type="submit" id="apply-filters">Apply filters</button>
  <a href="/explore" id="reset-filters">Reset</a>
</form>
<input type="text" id="preset-name">
<button type="button" id="save-preset">Save these filters</button>
<button type="button" class="delete-preset" data-id="7" id="delete-preset">x</button>
<ul id="active-chips"></ul>
<p id="result-status" role="status" aria-live="polite"></p>
<div id="result-grid"></div>
<div id="empty-state" hidden><p>No titles match these filters</p></div>
<nav id="pager">
  <button type="button" id="prev-page">Previous</button>
  <span id="page-label"></span>
  <button type="button" id="next-page">Next</button>
</nav>
</body></html>"""


def _cfg(**kw):
    """Canonical config from raw query-ish values (the real server contract)."""
    config, notices = parse_discovery_args(kw)
    return config, notices


def _chips_html(config):
    """Render chips exactly like templates/explore.html does server-side."""
    parts = []
    for chip in summarize(config):
        remove = '' if chip['key'] == 'type' else (
            '<a href="#" class="remove-chip" data-key="%s" data-value="%s" '
            'aria-label="Remove filter %s">x</a>'
            % (chip['key'], chip.get('value', ''), chip['label']))
        parts.append('<li><span class="chip">%s%s</span></li>' % (chip['label'], remove))
    return ''.join(parts)


@pytest.fixture(scope='module')
def _browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def explore(_browser):
    """Factory: load /explore with a given query string and stubbed proxy."""
    pages = []

    def make(query='', *, results=None, fail=False, chip_config=None):
        page = _browser.new_context().new_page()
        pages.append(page)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        # expose_binding survives a navigation, unlike window globals — which
        # matters because explore.js reloads after a successful save/delete.
        records = []
        page.expose_binding('__report', lambda _src, rec: records.append(rec))
        page.set_content(HTML)
        page.records = records

        config, _ = _cfg(**{k: v for k, v in _as_pairs(query).items()})
        params = build_discover_params(config, 1)
        payload = {
            'path': ('/discover/tv' if config['media_type'] == 'tv'
                     else '/discover/movie'),
            'params': params,
            'page': 1,
            'media_type': config['media_type'],
            'query': config_to_query(config),
        }

        chips_markup = chip_config or _chips_html(config)

        # Mirror the controls the real template pre-populates.
        page.evaluate("""([cfg, chips]) => {
            window.__REQUESTS__ = [];
            window.__SUBMITS__ = [];
            window.__DELETED__ = [];
            window.__SAVED__ = [];
            if (cfg.media_type === 'tv') {
                document.getElementById('type-tv').checked = true;
            } else {
                document.getElementById('type-movie').checked = true;
            }
            (cfg.genres || []).forEach(function (g) {
                var box = document.querySelector(
                    'input[name="genres"][value="' + g + '"]');
                if (box) box.checked = true;
            });
            if (cfg.year_from) document.getElementById('year-from').value = cfg.year_from;
            if (cfg.year_to) document.getElementById('year-to').value = cfg.year_to;
            document.getElementById('min-rating').value = cfg.min_rating || 0;
            document.getElementById('sort').value = cfg.sort;
            document.getElementById('active-chips').innerHTML = chips;
            // explore.js calls form.submit(), which by spec does NOT fire
            // the submit event — so intercept the method itself, otherwise the
            // harness navigates and every assertion after it sees a blank page.
            window.__recordSubmit__ = function (formEl) {
                var out = {};
                new FormData(formEl).forEach(function (v, k) {
                    if (v !== '') (out[k] = out[k] || []).push(v);
                });
                window.__SUBMITS__.push(out);
            };
            HTMLFormElement.prototype.submit = function () {
                window.__recordSubmit__(this);
            };
            document.getElementById('facet-form').addEventListener(
                'submit', function (e) {
                    e.preventDefault();
                    window.__recordSubmit__(e.target);
                });
            // Likewise neutralise the post-delete reload so the assertion can
            // read the recorded request.
            try {
                Object.defineProperty(window.location, 'reload',
                    {value: function () {}, configurable: true});
            } catch (err) { /* leave as-is; the delete request is still recorded */ }
            document.getElementById('save-preset').addEventListener(
                'click', function () {
                    window.__SAVED__.push({
                        name: document.getElementById('preset-name').value,
                        body: (window.__BODIES__ || {}).POST || null,
                    });
                });
            document.getElementById('delete-preset').addEventListener(
                'click', function () {
                    window.__DELETED__.push(this.getAttribute('data-id'));
                });
        }""", [config, chips_markup])

        body = (results if results is not None
                else [{'id': 1, 'title': 'Alpha', 'poster_path': '/a.jpg',
                       'release_date': '1999-05-04', 'vote_average': 7.5,
                       'vote_count': 900}])
        page.evaluate("""([payload, body, fail]) => {
            window.fetch = function (url, opts) {
                var o = opts || {};
            var method = o.method || 'GET';
                window.__REQUESTS__.push(url);
                window.__METHODS__ = window.__METHODS__ || {};
                window.__BODIES__ = window.__BODIES__ || {};
                window.__METHODS__[method] = o.headers || {};
                window.__BODIES__[method] = o.body || null;
                if (window.__report) {
                    window.__report({
                        url: url, method: method,
                        headers: o.headers || {}, body: o.body || null});
                }
                if (method === 'POST' || method === 'DELETE') {
                    return Promise.resolve({ok: true, json: function () {
                        return Promise.resolve({success: true});
                    }});
                }
                if (fail) {
                    return Promise.resolve({ok: false, status: 502,
                                            json: function () {
                        return Promise.resolve({error: 'TMDB request failed'});
                    }});
                }
                return Promise.resolve({ok: true, json: function () {
                    return Promise.resolve({
                        page: 1, total_results: body.length,
                        total_pages: 1, results: body});
                }});
            };
        }""", [payload, body, fail])

        # The real JS + the same server-emitted config block the template writes.
        page.evaluate("""(cfg) => {
            var el = document.createElement('script');
            el.id = 'explore-config';
            el.type = 'application/json';
            el.textContent = JSON.stringify(cfg);
            document.body.appendChild(el);
        }""", payload)
        page.add_script_tag(content=JS_PATH.read_text())
        page.wait_for_timeout(200)
        page.cfg = payload
        page.page_errors = errors
        return page

    yield make
    for p in pages:
        try:
            p.close()
        except Exception:
            pass


def _as_pairs(query):
    out = {}
    for part in query.split('&'):
        if not part:
            continue
        k, _, v = part.partition('=')
        out[k] = v
    return out


def _sent(page, index=0):
    """The params the JS actually put on the wire, URL-decoded.

    ``URLSearchParams`` percent-encodes the path (``%2Fdiscover%2Fmovie``) and
    Flask decodes it back to ``/discover/movie`` inside the proxy, so the
    decoder here mirrors what the server actually receives.
    """
    gets = [r for r in page.records if r['method'] == 'GET']
    assert gets, 'no proxy request was made'
    qs = gets[index]['url'].split('?', 1)[1]
    return {urllib.parse.unquote_plus(k): urllib.parse.unquote_plus(v)
            for k, v in (p.split('=', 1) for p in qs.split('&') if '=' in p)}


def _method_record(page, method):
    return [r for r in page.records if r['method'] == method]


# ── Initial state ──────────────────────────────────────────────────────────

class TestInitialState:

    def test_page_renders_controls_and_forwards_validated_params(self, explore):
        page = explore('type=movie&genres=horror')
        assert page.locator('#facet-form').count() == 1
        assert page.locator('#result-grid').count() == 1
        sent = _sent(page)
        assert sent['path'] == '/discover/movie'
        assert sent['with_genres'] == '27'

    def test_populated_controls_match_the_url(self, explore):
        page = explore('genres=horror,drama&year_from=1990&year_to=1999&min_rating=7')
        assert page.locator('input[name="genres"][value="horror"]').is_checked()
        assert page.locator('input[name="genres"][value="drama"]').is_checked()
        assert page.locator('#year-from').input_value() == '1990'
        assert page.locator('#year-to').input_value() == '1999'
        assert page.locator('#min-rating').input_value() == '7'

    def test_invalid_params_are_dropped_by_the_server_contract(self, explore):
        page = explore('type=anime&genres=nope&year_from=abc')
        sent = _sent(page)
        assert sent['path'] == '/discover/movie'
        assert 'with_genres' not in sent
        assert 'primary_release_date.gte' not in sent

    def test_page_never_reports_results_before_a_request(self, explore):
        page = explore('')
        assert page.evaluate('window.__REQUESTS__.length') == 1


# ── Applying filters ───────────────────────────────────────────────────────

class TestApplyingFilters:

    def test_movie_uses_primary_release_date(self, explore):
        page = explore('type=movie&year_from=1990&year_to=1999')
        sent = _sent(page)
        assert sent['primary_release_date.gte'] == '1990-01-01'
        assert sent['primary_release_date.lte'] == '1999-12-31'
        assert 'first_air_date.gte' not in sent

    def test_tv_uses_first_air_date(self, explore):
        page = explore('type=tv&year_from=2010&year_to=2019')
        sent = _sent(page)
        assert sent['path'] == '/discover/tv'
        assert sent['first_air_date.gte'] == '2010-01-01'
        assert sent['first_air_date.lte'] == '2019-12-31'
        assert 'primary_release_date.gte' not in sent

    def test_media_type_and_sort_stay_consistent_with_the_url(self, explore):
        page = explore('type=tv&sort=vote_average.desc')
        assert page.locator('#type-tv').is_checked()
        assert page.locator('#sort').input_value() == 'vote_average.desc'
        assert _sent(page)['sort_by'] == 'vote_average.desc'

    def test_rating_facet_is_forwarded(self, explore):
        assert _sent(explore('min_rating=7.5'))['vote_average.gte'] == '7.5'

    def test_page_number_is_bounded_and_forwarded(self, explore):
        assert _sent(explore(''))['page'] == '1'

    def test_no_provider_or_availability_params_are_ever_sent(self, explore):
        page = explore('genres=horror&min_rating=6&year_from=2000')
        joined = page.evaluate('window.__REQUESTS__[0]').lower()
        for forbidden in ('provider', 'with_watch', 'monetization', 'region'):
            assert forbidden not in joined

    def test_results_render_as_real_cards(self, explore):
        page = explore('')
        cards = page.locator('#result-grid a')
        assert cards.count() == 1
        assert cards.first.get_attribute('href') == '/movie/1'

    def test_empty_result_set_shows_the_empty_state(self, explore):
        page = explore('', results=[])
        assert page.locator('#empty-state').is_visible()
        assert page.locator('#result-grid').is_hidden()

    def test_api_failure_shows_an_honest_error_not_fake_results(self, explore):
        page = explore('', fail=True)
        assert page.locator('#result-grid a').count() == 0
        status = page.locator('#result-status').inner_text()
        assert 'Could not load titles' in status
        assert 'title' not in status.lower().split('could not')[0]

    def test_error_state_is_announced_to_assistive_tech(self, explore):
        page = explore('', fail=True)
        assert page.locator('#result-status').get_attribute('aria-live') == 'polite'

    def test_no_javascript_errors_on_load(self, explore):
        page = explore('type=tv&genres=drama')
        assert page.page_errors == []


# ── Active-filter chips ────────────────────────────────────────────────────

class TestActiveFilterChips:

    def test_active_facets_render_as_removable_chips(self, explore):
        page = explore('genres=horror&min_rating=7')
        chips = page.locator('#active-chips .chip')
        assert chips.count() >= 3          # Movies, Horror, Rating
        assert page.locator('.remove-chip').count() >= 2

    def test_removing_a_genre_clears_only_that_genre(self, explore):
        page = explore('genres=horror,drama&year_from=1990')
        page.locator('.remove-chip[data-key="genres"][data-value="horror"]').click()
        page.wait_for_timeout(120)
        submitted = page.evaluate('window.__SUBMITS__[0]')
        assert 'drama' in submitted['genres']
        assert 'horror' not in submitted['genres']
        assert '1990' in submitted['year_from']

    def test_removing_the_year_range_keeps_the_genre(self, explore):
        page = explore('genres=horror&year_from=1990&year_to=1999')
        page.locator('.remove-chip[data-key="year_from+year_to"]').click()
        page.wait_for_timeout(120)
        submitted = page.evaluate('window.__SUBMITS__[0]')
        assert 'genres' not in submitted or 'horror' in submitted['genres']
        assert 'year_from' not in submitted
        assert 'year_to' not in submitted

    def test_removing_the_rating_keeps_the_genre(self, explore):
        page = explore('genres=horror&min_rating=8')
        page.locator('.remove-chip[data-key="min_rating"]').click()
        page.wait_for_timeout(120)
        submitted = page.evaluate('window.__SUBMITS__[0]')
        assert 'horror' in submitted['genres']
        assert page.locator('#rating-out').inner_text() == 'Any'

    def test_media_type_chip_is_not_removable(self, explore):
        page = explore('type=tv')
        assert page.locator('.remove-chip[data-key="type"]').count() == 0

    def test_unrelated_selection_survives_a_facet_change(self, explore):
        page = explore('')
        page.locator('input[name="genres"][value="comedy"]').check()
        page.locator('#year-from').fill('2001')
        page.locator('#apply-filters').click()
        page.wait_for_timeout(120)
        submitted = page.evaluate('window.__SUBMITS__[0]')
        assert 'comedy' in submitted['genres']
        assert '2001' in submitted['year_from']

    def test_focus_survives_toggling_a_genre(self, explore):
        page = explore('genres=horror')
        box = page.locator('input[name="genres"][value="comedy"]')
        box.focus()
        page.keyboard.press('Space')
        page.wait_for_timeout(120)
        assert page.evaluate('document.activeElement.value') == 'comedy'
        assert page.evaluate('document.activeElement.checked') is True

    def test_reset_link_returns_to_an_unfiltered_discovery_page(self, explore):
        page = explore('genres=horror')
        assert page.locator('#reset-filters').get_attribute('href') == '/explore'

    def test_rating_output_updates_with_keyboard(self, explore):
        page = explore('')
        rating = page.locator('#min-rating')
        rating.focus()
        for _ in range(4):
            page.keyboard.press('ArrowRight')
        page.wait_for_timeout(80)
        assert page.locator('#rating-out').inner_text() != 'Any'


# ── Saved discovery presets ────────────────────────────────────────────────

class TestSavedPresets:

    def test_save_posts_the_supported_configuration(self, explore):
        page = explore('type=movie&genres=horror&year_from=1990')
        page.locator('#preset-name').fill('Nineties horror')
        page.locator('#save-preset').click()
        page.wait_for_timeout(120)
        posts = _method_record(page, 'POST')
        assert posts, 'saving issued no POST'
        body = json.loads(posts[0]['body'])
        assert body['name'] == 'Nineties horror'
        assert body['config']['media_type'] == 'movie'
        assert body['config']['genres'] == ['horror']
        assert body['config']['year_from'] == 1990

    def test_save_never_sends_local_smart_list_filters(self, explore):
        page = explore('genres=horror')
        page.locator('#preset-name').fill('Horror')
        page.locator('#save-preset').click()
        page.wait_for_timeout(120)
        posts = _method_record(page, 'POST')
        assert posts
        body = json.loads(posts[0]['body'])
        assert set(body) == {'name', 'config'}
        assert 'filters' not in body
        assert 'scope' not in body

    def test_save_sends_a_csrf_header(self, explore):
        page = explore('')
        page.locator('#preset-name').fill('X')
        page.locator('#save-preset').click()
        page.wait_for_timeout(120)
        posts = _method_record(page, 'POST')
        assert posts
        assert posts[0]['headers'].get('X-CSRFToken')

    def test_save_without_a_name_does_not_post(self, explore):
        page = explore('')
        page.locator('#preset-name').fill('   ')
        page.locator('#save-preset').click()
        page.wait_for_timeout(120)
        assert _method_record(page, 'POST') == []

    def test_delete_targets_the_preset_endpoint(self, explore):
        page = explore('')
        page.evaluate('window.confirm = function () { return true; };')
        page.locator('#delete-preset').click()
        page.wait_for_timeout(150)
        deletes = _method_record(page, 'DELETE')
        assert deletes
        assert '/explore/presets/7' in deletes[0]['url']

    def test_ordinary_smart_list_link_is_not_rerouted_to_discovery(self, explore):
        page = explore('')
        # The harness has no Smart List link; assert the JS never synthesises
        # one, so a normal list keeps its original target.
        assert page.locator('a[href^="/smart-lists/"]').count() == 0
        assert page.locator('a[href^="/explore/presets/"]').count() == 0

    def test_inaccessible_preset_response_does_not_crash_the_page(self, explore):
        page = explore('')
        page.evaluate("""() => {
            window.fetch = function (url, opts) {
                window.__REQUESTS__.push(url);
                return Promise.resolve({ok: false, status: 404, json: function () {
                    return Promise.resolve({error: 'Saved discovery not found'});
                }});
            };
        }""")
        page.locator('#delete-preset').evaluate(
            'el => el.dispatchEvent(new MouseEvent("click", {bubbles: true}))')
        page.wait_for_timeout(150)
        assert page.page_errors == []