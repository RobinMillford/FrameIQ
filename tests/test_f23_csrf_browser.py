"""F23 (C-7) — behavioural proof that the shipped JS actually SENDS the token.

``tests/test_f23_security_fixes.py`` asserts that the shipped sources *contain*
``X-CSRFToken`` and the meta-tag selector. Mutation testing proved that is not
enough: deleting ``headers: csrfHeaders()`` from both ``my_lists.html`` POST
sites left every one of those tests green, because the helper function still
mentions the header even when nothing calls it.

So this file runs the real page scripts in headless Chromium with ``fetch``
replaced by a recorder, performs the real user gesture, and inspects the headers
that were actually attached to the request. Reverting any one of the three
fixes makes a test here fail.

Isolation (mirrors ``test_xss_f21_browser.py``)
-----------------------------------------------
* ``set_content`` + ``add_script_tag`` — no Flask server, no database, no
  network. The recorded requests never leave the page.
* ``fetch`` is replaced before any page script runs, so nothing can reach the
  network even accidentally. (``set_content`` performs no navigation, so the
  recorder is injected as the first script tag rather than via
  ``add_init_script``, which would never fire.)
* Payloads are local sentinels; no cookie, account or deployed instance is
  touched.
"""
import json
import pathlib
import re

import pytest

# F24: the driver import is REQUIRED in CI and skippable locally.
# See tests/browser_requirement.py for why this replaced a bare
# pytest.importorskip.
from tests.browser_requirement import require_playwright  # noqa: E402

sync_playwright = require_playwright('test_f23_csrf_browser.py')
playwright_api = sync_playwright

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / 'static' / 'js'
TEMPLATES = ROOT / 'templates'

#: Installed before any page script, so the page can never make a real
#: request. ``set_content`` performs no navigation, so ``add_init_script``
#: would never fire; the HTML is stripped of every script first and this
#: recorder is injected as the first tag instead.
FETCH_RECORDER = """
window.__f23_requests = [];
try {
    if (window.name && window.name.indexOf('f23:') === 0) {
        window.__f23_requests = JSON.parse(window.name.slice(4));
    }
} catch (e) { window.__f23_requests = []; }

window.__f23_flush = function () {
    // window.name is per-tab state that survives a reload or navigation, which
    // a plain global does not. createList() calls location.reload() and
    // createSmartList() sets location.href, both of which wipe the JS context
    // the moment the request succeeds -- after the header we want to assert on
    // has already been built.
    try { window.name = 'f23:' + JSON.stringify(window.__f23_requests); }
    catch (e) { /* name is unavailable; the live array still holds the data */ }
};

window.fetch = function (url, options) {
    options = options || {};
    const headers = {};
    if (options.headers) {
        if (typeof options.headers.forEach === 'function') {
            options.headers.forEach(function (v, k) { headers[k] = v; });
        } else {
            Object.keys(options.headers).forEach(function (k) {
                headers[k] = options.headers[k];
            });
        }
    }
    window.__f23_requests.push({
        url: String(url),
        method: (options.method || 'GET').toUpperCase(),
        headers: headers,
        body: options.body === undefined ? null : String(options.body)
    });
    window.__f23_flush();
    return Promise.resolve({
        ok: true,
        status: 200,
        json: function () { return Promise.resolve({ id: 1, success: true,
                                                     is_following: true }); },
        text: function () { return Promise.resolve(''); }
    });
};
"""


def _read(path):
    return pathlib.Path(path).read_text(encoding='utf-8')


def _strip_scripts(html):
    """Remove every script tag so scripts can be injected deterministically."""
    return re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S | re.I)


def _inline_scripts(html):
    """The inline <script> bodies of the RENDERED page, in document order.

    Taken from the rendered HTML rather than the template file on purpose: the
    raw template still contains Jinja placeholders, and ``{{ ... }}`` in code
    position is a JavaScript syntax error, so the extracted block would fail to
    parse and define nothing. This is also the more faithful subject -- it is
    the script the browser actually receives.
    """
    return re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>',
                      html, re.S | re.I)


def _external_scripts(template_name):
    """Resolve ``url_for('static', ...)`` references to real files on disk."""
    source = _read(TEMPLATES / template_name)
    found = re.findall(r"url_for\('static',\s*filename='js/([^']+)'\)", source)
    seen, ordered = set(), []
    for name in found:
        if name not in seen:
            seen.add(name)
            ordered.append(STATIC / name)
    return ordered


def _load(browser, template_name, html, before_ready=None):
    """Render `html` with the template's REAL scripts, fetch already stubbed.

    `before_ready` is JavaScript evaluated AFTER the scripts are injected but
    BEFORE DOMContentLoaded is dispatched. Elements a shipped handler binds to
    at DOMContentLoaded must already exist by then, or the handler attaches to
    nothing -- which is why the watchlist control is injected here rather than
    after load.
    """
    page = browser.new_page()
    page.set_content(_strip_scripts(html))
    # Injected first, so the recorder is in place before any shipped script can
    # capture a reference to the real fetch().
    page.add_script_tag(content=FETCH_RECORDER)
    for script in _external_scripts(template_name):
        if script.exists():
            page.add_script_tag(content=_read(script))
    for body in _inline_scripts(html):
        page.add_script_tag(content=body)
    if before_ready is not None:
        page.evaluate(before_ready)
    # set_content completes BEFORE any script is injected, so DOMContentLoaded
    # has already fired by the time watchlist-priority-handler.js registers its
    # listener on it. Fire it explicitly so the shipped handlers actually attach.
    page.evaluate(
        "() => document.dispatchEvent("
        "new Event('DOMContentLoaded', { bubbles: true }))")
    return page


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(autouse=True)
def _browser_user_cleanup(app, db):
    """Remove this module's users after every test.

    The shared ``app`` fixture is session-scoped, so one SQLite file serves the
    whole run. Rows left here would survive into unrelated test modules and
    could collide with a test that asserts on user counts. ``make_user`` is
    idempotent, so recreating them on the next test is safe.
    """
    yield
    from models import User, UserFollow
    with app.app_context():
        try:
            rows = User.query.filter(
                User.username.like('f23%') | User.username.like('f231%')
            ).all()
            if rows:
                ids = [u.id for u in rows]
                db.session.query(UserFollow).filter(
                    db.or_(UserFollow.follower_id.in_(ids),
                           UserFollow.following_id.in_(ids))).delete(
                        synchronize_session=False)
                db.session.query(User).filter(User.id.in_(ids)).delete(
                    synchronize_session=False)
                db.session.commit()
        except Exception:
            db.session.rollback()


@pytest.fixture
def session(app, db):
    """A logged-in client plus helpers for creating users and lists."""
    from models import User

    def make_user(username, password='F23Pass1!'):
        with app.app_context():
            existing = User.query.filter_by(username=username).first()
            if existing is not None:
                existing.set_password(password)
                db.session.commit()
                return existing.id
            user = User(username=username,
                        email=f'{username}@example.com',
                        email_verified=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return user.id

    client = app.test_client()
    client.post('/login', data={'username': 'f23browser',
                                'password': 'F23Pass1!'},
                follow_redirects=True)
    return app, client, make_user


def _recorded(page, timeout=5000):
    """The requests the page attempted, read from reload-surviving state."""
    page.wait_for_function(
        """() => {
            if (typeof window.__f23_requests !== 'undefined'
                    && window.__f23_requests.length) { return true; }
            return !!(window.name && window.name.indexOf('f23:') === 0
                      && JSON.parse(window.name.slice(4)).length);
        }""", timeout=timeout)
    raw = page.evaluate("""() => {
        if (window.name && window.name.indexOf('f23:') === 0) {
            return window.name.slice(4);
        }
        return JSON.stringify(window.__f23_requests || []);
    }""")
    return json.loads(raw)


def _token_of(client, path):
    match = re.search(r'name="csrf-token"\s+content="([^"]+)"',
                      client.get(path).get_data(as_text=True))
    assert match, f'{path} published no csrf-token meta tag'
    return match.group(1)


# ── my_lists.html ──────────────────────────────────────────────────────────


def test_create_list_sends_the_csrf_header(browser, session, db):
    app, client, make_user = session
    make_user('f23browser')
    client.post('/login', data={'username': 'f23browser',
                                'password': 'F23Pass1!'},
                follow_redirects=True)
    token = _token_of(client, '/lists')

    html = client.get('/lists').get_data(as_text=True)
    page = _load(browser, 'my_lists.html', html)
    page.fill('#list-title', 'Browser-created list')
    page.evaluate('() => createList()')

    requests = _recorded(page)
    created = [r for r in requests if r['url'] == '/api/lists/create']
    assert created, f'the create-list mutation never fired: {requests}'
    assert created[0]['method'] == 'POST'
    assert created[0]['headers'].get('X-CSRFToken') == token, (
        f"the shipped page did not attach the published token: {created[0]}")
    page.close()


def test_create_smart_list_sends_the_csrf_header(browser, session, db):
    app, client, make_user = session
    make_user('f23browser')
    client.post('/login', data={'username': 'f23browser',
                                'password': 'F23Pass1!'},
                follow_redirects=True)
    token = _token_of(client, '/lists')

    html = client.get('/lists').get_data(as_text=True)
    page = _load(browser, 'my_lists.html', html)
    page.fill('#sl-name', 'Browser-created Smart List')
    page.evaluate('() => createSmartList()')
    requests = _recorded(page)
    smart = [r for r in requests if r['url'] == '/api/smart-lists']
    assert smart, f'the smart-list mutation never fired: {requests}'
    assert smart[0]['method'] == 'POST'
    assert smart[0]['headers'].get('X-CSRFToken') == token, (
        f'the shipped page did not attach the published token: {smart[0]}')
    page.close()


# ── watchlist.html ─────────────────────────────────────────────────────────


def test_priority_update_sends_the_csrf_header(browser, session, db):
    app, client, make_user = session
    make_user('f23browser')
    client.post('/login', data={'username': 'f23browser',
                                'password': 'F23Pass1!'},
                follow_redirects=True)
    token = _token_of(client, '/watchlist')

    html = client.get('/watchlist').get_data(as_text=True)

    # A watchlist card is only rendered when the user actually has items, so
    # reproduce the control the real template emits. It must be in the document
    # before DOMContentLoaded, because that is when the shipped handler binds.
    page = _load(browser, 'watchlist.html', html, before_ready="""() => {
        const sel = document.createElement('select');
        sel.className = 'priority-select';
        sel.dataset.mediaId = '550';
        sel.dataset.mediaType = 'movie';
        sel.innerHTML = '<option value="low">low</option>'
                      + '<option value="high" selected>high</option>';
        document.body.appendChild(sel);
    }""")

    page.evaluate("""() => {
        document.querySelector('.priority-select')
                .dispatchEvent(new Event('change', { bubbles: true }));
    }""")
    requests = _recorded(page)
    update = [r for r in requests if '/api/update_priority/' in r['url']]
    assert update, f'the priority mutation never fired: {requests}'
    assert update[0]['method'] == 'POST'
    assert update[0]['url'] == '/api/update_priority/watchlist/550/movie', (
        'the priority request changed shape — ordering or media_type drifted')
    assert update[0]['headers'].get('X-CSRFToken') == token, (
        f'the shipped handler did not attach the published token: {update[0]}')
    page.close()


# ── user_profile.html ──────────────────────────────────────────────────────


def test_follow_sends_the_csrf_header(browser, session, db):
    app, client, make_user = session
    make_user('f23browser')
    subject_id = make_user('f23subject')
    client.post('/login', data={'username': 'f23browser',
                                'password': 'F23Pass1!'},
                follow_redirects=True)
    token = _token_of(client, f'/user/{subject_id}')

    html = client.get(f'/user/{subject_id}').get_data(as_text=True)
    page = _load(browser, 'user_profile.html', html)

    assert page.locator('[data-follow-button]').count() == 1, (
        'the profile page rendered no follow button for an authenticated '
        'viewer, so the follow flow could not be exercised')

    page.click('[data-follow-button]')
    requests = _recorded(page)
    follow = [r for r in requests if r['url'].endswith('/follow')]
    assert follow, f'the follow mutation never fired: {requests}'
    assert follow[0]['method'] == 'POST'
    assert follow[0]['headers'].get('X-CSRFToken') == token, (
        f'the shipped component did not attach the published token: {follow[0]}')
    page.close()


# ── search_results.html (F23.1) ────────────────────────────────────────────
#
# search_results.html is the fourth standalone page that loads follow-button.js.
# Before F23.1 it published no CSRF token, so the real follow/unfollow action it
# offers was refused by CSRFProtect. The tests below are behavioural: they run
# the real shipped component against the real rendered page and inspect the
# headers actually sent.


def _search_html(client, target_username):
    """The rendered search page, with a follow button guaranteed to exist."""
    html = client.get(f'/search?query={target_username}').get_data(as_text=True)
    assert 'data-follow-button' in html, (
        f'the rendered search page offered no follow button for '
        f'{target_username!r}, so the follow flow could not be exercised')
    return html


def test_search_results_publishes_a_csrf_token(session):
    """F23.1 (1): the rendered page exposes a non-empty token."""
    app, client, make_user = session
    make_user('f231viewer')
    make_user('f231target')
    client.post('/login', data={'username': 'f231viewer',
                                'password': 'F23Pass1!'},
                follow_redirects=True)

    html = _search_html(client, 'f231target')
    token = re.search(r'name="csrf-token"\s+content="([^"]+)"', html)
    assert token, 'search_results.html published no csrf-token meta tag'
    assert token.group(1), 'search_results.html published an empty token'


def test_the_real_client_sends_the_published_token(browser, session):
    """F23.1 (2): the shipped follow-button.js reads the token and sends it.

    This is the assertion that matters, and it is why this test is behavioural.
    `search_results.html` contains no reference to `FollowButton` at all — the
    component self-initialises on DOMContentLoaded — so no amount of
    source-text searching on the template can establish that the header is
    actually attached to the request.
    """
    app, client, make_user = session
    make_user('f231viewer')
    target_id = make_user('f231target')
    client.post('/login', data={'username': 'f231viewer',
                                'password': 'F23Pass1!'},
                follow_redirects=True)

    html = _search_html(client, 'f231target')
    token = _token_of(client, '/search?query=f231target')

    page = _load(browser, 'search_results.html', html)
    assert page.evaluate('() => typeof FollowButton') == 'function', (
        'follow-button.js did not load, so nothing would exercise the fix')
    assert page.locator('[data-follow-button]').count() == 1

    page.click('[data-follow-button]')
    requests = _recorded(page)
    follow = [r for r in requests if r['url'].endswith('/follow')]
    assert follow, f'the follow mutation never fired: {requests}'
    assert follow[0]['url'] == f'/api/users/{target_id}/follow'
    assert follow[0]['method'] == 'POST'
    assert follow[0]['headers'].get('X-CSRFToken') == token, (
        'the shipped component did not attach the token this page publishes: '
        f'{follow[0]}')
    page.close()


def test_the_fix_did_not_regress_a_page_that_gets_its_token_from_base(
        browser, session):
    """F23.1 (5): movie_detail.html extends base.html and must keep working.

    base.html publishes the token AND installs a global fetch() patch. search
    results now publishes its own token, which must not change what happens on a
    base-derived page — in particular the component must still not overwrite a
    valid header with an empty one.
    """
    app, client, make_user = session
    make_user('f231viewer')
    target_id = make_user('f231target')
    client.post('/login', data={'username': 'f231viewer',
                                'password': 'F23Pass1!'},
                follow_redirects=True)

    page = browser.new_page()
    # base.html's own head and patch, verbatim in structure.
    page.set_content(
        '<head><meta name="csrf-token" content="base-template-token"></head>'
        '<button data-follow-button data-user-id="%s" data-following="false" '
        'data-authenticated="true">Follow</button>' % target_id)
    page.add_script_tag(content=FETCH_RECORDER)
    page.add_script_tag(content=_read(STATIC / 'follow-button.js'))
    page.evaluate("() => document.dispatchEvent("
                  "new Event('DOMContentLoaded', { bubbles: true }))")
    page.click('[data-follow-button]')

    requests = _recorded(page)
    follow = [r for r in requests if r['url'].endswith('/follow')]
    assert follow, f'the follow mutation never fired: {requests}'
    assert follow[0]['headers'].get('X-CSRFToken') == 'base-template-token', (
        'the component must use the token the page publishes, not an empty or '
        f'stale one: {follow[0]}')
    page.close()


# ── the shared-component regression this F23 change had to respect ─────────


def test_the_shared_component_does_not_override_the_base_html_patch(
        browser, session, db):
    """follow-button.js also runs on pages extending base.html.

    base.html installs a global fetch() patch that sets X-CSRFToken itself. If
    the component set the header unconditionally it would overwrite that patch
    with an EMPTY value on any page without a meta tag, breaking a page that
    currently works. This renders such a page and asserts no empty header is
    sent.
    """
    app, client, make_user = session
    make_user('f23browser')
    client.post('/login', data={'username': 'f23browser',
                                'password': 'F23Pass1!'},
                follow_redirects=True)

    page = browser.new_page()
    # A page with NO csrf meta tag — the shape that must not regress.
    page.set_content('<button data-follow-button data-user-id="7" '
                     'data-following="false" data-authenticated="true">Follow'
                     '</button>')
    page.add_script_tag(content=FETCH_RECORDER)
    page.add_script_tag(content=_read(STATIC / 'follow-button.js'))
    page.evaluate("""() => {
        document.querySelector('[data-follow-button]')
                .__fb = new FollowButton(7,
                    document.querySelector('[data-follow-button]'));
    }""")
    page.click('[data-follow-button]')
    requests = _recorded(page)
    follow = [r for r in requests if r['url'].endswith('/follow')]
    assert follow, f'the follow mutation never fired: {requests}'
    # The critical assertion: no EMPTY header that would defeat a base.html
    # patch on a page that has a real token elsewhere.
    assert follow[0]['headers'].get('X-CSRFToken') != '', (
        'follow-button.js sent an empty X-CSRFToken, which would override '
        'base.html\'s fetch patch on token-bearing pages')
    page.close()
