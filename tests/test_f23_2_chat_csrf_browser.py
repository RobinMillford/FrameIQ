"""F23.2 — the globally injected CineBot widget's CSRF token delivery.

F23's adversarial review found that ``static/js/chrome.js`` read the CSRF token
and threw it away::

    const csrf = document.querySelector('meta[name="csrf-token"]')?.content;
    fetch('/chat_api', { headers: { 'Content-Type': 'application/json' }, ... })

The header therefore came only from the ``base.html`` fetch patch, which exists
only on pages extending ``base.html``. ``/chat_api`` is ``@login_required`` and
is not exempted, so ``CSRFProtect`` refused the widget on every standalone page
that loads ``chrome.js``.

These tests are behavioural. They run the REAL shipped ``chrome.js`` against
REAL rendered pages, open the panel through its own ⌘J gesture, and inspect the
headers actually attached to the request — because "the options object omits the
header" is not the same claim as "the effective request has no header", and on
base.html pages it demonstrably was not.

Isolation: ``set_content`` + ``add_script_tag``; ``fetch`` is replaced before any
shipped script runs; ``marked``/``DOMPurify`` are stubbed because they load from
a CDN and this suite must not touch the network. No Flask server, no database,
no deployed instance.
"""
import json
import pathlib
import re

import pytest

# F24: the driver import is REQUIRED in CI and skippable locally.
# See tests/browser_requirement.py for why this replaced a bare
# pytest.importorskip.
from tests.browser_requirement import require_playwright  # noqa: E402

sync_playwright = require_playwright('test_f23_2_chat_csrf_browser.py')
playwright_api = sync_playwright

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / 'static' / 'js'
TEMPLATES = ROOT / 'templates'
CHROME_JS = STATIC / 'chrome.js'

#: Installed first, so the page can never make a real request.
FETCH_RECORDER = """
window.__f23 = [];
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
    window.__f23.push({
        url: String(url),
        method: (options.method || 'GET').toUpperCase(),
        headers: headers,
        body: options.body === undefined ? null : String(options.body),
    });
    const status = window.__f23_status;
    if (status && status !== 200) {
        return Promise.resolve({ ok: false, status: status,
                                 json: function () { return Promise.resolve({}); },
                                 text: function () { return Promise.resolve(''); } });
    }
    return Promise.resolve({
        ok: true, status: 200,
        json: function () { return Promise.resolve({}); },
        text: function () { return Promise.resolve(''); },
        body: { getReader: function () {
            var done = false;
            return { read: function () {
                if (done) return Promise.resolve({ done: true });
                done = true;
                var enc = new TextEncoder().encode(
                    'data: {"type":"token","content":"pong"}\\n\\n');
                return Promise.resolve({ done: false, value: enc });
            } };
        } },
    });
};
"""

#: chrome.js renders replies through marked + DOMPurify, which base.html loads
#: from a CDN. Stub both so the render path runs without any network access.
MARKDOWN_STUBS = """
window.marked = { parse: function (t) { return String(t == null ? '' : t); } };
window.DOMPurify = { sanitize: function (t) { return String(t == null ? '' : t); } };
"""

#: Verbatim structure of the base.html global fetch patch, which the widget used
#: to depend on exclusively.
BASE_FETCH_PATCH = """
(function () {
    const getMeta = () => {
        const el = document.querySelector('meta[name="csrf-token"]');
        return el ? el.content : '';
    };
    const _fetch = window.fetch;
    window.fetch = function (url, options) {
        options = options || {};
        const method = (options.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
            const headers = new Headers(options.headers || {});
            if (!headers.has('X-CSRFToken')) {
                headers.set('X-CSRFToken', getMeta());
            }
            options = Object.assign({}, options, { headers: headers });
        }
        return _fetch.call(this, url, options);
    };
})();
"""


def _read(path):
    return pathlib.Path(path).read_text(encoding='utf-8')


def _strip_scripts(html):
    return re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S | re.I)


def _csrf_of(html):
    match = re.search(r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"', html)
    return match.group(1) if match else None


def _open_panel_and_send(page, message='ping'):
    """Drive the widget exactly as a user does: ⌘J, type, send."""
    page.keyboard.press('Control+j')
    page.wait_for_selector('.cinebot-panel', timeout=5000)
    page.fill('.cinebot-panel input', message)
    page.click('.cinebot-inputbar button')
    page.wait_for_function('() => window.__f23.length > 0', timeout=5000)
    return page.evaluate('() => window.__f23')


def _chat_requests(requests):
    # Header keys arrive lowercased when they come from a Headers object.
    out = []
    for r in requests:
        if r['url'].endswith('/chat_api'):
            out.append({**r,
                        'headers': {k.lower(): v
                                    for k, v in r['headers'].items()}})
    return out


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def chat_page(browser):
    """Factory: render real markup, inject the REAL chrome.js, return the page."""
    pages = []

    def make(html, with_base_patch=False, status=200):
        page = browser.new_page()
        pages.append(page)
        page.set_content(_strip_scripts(html))
        page.add_script_tag(content='window.__IS_AUTH__ = true;')
        page.add_script_tag(content=MARKDOWN_STUBS)
        page.add_script_tag(content=FETCH_RECORDER)
        page.evaluate('(s) => { window.__f23_status = s; }', status)
        if with_base_patch:
            page.add_script_tag(content=BASE_FETCH_PATCH)
        page.add_script_tag(content=_read(CHROME_JS))
        page.evaluate("() => document.dispatchEvent("
                      "new Event('DOMContentLoaded', { bubbles: true }))")
        return page

    yield make
    for page in pages:
        page.close()


@pytest.fixture
def session(app, db):
    """A logged-in client plus a synthetic-user factory."""
    from models import User

    def make_user(username, password='F23Pass1!'):
        with app.app_context():
            existing = User.query.filter_by(username=username).first()
            if existing is not None:
                existing.set_password(password)
                db.session.commit()
                return existing.id
            user = User(username=username, email=f'{username}@example.com',
                        email_verified=True)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            return user.id

    # Create the account BEFORE logging in: a login for a user that does not
    # exist yet silently fails, and every page then redirects to /login.
    make_user('f23chat')
    client = app.test_client()
    client.post('/login', data={'username': 'f23chat',
                                'password': 'F23Pass1!'},
                follow_redirects=True)
    return app, client, make_user


def _ensure_user(app, db, username, password='F23Pass1!'):
    """Create the account if absent; return its id. Idempotent."""
    from models import User
    with app.app_context():
        existing = User.query.filter_by(username=username).first()
        if existing is not None:
            existing.set_password(password)
            db.session.commit()
            return existing.id
        user = User(username=username, email=f'{username}@example.com',
                    email_verified=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


@pytest.fixture(autouse=True)
def _cleanup(app, db):
    """This module's users must not outlive it (the app fixture is session-wide)."""
    yield
    from models import User
    with app.app_context():
        try:
            rows = User.query.filter(User.username.like('f23chat%')).all()
            if rows:
                db.session.query(User).filter(
                    User.id.in_([u.id for u in rows])).delete(
                        synchronize_session=False)
                db.session.commit()
        except Exception:
            db.session.rollback()


# ══════════════════════════════════════════════════════════════════════════
# 1–3. The widget works and the effective request carries the header
# ══════════════════════════════════════════════════════════════════════════


def test_the_panel_is_injected_and_its_send_reaches_the_api(chat_page, session):
    """(1) The shipped chrome.js really builds the panel and fires a request."""
    app, client, make_user = session
    page = chat_page(client.get('/lists').get_data(as_text=True))

    assert page.locator('.cinebot-panel').count() == 0, (
        'the panel should only exist after the user opens it')
    requests = _open_panel_and_send(page)
    assert page.locator('.cinebot-panel').count() == 1
    assert page.evaluate(
        '() => document.querySelectorAll(".cinebot-msg.user").length') == 1, (
        'the user message was not rendered into the transcript')
    assert _chat_requests(requests), f'no /chat_api request was made: {requests}'


def test_the_effective_request_carries_the_published_token(chat_page, session):
    """(2) On a standalone page the SCRIPT must supply the header itself.

    There is no base.html fetch patch here, so nothing else can add it. This is
    the assertion that was impossible to make from source text alone.
    """
    app, client, make_user = session
    html = client.get('/lists').get_data(as_text=True)
    token = _csrf_of(html)
    assert token, 'the standalone page published no token to send'

    page = chat_page(html, with_base_patch=False)
    chat = _chat_requests(_open_panel_and_send(page))
    assert chat, 'no /chat_api request'
    assert chat[0]['headers'].get('x-csrftoken') == token, (
        f'the widget did not attach the token the page publishes: {chat[0]}')


def test_existing_headers_and_the_json_body_are_preserved(chat_page, session):
    """(3) Content-Type survives, and the body is still the JSON message."""
    app, client, make_user = session
    page = chat_page(client.get('/lists').get_data(as_text=True))

    chat = _chat_requests(_open_panel_and_send(page, 'hello cinebot'))
    assert chat, 'no /chat_api request'
    headers = chat[0]['headers']
    assert headers.get('content-type') == 'application/json', (
        f"the original Content-Type header was dropped: {headers}")
    assert chat[0]['method'] == 'POST'
    payload = json.loads(chat[0]['body'])
    # chrome.js deliberately appends page context to the FIRST message of a
    # conversation, so assert the prefix rather than exact equality.
    assert payload.get('message', '').startswith('hello cinebot'), payload
    assert set(payload) == {'message'}, f'unexpected payload keys: {payload}'


# ══════════════════════════════════════════════════════════════════════════
# 4–5. Both page contexts
# ══════════════════════════════════════════════════════════════════════════


def test_a_base_html_page_still_works_through_the_shared_patch(chat_page, session):
    """(4) The base.html fetch wrapper keeps working; the script agrees with it."""
    app, client, make_user = session
    html = client.get('/movies').get_data(as_text=True)
    assert 'csrf-token' in html, 'a base.html page must publish the token'

    page = chat_page(html, with_base_patch=True)
    chat = _chat_requests(_open_panel_and_send(page))
    assert chat, 'no /chat_api request'
    token = _csrf_of(html)
    assert chat[0]['headers'].get('x-csrftoken') == token, (
        f'base.html page lost the token: {chat[0]}')
    assert chat[0]['headers'].get('content-type') == 'application/json'


def test_a_page_without_a_patch_still_sends_the_header(chat_page, session):
    """(5) A standalone page (token published, no patch) is the F23.2 case."""
    app, client, make_user = session
    for path in ('/lists', '/watchlist', '/search?query=f23chat'):
        html = client.get(path).get_data(as_text=True)
        assert _csrf_of(html), f'{path} publishes no token'
        page = chat_page(html, with_base_patch=False)
        chat = _chat_requests(_open_panel_and_send(page))
        assert chat, f'no /chat_api request on {path}'
        assert chat[0]['headers'].get('x-csrftoken') == _csrf_of(html), (
            f'{path}: the widget sent no usable token: {chat[0]}')


def test_a_missing_token_is_omitted_rather_than_sent_empty(chat_page, session):
    """The conditional guard: with no token the field is omitted entirely.

    Measured WITHOUT the base.html patch, so what is observed is chrome.js's own
    contribution. (The patch sets an empty value when no meta tag exists; that
    is pre-existing behaviour and not what this test is about.)
    """
    app, client, _make_user = session
    html = client.get('/lists').get_data(as_text=True)
    stripped = re.sub(r'<meta[^>]*name="csrf-token"[^>]*>', '', html)

    page = chat_page(stripped, with_base_patch=False)
    chat = _chat_requests(_open_panel_and_send(page))
    assert chat, 'no /chat_api request'
    assert 'x-csrftoken' not in chat[0]['headers'], (
        'chrome.js must omit X-CSRFToken entirely when no token exists, so it '
        f"can never clobber a valid header: {chat[0]}")


def test_the_widget_does_not_clobber_a_valid_patch_supplied_header(
        chat_page, session):
    """With a token present, script and patch must agree, not fight."""
    app, client, _make_user = session
    html = client.get('/lists').get_data(as_text=True)
    token = _csrf_of(html)
    assert token

    page = chat_page(html, with_base_patch=True)
    chat = _chat_requests(_open_panel_and_send(page))
    assert chat, 'no /chat_api request'
    assert chat[0]['headers'].get('x-csrftoken') == token, (
        f'the widget replaced or blanked the valid token: {chat[0]}')


# ══════════════════════════════════════════════════════════════════════════
# 6–8. Server-side enforcement, failure handling, unchanged behaviour
# ══════════════════════════════════════════════════════════════════════════


def _chat_client(app, db):
    """A logged-in client, established with enforcement OFF.

    The login POST is itself CSRF-protected once enforcement is on, so the
    session has to exist first.
    """
    _ensure_user(app, db, 'f23chat')
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()
    client.post('/login', data={'username': 'f23chat', 'password': 'F23Pass1!'},
                follow_redirects=True)
    return client


# These are three SEPARATE tests, each with its own client, on purpose. A request
# that CSRF rejects leaves state behind that makes a later client's freshly
# scraped token fail validation -- so asserting "missing", then "invalid", then
# "valid" in one function reports a broken fix that does not exist. Isolated, each
# claim is measured honestly. (Confirmed empirically: three sequential clients
# each performing only a valid post all succeed; adding a rejected post first does
# not.)

def test_chat_rejects_a_missing_token(app, db):
    """(6a) Enforcement is unchanged: delivery was broken, not protection."""
    client = _chat_client(app, db)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = client.post('/chat_api', json={'message': 'hi'})
        assert response.status_code == 400, (
            f'a request with no CSRF token was accepted: {response.status_code}')
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_chat_rejects_an_invalid_token(app, db):
    """(6b) A forged token is refused."""
    client = _chat_client(app, db)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        response = client.post('/chat_api', headers={'X-CSRFToken': 'forged'},
                               json={'message': 'hi'})
        assert response.status_code == 400, (
            f'a forged token was accepted: {response.status_code}')
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_chat_accepts_the_token_the_page_publishes(app, db):
    """(6c) The token the page publishes is accepted by /chat_api.

    This is the server-side half of the F23.2 fix: with the header actually
    arriving, the endpoint works.
    """
    client = _chat_client(app, db)
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        token = _csrf_of(client.get('/lists').get_data(as_text=True))
        assert token, 'the page published no token to send'
        response = client.post('/chat_api', headers={'X-CSRFToken': token},
                               json={'message': 'hi'})
        assert response.status_code == 200, (
            f'the published token was rejected: {response.status_code}')
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_a_rejected_request_is_not_shown_as_a_successful_reply(chat_page, session):
    """(7) A 400 from /chat_api must surface as the failure notice, not 'pong'."""
    app, client, make_user = session
    page = chat_page(client.get('/lists').get_data(as_text=True), status=400)

    _open_panel_and_send(page, 'will fail')
    page.wait_for_timeout(300)
    bubbles = page.evaluate(
        '() => Array.from(document.querySelectorAll(".cinebot-msg.bot"))'
        '.map(e => e.textContent)')
    joined = ' '.join(bubbles)
    assert bubbles, 'no bot bubble was rendered at all'
    assert 'pong' not in joined, (
        f'a rejected request displayed a successful reply: {bubbles!r}')
    assert 'snag' in joined.lower(), (
        f'the failure notice was not shown: {bubbles!r}')


def test_anonymous_users_still_cannot_use_chat(app):
    """(8) Authentication behaviour is untouched by F23.2."""
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        client = app.test_client()
        response = client.post('/chat_api', json={'message': 'hi'})
        assert response.status_code in (302, 401, 403), response.status_code
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


# ══════════════════════════════════════════════════════════════════════════
# The structural invariant that stops this regressing page-by-page
# ══════════════════════════════════════════════════════════════════════════


def test_every_page_exposing_the_widget_publishes_a_token():
    """Any template loading chrome.js must publish a token or extend base.html.

    chrome.js builds the CineBot panel itself and ⌘J opens it, so EVERY template
    that loads the file exposes the widget -- whether or not it has a nav
    button. A standalone page with no token silently breaks the feature, which
    is exactly the F23.2 bug. This pins the invariant for future templates.
    """
    offenders = []
    for path in sorted(TEMPLATES.glob('*.html')):
        source = path.read_text(encoding='utf-8', errors='replace')
        if not re.search(r"url_for\('static',\s*filename='js/chrome\.js'\)",
                         source):
            continue
        if 'extends "base.html"' in source:
            continue  # inherits base.html's tag AND its fetch patch
        if not re.search(r'<meta[^>]*name="csrf-token"', source):
            offenders.append(path.name)

    assert offenders == [], (
        'these templates load chrome.js (so they expose CineBot) but publish no '
        f'CSRF token and do not extend base.html: {offenders}')


def test_chrome_js_does_not_send_an_empty_token():
    """The guard is conditional; assert the shape so it cannot regress."""
    source = _read(CHROME_JS)
    assert "if (csrf) headers['X-CSRFToken'] = csrf;" in source, (
        'chrome.js must only set X-CSRFToken when a non-empty token exists')
    assert 'csrf.exempt' not in source


def test_chat_api_is_not_exempted_and_still_requires_auth():
    """No server-side weakening accompanied the client-side fix."""
    chat = _read(ROOT / 'routes' / 'chat.py')
    assert 'csrf.exempt' not in chat and 'csrf_exempt' not in chat
    at = chat.index('def chat_api')
    window = chat[max(0, at - 200):at + 400]
    assert '@login_required' in window, (
        '/chat_api must still require authentication')
