"""F35 — behavioural regressions: do the payloads actually execute?

The static half (``tests/test_xss_f35.py``) proves the code *shape*. This module
proves the *effect*, by rendering the F35-fixed code in a real browser with
harmless sentinel payloads and asserting nothing fires.

Every payload here is inert and local: it increments ``window.__XSS_FIRED``
instead of doing anything, and it is never sent to a server — responses are
stubbed in the page. No live application is exercised.

Runs only where Playwright is available; see tests/browser_requirement.py.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))

from tests.browser_requirement import require_playwright  # noqa: E402

sync_playwright = require_playwright('test_xss_f35_browser.py')

from playwright.sync_api import sync_playwright  # noqa: E402

JS = ROOT / 'static' / 'js'

# Harmless sentinels. They break a quoting context and record execution; they
# never call anything and never leave the page.
QUOTE_BREAK = 'x" onmouseover="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1'
MARKUP = '<img src=x onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1">'
JS_URL = 'javascript:window.__XSS_FIRED=(window.__XSS_FIRED||0)+1'

SENTINEL = 'window.__XSS_FIRED || 0'


def _read(name):
    return (JS / name).read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        yield pw.chromium.launch()


def _new(browser, html, scripts=()):
    page = browser.new_page()
    page.set_content(html)
    page.add_script_tag(content=_read('dom-safety.js'))
    for name in scripts:
        page.add_script_tag(content=_read(name))
    page.evaluate('window.__XSS_FIRED = 0;')
    return page


def _fired(page):
    return page.evaluate(SENTINEL)


# ── friends-activity.js — the F34 F1 file, which had no escaping at all ──────

@pytest.fixture
def fa_page(browser):
    page = _new(browser, '<div id="fa"></div>', scripts=['friends-activity.js'])
    page.evaluate("document.getElementById('fa').innerHTML = '';")
    return page


def _render_fa(page, activity):
    page.evaluate(
        """(act) => {
            const m = new FriendsActivityManager(1, 'movie');
            m.container = document.getElementById('fa');
            m.renderActivity({
                success: true, has_friends: true,
                engaging_friends_count: 1, friends_count: 3,
                activity: Object.assign(
                    {reviews: [], watched: [], watchlisted: [],
                     likes: [], tags: []}, act),
            });
        }""", activity)


def test_fa_review_text_renders_as_text(fa_page):
    _render_fa(fa_page, {'reviews': [{
        'user_id': 1, 'username': 'alice', 'user_avatar': None,
        'rating': 8, 'content': MARKUP, 'created_at': '2026-01-01T00:00:00',
    }]})
    assert _fired(fa_page) == 0, 'SCRIPT EXECUTED'
    assert fa_page.eval_on_selector_all(
        '#fa .friend-review img', 'els => els.length') == 0, (
        'review text produced a real element')
    assert '__XSS_FIRED' in fa_page.inner_text('#fa')


def test_fa_username_cannot_break_the_avatar_attribute(fa_page):
    """The F34 F1 defect: a quote in a username closed the alt attribute."""
    _render_fa(fa_page, {'reviews': [{
        'user_id': 1, 'username': QUOTE_BREAK, 'user_avatar': None,
        'rating': None, 'content': 'hello', 'created_at': '2026-01-01T00:00:00',
    }]})
    assert _fired(fa_page) == 0, 'SCRIPT EXECUTED'
    assert fa_page.eval_on_selector_all(
        '#fa [onmouseover]', 'els => els.length') == 0, \
        'the username produced a live onmouseover handler'
    assert fa_page.get_attribute('#fa img', 'alt') == QUOTE_BREAK, \
        'the username should survive as literal attribute text'


def test_fa_avatar_url_scheme_is_validated(fa_page):
    _render_fa(fa_page, {'reviews': [{
        'user_id': 1, 'username': 'alice', 'user_avatar': JS_URL,
        'rating': None, 'content': 'hi', 'created_at': '2026-01-01T00:00:00',
    }]})
    src = fa_page.get_attribute('#fa img', 'src') or ''
    assert not src.startswith('javascript:'), 'javascript: avatar survived: %r' % src
    assert _fired(fa_page) == 0


def test_fa_avatar_falls_back_when_the_url_is_unusable(fa_page):
    _render_fa(fa_page, {'reviews': [{
        'user_id': 1, 'username': 'alice', 'user_avatar': JS_URL,
        'rating': None, 'content': 'hi', 'created_at': '2026-01-01T00:00:00',
    }]})
    src = fa_page.get_attribute('#fa img', 'src') or ''
    assert 'default-avatar' in src, 'expected the local placeholder, got %r' % src


def test_fa_tag_names_render_as_text(fa_page):
    _render_fa(fa_page, {'tags': [{
        'user_id': 2, 'username': 'bob',
        'tags': [MARKUP, '<b>bold</b>'],
    }]})
    assert _fired(fa_page) == 0, 'SCRIPT EXECUTED'
    assert fa_page.eval_on_selector_all(
        '#fa .friend-tag b', 'els => els.length') == 0, \
        'a tag name produced a real <b>'
    assert '__XSS_FIRED' in fa_page.inner_text('#fa')


def test_fa_username_in_avatar_grids_renders_as_text(fa_page):
    for bucket in ('watched', 'watchlisted', 'likes'):
        _render_fa(fa_page, {bucket: [{
            'user_id': 3, 'username': QUOTE_BREAK, 'user_avatar': None,
            'rating': 7,
        }]})
        assert fa_page.eval_on_selector_all(
            '#fa [onmouseover]', 'els => els.length') == 0, bucket
        assert _fired(fa_page) == 0, 'SCRIPT EXECUTED in %s' % bucket


def test_fa_emits_no_inline_handler_attributes(fa_page):
    _render_fa(fa_page, {'reviews': [{
        'user_id': 1, 'username': 'alice', 'user_avatar': None,
        'rating': 5, 'content': 'good', 'created_at': '2026-01-01T00:00:00',
    }]})
    attrs = fa_page.eval_on_selector_all(
        '#fa *',
        'els => els.flatMap(e => Array.from(e.attributes).map(a => a.name))')
    assert not [a for a in attrs if a.startswith('on')], attrs


def test_fa_legitimate_content_still_renders(fa_page):
    """The fix must not have broken the feature."""
    _render_fa(fa_page, {'reviews': [{
        'user_id': 1, 'username': 'alice', 'user_avatar': None,
        'rating': 8, 'content': 'A genuinely good film',
        'created_at': '2026-01-01T00:00:00',
    }]})
    assert 'A genuinely good film' in fa_page.inner_text('#fa')
    assert fa_page.get_attribute('#fa a.friend-name', 'href') == '/profile/1'
    assert '★' * 8 in fa_page.inner_text('#fa')


def test_fa_empty_state_is_preserved(fa_page):
    page_html = fa_page.evaluate(
        """() => {
            const m = new FriendsActivityManager(1, 'movie');
            m.container = document.getElementById('fa');
            m.renderActivity({success: true, has_friends: true,
                engaging_friends_count: 0, friends_count: 0,
                activity: {reviews: [], watched: [], watchlisted: [],
                           likes: [], tags: []}});
            return document.getElementById('fa').innerHTML;
        }""")
    assert 'None of your friends' in page_html
    assert 'Be the first to review it' in page_html


def test_fa_no_friends_state_is_preserved(fa_page):
    page_html = fa_page.evaluate(
        """() => {
            const m = new FriendsActivityManager(1, 'movie');
            m.container = document.getElementById('fa');
            m.renderActivity({success: true, has_friends: false,
                activity: {reviews: [], watched: [], watchlisted: [],
                           likes: [], tags: []}});
            return document.getElementById('fa').innerHTML;
        }""")
    assert 'Follow other users' in page_html
    assert 'discover-users' in page_html


# ── media-comments.js — the F34 F2 sink ─────────────────────────────────────

@pytest.fixture
def mc_page(browser):
    return _new(browser, '<div id="media-comments-list"></div>',
                scripts=['media-comments.js'])


def _render_comments(page, comments):
    page.evaluate(
        """(list) => {
            const m = new MediaCommentsManager(1, 'movie');
            m.comments = list;
            m.renderComments();
        }""", comments)


def test_mc_username_cannot_break_the_profile_href(mc_page):
    """The F34 F2 defect: ``href="/profile/${username}"`` was unescaped."""
    _render_comments(mc_page, [{
        'id': 1, 'user': {'username': QUOTE_BREAK}, 'content': 'hello',
        'created_at': '2026-01-01T00:00:00', 'updated_at': None,
        'is_author': False,
    }])
    assert _fired(mc_page) == 0, 'SCRIPT EXECUTED'
    assert mc_page.eval_on_selector_all(
        '#media-comments-list [onmouseover]', 'els => els.length') == 0, \
        'the username produced a live onmouseover handler'
    href = mc_page.get_attribute('#media-comments-list a', 'href')
    assert href and href.startswith('/profile/'), href
    assert '"' not in href and ' ' not in href, href


def test_mc_username_is_shown_literally(mc_page):
    _render_comments(mc_page, [{
        'id': 1, 'user': {'username': QUOTE_BREAK}, 'content': 'hello',
        'created_at': '2026-01-01T00:00:00', 'updated_at': None,
        'is_author': False,
    }])
    assert QUOTE_BREAK in mc_page.inner_text('#media-comments-list')


def test_mc_comment_content_renders_as_text(mc_page):
    _render_comments(mc_page, [{
        'id': 1, 'user': {'username': 'alice'}, 'content': MARKUP,
        'created_at': '2026-01-01T00:00:00', 'updated_at': None,
        'is_author': False,
    }])
    assert _fired(mc_page) == 0, 'SCRIPT EXECUTED'
    assert '__XSS_FIRED' in mc_page.inner_text('#media-comments-list')


def test_mc_author_buttons_have_no_inline_handler(mc_page):
    _render_comments(mc_page, [{
        'id': 7, 'user': {'username': 'alice'}, 'content': 'mine',
        'created_at': '2026-01-01T00:00:00', 'updated_at': None,
        'is_author': True,
    }])
    attrs = mc_page.eval_on_selector_all(
        '#media-comments-list button',
        'els => els.flatMap(e => Array.from(e.attributes)'
        '.map(a => a.name))')
    assert not [a for a in attrs if a.startswith('on')], attrs
    assert mc_page.eval_on_selector_all(
        '#media-comments-list button', 'els => els.length') == 2, \
        'the Edit/Delete buttons must still be rendered'


def test_mc_legitimate_comment_still_renders(mc_page):
    _render_comments(mc_page, [{
        'id': 1, 'user': {'username': 'alice'}, 'content': 'Great film',
        'created_at': '2026-01-01T00:00:00', 'updated_at': None,
        'is_author': False,
    }])
    assert 'Great film' in mc_page.inner_text('#media-comments-list')
    assert mc_page.get_attribute('#media-comments-list a', 'href') == '/profile/alice'
    assert mc_page.inner_text('#media-comments-list').strip().startswith('A')


def test_mc_empty_state_is_preserved(mc_page):
    mc_page.evaluate(
        """() => {
            const m = new MediaCommentsManager(1, 'movie');
            m.comments = [];
            m.renderComments();
        }""")
    assert 'No comments yet' in mc_page.inner_text('#media-comments-list')


# ── notifications.js — the F34 F3 sink ──────────────────────────────────────

NOTIF_DOM = """
<div id="notif-root">
  <button id="notif-bell"></button>
  <div id="notif-panel" class="hidden"><ul id="notif-list"></ul>
    <button id="notif-mark-all"></button>
  </div>
  <span id="notif-badge" class="hidden"></span>
</div>
"""


@pytest.fixture
def notif_page(browser):
    page = _new(browser, NOTIF_DOM, scripts=['notifications.js'])
    page.evaluate(
        """(rows) => {
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve(
                    rows.length === 1 && rows[1] !== undefined
                        ? rows[0]
                        : {unread_count: rows.length, notifications: rows}),
            });
        }""", [{'unread_count': 1, 'notifications': []}])
    return page


def _load_notifications(page, notifications):
    page.evaluate(
        """(list) => {
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve(
                    {unread_count: list.length, notifications: list}),
            });
        }""", notifications)
    page.click('#notif-bell')
    page.wait_for_timeout(150)


def _row(**kwargs):
    row = {'id': 1, 'title': 't', 'body': 'b', 'read': False,
           'created_at': '2026-01-01T00:00:00', 'target_url': None,
           'season': None, 'episode': None}
    row.update(kwargs)
    return row


def test_notif_javascript_target_url_is_refused(notif_page):
    """F34 F3: ``esc()`` left the quote intact and never checked the scheme."""
    _load_notifications(notif_page, [_row(target_url=JS_URL)])
    hrefs = notif_page.eval_on_selector_all(
        '#notif-list a', 'els => els.map(e => e.getAttribute("href"))')
    for href in hrefs:
        assert not (href or '').startswith('javascript:'), \
            'a javascript: target survived: %r' % href
    assert _fired(notif_page) == 0, 'SCRIPT EXECUTED'


def test_notif_malformed_target_url_renders_inert(notif_page):
    _load_notifications(notif_page, [_row(target_url='//evil.example/x')])
    hrefs = notif_page.eval_on_selector_all(
        '#notif-list a', 'els => els.map(e => e.getAttribute("href"))')
    for href in hrefs:
        assert not (href or '').startswith('//'), href
    assert _fired(notif_page) == 0


def test_notif_legitimate_target_still_links(notif_page):
    """The feature must survive: real destinations still render as links."""
    _load_notifications(notif_page, [
        _row(target_url='/watch/tv/1399/1/2')])
    href = notif_page.get_attribute('#notif-list a', 'href')
    assert href == '/watch/tv/1399/1/2', href
    assert 't' in notif_page.inner_text('#notif-list')


def test_notif_row_without_target_is_not_a_link(notif_page):
    _load_notifications(notif_page, [_row(target_url=None)])
    assert notif_page.eval_on_selector_all(
        '#notif-list a', 'els => els.length') == 0


def test_notif_title_and_body_render_as_text(notif_page):
    _load_notifications(notif_page, [
        _row(title=MARKUP, body=MARKUP, target_url=None)])
    assert _fired(notif_page) == 0, 'SCRIPT EXECUTED'
    assert notif_page.eval_on_selector_all(
        '#notif-list img', 'els => els.length') == 0


def test_notif_emits_no_inline_handler_attributes(notif_page):
    _load_notifications(notif_page, [_row(target_url='/watch/tv/1/1/1')])
    attrs = notif_page.eval_on_selector_all(
        '#notif-list *',
        'els => els.flatMap(e => Array.from(e.attributes).map(a => a.name))')
    assert not [a for a in attrs if a.startswith('on')], attrs


# ── F37: tv_dashboard poster URLs must not leave the TMDb image origin ────────
#
# These drive the REAL dashboard render path (`loadUnfinishedShows`), not a
# standalone URL helper, so a regression that reintroduces a raw concatenation
# in the template is caught.
#
# Every request is intercepted with page.route() and answered locally: images
# are fulfilled with a 1x1 PNG so `onerror` cannot rewrite the `src` mid-assert,
# and nothing is ever sent to a remote host.

#: 1x1 transparent PNG — lets an <img> "succeed" without any network.
PIXEL = bytes.fromhex(
    '89504e470d0a1a0a0000000d494844520000000100000001080600000'
    '01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4'
    '0000000049454e44ae426082')

DASHBOARD_DOM = """
<div id="unfinished-shows"></div>
<div id="recently-completed"></div>
<div id="continue-watching"></div>
<div id="upcoming-week"></div>
<div id="quick-stats"></div>
<div id="calendar-view"></div>
<div id="empty-state" class="hidden"></div>
"""


@pytest.fixture
def dash(browser):
    """A page with the real tv_dashboard inline script and a request trap."""
    page = browser.new_page()
    attempted = []

    def _trap(route):
        attempted.append(route.request.url)
        is_image = route.request.resource_type == 'image'
        route.fulfill(status=200,
                      content_type='image/png' if is_image else 'text/html',
                      body=PIXEL if is_image else b'')

    page.route('**/*', _trap)
    page.set_content(DASHBOARD_DOM)
    # Record the markup the template ASSIGNS, before the browser can alter it.
    # Reading `innerHTML` back is not equivalent: the rendered markup carries
    # `onerror="this.src='...'"`, so an image the browser refuses to load
    # rewrites its own src attribute and the read-back no longer shows what the
    # template produced. Chromium refuses subresource requests whose URL carries
    # embedded credentials, which made a credentialed poster look correctly
    # rejected when it had in fact been emitted. Serialising the assignment is
    # the only faithful view.
    # NOTE: page.evaluate, not add_init_script — add_init_script only applies
    # to later navigations, so it would never run on this set_content page.
    page.evaluate(RECORD_INNER_HTML)
    page.add_script_tag(content=_read('dom-safety.js'))
    page.add_script_tag(content=_dashboard_script())
    page.evaluate('window.__XSS_FIRED = 0;')
    page._attempted = attempted
    return page


#: Installed before the template script is added to the page.
RECORD_INNER_HTML = """
(() => {
  if (window.__innerHtmlRecorded) return;
  window.__innerHtmlRecorded = true;
  window.__rawHtml = [];
  const desc = Object.getOwnPropertyDescriptor(
      Element.prototype, 'innerHTML');
  Object.defineProperty(Element.prototype, 'innerHTML', {
    configurable: true,
    get: desc.get,
    set(value) {
      window.__rawHtml.push(String(value));
      desc.set.call(this, value);
    },
  });
})();
"""


def _dashboard_script():
    """The shipped tv_dashboard inline script, Jinja removed."""
    from template_script_syntax import javascript_blocks, strip_jinja
    source = strip_jinja(
        (ROOT / 'templates' / 'tv_dashboard.html').read_text(encoding='utf-8'))
    return '\n'.join(javascript_blocks(source))


def _render_unfinished(page, shows):
    page.evaluate(
        """(rows) => {
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({shows: rows}),
            });
        }""", shows)
    page.evaluate('loadUnfinishedShows()')
    page.wait_for_timeout(120)


def _show(poster_path, **kw):
    row = {'show_id': 1, 'name': 'Some Show', 'poster_path': poster_path,
           'status': 'watching', 'progress_percent': 10,
           'last_episode': None, 'next_episode': None, 'watch_url': None}
    row.update(kw)
    return row


def _poster_srcs(page):
    return page.eval_on_selector_all(
        '#unfinished-shows img',
        'els => els.map(e => e.getAttribute("src"))')


def _emitted_srcs(page, selector='#unfinished-shows'):
    """Read the src values the TEMPLATE actually wrote.

    `_poster_srcs` reads the live DOM, which is not trustworthy here: the
    markup carries `onerror="this.src='/static/images/no-image.png'"`, so any
    image the browser refuses to load — notably one with embedded credentials,
    which Chromium blocks outright — silently rewrites its own src and hides
    whatever the template emitted. Asserting on innerHTML is what makes the
    validator's behaviour observable.
    """
    return page.eval_on_selector_all(
        selector + ' img',
        'els => els.map(e => e.getAttribute("src"))')


def _emitted_html(page, selector=None):
    """The markup the template assigned, captured before the DOM could change."""
    del selector  # recorded globally; the selector is not needed
    return page.evaluate('() => (window.__rawHtml || []).join("")')


# 1. the ordinary case: a relative TMDb path gets the fixed origin
def test_dash_relative_poster_uses_the_fixed_origin(dash):
    _render_unfinished(dash, [_show('/abc123.jpg')])
    assert _emitted_srcs(dash) == [
        'https://image.tmdb.org/t/p/w200/abc123.jpg']


# 2. a legitimate absolute TMDb URL is accepted (api/continue_watching stores these)
def test_dash_valid_tmdb_absolute_poster_is_kept(dash):
    _render_unfinished(dash, [
        _show('https://image.tmdb.org/t/p/w500/xyz.jpg')])
    assert _emitted_srcs(dash) == ['https://image.tmdb.org/t/p/w500/xyz.jpg']


# 3. THE FINDING: an off-host https URL must be refused, and never requested
def test_dash_off_host_poster_is_refused_and_never_requested(dash):
    _render_unfinished(dash, [
        _show('https://attacker.example/pixel.png'),
        _show('https://image.tmdb.org.evil.example/x.jpg'),
        _show('https://user:pw@image.tmdb.org/x.jpg'),
    ])
    html = _emitted_html(dash)
    for hostile in ('attacker.example', 'evil.example', 'user:pw@'):
        assert hostile not in html, (
            'the template emitted a rejected value %r: %s' % (hostile, html))
    assert _emitted_srcs(dash) == ['/static/images/no-image.png'] * 3, html
    assert not any('attacker.example' in u for u in dash._attempted), (
        'a request left for an attacker host: %r' % dash._attempted)
    assert not any('evil.example' in u for u in dash._attempted), (
        'a request left for a lookalike host: %r' % dash._attempted)


def test_dash_plaintext_tmdb_poster_is_refused(dash):
    """http:// is refused rather than silently upgraded."""
    _render_unfinished(dash, [_show('http://image.tmdb.org/x.jpg')])
    assert _emitted_srcs(dash) == ['/static/images/no-image.png']
    assert 'image.tmdb.org' not in _emitted_html(dash)


# 4. protocol-relative and malformed values fall back safely
@pytest.mark.parametrize('value', [
    '//evil.example/pixel.png',
    'javascript:alert(1)',
    '/../../etc/passwd',
    '',
    None,
])
def test_dash_malformed_poster_falls_back(dash, value):
    _render_unfinished(dash, [_show(value)])
    assert _poster_srcs(dash) == ['/static/images/no-image.png'], value
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'


def test_dash_poster_cannot_break_out_of_the_src_attribute(dash):
    """A quote in the stored value must not become markup."""
    _render_unfinished(dash, [
        _show('x" onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1')])
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'
    assert dash.eval_on_selector_all(
        '#unfinished-shows img[onerror^="window.__XSS_FIRED"]',
        'els => els.length') == 0, 'the payload produced a live onerror handler'


# 5. existing behaviour is preserved
def test_dash_empty_state_is_preserved(dash):
    _render_unfinished(dash, [])
    assert dash.eval_on_selector_all(
        '#unfinished-shows img', 'els => els.length') == 0


def test_dash_layout_and_links_are_preserved(dash):
    _render_unfinished(
        dash, [_show('/abc123.jpg', show_id=77, name='Stranger Things')])
    href = dash.get_attribute('#unfinished-shows a', 'href')
    assert href == '/tv/77', href
    assert 'Stranger Things' in dash.inner_text('#unfinished-shows')
    assert dash.eval_on_selector_all(
        '#unfinished-shows img', 'els => els.length') == 1


def test_dash_valid_poster_size_is_preserved(dash):
    """w200 for the grid, not a size change."""
    _render_unfinished(dash, [_show('/abc.jpg')])
    assert '/t/p/w200/' in _poster_srcs(dash)[0]


# ── F37: the "Watch Next" href is a URL context, not a text one ─────────────
#
# Found by mutation testing: reverting `watch_url` to a raw `s.watch_url`
# interpolation changed no assertion, because no test rendered a row that HAS a
# watch_url. `_show()` defaulted it to None, so the whole branch was untested.

def test_dash_watch_link_renders_for_a_valid_target(dash):
    _render_unfinished(dash, [_show('/abc.jpg', watch_url='/watch/tv/77/1/2')])
    assert dash.eval_on_selector_all(
        '#unfinished-shows a[href="/watch/tv/77/1/2"]', 'els => els.length') == 1


@pytest.mark.parametrize('watch_url', [
    'javascript:window.__XSS_FIRED=(window.__XSS_FIRED||0)+1',
    '//evil.example/x',
    'https://evil.example/x',
])
def test_dash_watch_link_refuses_unsafe_targets(dash, watch_url):
    """An href is executable; it must pass the URL policy, not just escaping."""
    _render_unfinished(dash, [_show('/abc.jpg', watch_url=watch_url)])
    hrefs = dash.eval_on_selector_all(
        '#unfinished-shows a', 'els => els.map(e => e.getAttribute("href"))')
    for href in hrefs:
        assert not (href or '').startswith('javascript:'), href
        assert not (href or '').startswith('//'), href
        assert 'evil.example' not in (href or ''), href
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'


def test_dash_watch_link_cannot_break_out_of_the_href(dash):
    _render_unfinished(dash, [
        _show('/abc.jpg', watch_url='x" onclick="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1')])
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'
    assert dash.eval_on_selector_all(
        '#unfinished-shows a[onclick]', 'els => els.length') == 0, \
        'the payload produced a live onclick handler'


def test_dash_missing_watch_url_renders_no_link(dash):
    _render_unfinished(dash, [_show('/abc.jpg', watch_url=None)])
    assert dash.eval_on_selector_all(
        '#unfinished-shows a[href*="watch"]', 'els => els.length') == 0


def _render_calendar(page, episodes):
    page.evaluate(
        """(rows) => {
            window.fetch = () => Promise.resolve({
                ok: true,
                json: () => Promise.resolve({episodes: rows}),
            });
        }""", episodes)
    page.evaluate('loadCalendarView()')
    page.wait_for_timeout(120)


def _episode(still_path, **kw):
    row = {'show_id': 5, 'show_name': 'Some Show', 'episode_name': 'Pilot',
           'season_number': 1, 'episode_number': 1, 'air_date': '2026-01-01',
           'days_until_air': 2, 'watched': False, 'episode_overview': None,
           'still_path': still_path, 'poster_path': None}
    row.update(kw)
    return row


# ── F37: the calendar still src ─────────────────────────────────────────────
#
# Mutation testing found this path had NO coverage at all: reverting
# `<img src="${escapeAttr(stillUrl)}">` to the raw interpolation changed nothing.
# That sink is the one F35's linter missed and that was genuinely unescaped, so
# an untested regression there would have been invisible.

def test_cal_relative_still_uses_the_fixed_origin(dash):
    _render_calendar(dash, [_episode('/still123.jpg')])
    srcs = dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.map(e => e.getAttribute("src"))')
    assert srcs == ['https://image.tmdb.org/t/p/w300/still123.jpg'], srcs


def test_cal_off_host_still_is_refused_and_never_requested(dash):
    _render_calendar(dash, [_episode('https://attacker.example/pixel.png')])
    srcs = dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.map(e => e.getAttribute("src"))')
    assert srcs == ['/static/images/no-poster.png'], srcs
    assert not any('attacker.example' in u for u in dash._attempted), \
        dash._attempted


def test_cal_still_cannot_break_out_of_the_src_attribute(dash):
    """This is the F35 defect: stillUrl was interpolated with no escaping."""
    _render_calendar(dash, [
        _episode('x" onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1')])
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'
    assert dash.eval_on_selector_all(
        '#calendar-view img[onerror^="window.__XSS_FIRED"]',
        'els => els.length') == 0, 'the payload produced a live onerror handler'
    srcs = dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.map(e => e.getAttribute("src"))')
    assert all('onerror' not in (s or '') for s in srcs), srcs


@pytest.mark.parametrize('value', ['//evil.example/p.png', '/../x.jpg',
                                   'javascript:alert(1)', '',
                                   '/x?y.jpg', '/x#y.jpg', '/a\\b.jpg',
                                   '/a b.jpg'])
def test_cal_malformed_still_falls_back(dash, value):
    _render_calendar(dash, [_episode(value)])
    srcs = dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.map(e => e.getAttribute("src"))')
    assert srcs == ['/static/images/no-poster.png'], value
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'


def test_cal_empty_state_is_preserved(dash):
    _render_calendar(dash, [])
    assert dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.length') == 0


def test_cal_uses_poster_when_still_is_absent(dash):
    _render_calendar(dash, [_episode(None, poster_path='/p9.jpg')])
    srcs = dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.map(e => e.getAttribute("src"))')
    assert srcs == ['https://image.tmdb.org/t/p/w300/p9.jpg'], srcs


# ── F37: pin the "root-relative only" contract that safeInternalUrl enforces ──
#
# Mutation testing showed the leading-slash check is not independently
# covered: the leading-scheme guard happens to reject the same payloads, so
# removing it alone changes nothing. These two cases fail only because the
# value does not start with "/".

@pytest.mark.parametrize('watch_url', ['evil.example/x', 'watch/tv/1/1/1'])
def test_dash_watch_link_requires_a_root_relative_path(dash, watch_url):
    _render_unfinished(dash, [_show('/abc.jpg', watch_url=watch_url)])
    assert dash.eval_on_selector_all(
        '#unfinished-shows a[href*="watch"]', 'els => els.length') == 0, \
        'a non-root-relative target produced a link'


# ── F37: prove which layer actually controls the calendar still src ──────────
#
# Mutation M5 (replacing `${escapeAttr(stillUrl)}` with `${stillUrl}`) does NOT
# fail any test, and this is the reason: `tmdbImageUrl()` already returns a
# value that cannot contain a quote — a hostile path either fails validation or
# comes back percent-encoded from the URL parser. The escape is therefore
# defence-in-depth, not the controlling control, and removing it is genuinely
# unobservable. Rather than contrive a test that only passes by accident, this
# asserts the property that makes the redundancy safe.
_HOSTILE_POSTERS = [
    'x" onerror="alert(1)', "x' onerror='alert(1)", '<script>alert(1)</script>',
    'https://image.tmdb.org/a".jpg', 'https://image.tmdb.org/a".jpg'.replace(
        'image.tmdb.org', 'image.tmdb.org\\'),
    '//evil.example/"x', '/a"b.jpg', '/a<b>c.jpg', "javascript:'x'", '',
    None, 42, {}, '/ok.jpg',
]


def test_cal_tmdb_image_url_output_is_always_attribute_safe(dash):
    """tmdbImageUrl() must never emit a character that could break out of src."""
    offenders = dash.evaluate(
        """(values) => values.map(v => {
            const out = DOM.tmdbImageUrl(v, 'w300', '/static/images/no-poster.png');
            return {value: String(v), out: String(out)};
        })""", _HOSTILE_POSTERS)
    for row in offenders:
        out = row['out']
        # The attribute is double-quoted, so '"' is the breakout character;
        # '<', '>' and '`' would let the value become markup; whitespace and
        # DEL are URL-smuggling vectors. A bare apostrophe is not in this set:
        # inside a double-quoted attribute it is inert, and escapeAttr encodes
        # it anyway.
        for bad in ('"', '<', '>', '`', ' ', '\t', '\n', '\r', '\x7f'):
            assert bad not in out, (
                'tmdbImageUrl(%r) emitted %r, which could break out of an '
                'attribute: %r' % (row['value'], bad, out))
        assert not out.strip() != out, 'leading/trailing whitespace: %r' % out


# ── F37: cover the traversal guard in safeInternalUrl (mutation M9) ──────────

@pytest.mark.parametrize('watch_url', ['/watch/../admin', '/../evil', '/a/../../b'])
def test_dash_watch_link_refuses_traversal(dash, watch_url):
    _render_unfinished(dash, [_show('/abc.jpg', watch_url=watch_url)])
    assert dash.eval_on_selector_all(
        '#unfinished-shows a[href*="watch"], #unfinished-shows a[href*="admin"]',
        'els => els.length') == 0, \
        'a traversing target produced a link: %r' % watch_url


# ── F37: same property for the link validator ───────────────────────────────
#
# Mutations M5/M14/M16 (dropping escapeAttr from the poster/still srcs) and M15
# (dropping it from the watch href) all survive, and for the same reason: the
# upstream validator already guarantees an attribute-safe value, so the escape is
# defence-in-depth whose removal is unobservable. These two property tests are
# what make that redundancy explicit and checkable — if a future change to
# either validator lets a breakout character through, these fail immediately.
_HOSTILE_LINKS = [
    'x" onclick="alert(1)', "x' onclick='alert(1)", 'javascript:alert(1)',
    '//evil.example/x', 'https://evil.example/x', 'http://image.tmdb.org/x',
    '/watch/../admin', '/a b', '/a\tb', '/a\nb', 'relative/path', '', None,
    42, {}, '/watch/tv/1/1/1',
]


def test_watch_link_policy_output_is_always_attribute_safe(dash):
    offenders = dash.evaluate(
        """(values) => values.map(v => {
            const out = DOM.safeInternalUrl(v);
            return {value: String(v), out: String(out)};
        })""", _HOSTILE_LINKS)
    for row in offenders:
        out = row['out']
        for bad in ('"', "'", '<', '>', '`', ' ', '\t', '\n', '\r'):
            assert bad not in out, (
                'safeInternalUrl(%r) emitted %r: %r' % (row['value'], bad, out))
        assert out == '' or out.startswith('/'), (
            'safeInternalUrl(%r) produced a non root-relative target: %r'
            % (row['value'], out))
        assert not out.startswith('//'), 'protocol-relative: %r' % out


# ── F38 gap closed by mutation testing ───────────────────────────────────────
#
# Reverting `${DOM.escapeText(ep.show_name)}` in BOTH dashboard render paths
# left the browser suite fully green: no existing test rendered a hostile show
# name, episode name or overview there. The static rule caught the revert, but
# the render paths themselves were unexercised.

_TEXT_PAYLOAD = '<img src=x onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1"> & "q"'


def _render_week(page, episodes):
    page.evaluate(
        """(rows) => {
            window.fetch = () => Promise.resolve({
                ok: true, json: () => Promise.resolve({episodes: rows}),
            });
        }""", episodes)
    page.evaluate('loadUpcomingWeek()')
    page.wait_for_timeout(150)


def _calendar_rows(**kw):
    return _episode(**kw)


def _payload_handlers(page, container):
    """Elements carrying a handler that came from the payload.

    Both render paths legitimately emit `onerror="this.src=..."` on the poster
    <img>, so counting handlers would be wrong. Matching only the sentinel
    distinguishes "the template's own handler" from "a handler the attacker
    smuggled into a text field".
    """
    return page.eval_on_selector_all(
        '%s [onerror*="__XSS_FIRED"], %s [onclick*="__XSS_FIRED"], '
        '%s [onload*="__XSS_FIRED"]' % (container, container, container),
        'els => els.length')


def test_cal_show_name_episode_name_and_overview_render_as_text(dash):
    _render_calendar(dash, [_calendar_rows(
        still_path=None, show_name=_TEXT_PAYLOAD, episode_name=_TEXT_PAYLOAD,
        episode_overview=_TEXT_PAYLOAD)])
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'
    assert _payload_handlers(dash, '#calendar-view') == 0, (
        'a text field produced a live handler')
    # The text must still READ as the text that was rendered, not be dropped.
    assert 'onerror' in dash.inner_text('#calendar-view')


def test_cal_show_name_cannot_add_an_image_element(dash):
    _render_calendar(dash, [_calendar_rows(
        still_path=None, show_name=_TEXT_PAYLOAD)])
    # The calendar always renders one poster <img> (the no-poster placeholder).
    # Anything beyond that came from the payload.
    assert dash.eval_on_selector_all(
        '#calendar-view img', 'els => els.length') == 1


def test_week_show_name_episode_name_render_as_text(dash):
    _render_week(dash, [_calendar_rows(
        still_path=None, show_name=_TEXT_PAYLOAD, episode_name=_TEXT_PAYLOAD)])
    assert _fired(dash) == 0, 'SCRIPT EXECUTED'
    assert _payload_handlers(dash, '#upcoming-week') == 0
    assert 'onerror' in dash.inner_text('#upcoming-week')


def test_week_off_host_still_is_refused_and_never_requested(dash):
    _render_week(dash, [_calendar_rows(still_path=OFF_HOST)])
    assert 'attacker.example' not in _emitted_html(dash)
    assert not any('attacker.example' in u for u in dash._attempted)


# ── F38: the templates and JS files that carried the remaining known-open sinks ──
#
# These drive the REAL render functions out of the shipped templates (the script
# is extracted and executed, not re-implemented), so a regression that puts a
# raw concatenation or an unescaped interpolation back into the markup fails
# here rather than passing on a source-shape check.
#
# Every request is intercepted with page.route() and answered locally, and the
# `innerHTML` setter is hooked to record what the template ASSIGNED — reading
# `innerHTML` back would let an `onerror` rewrite hide the real value.

F38_DOM = """
<div id="episodes-list"></div>
<div id="episodes-container"></div>
<div id="credits-content"></div>
<div id="calendar-view"></div>
<div id="unfinished-shows"></div>
<div id="recently-completed"></div>
<div id="continue-watching"></div>
<div id="upcoming-week"></div>
<div id="quick-stats"></div>
<div id="empty-state" class="hidden"></div>
<div id="lists-grid"></div>
"""

OFF_HOST = 'https://attacker.example/pixel.png'


def _template_script(name):
    from template_script_syntax import javascript_blocks, strip_jinja
    source = strip_jinja(
        (ROOT / 'templates' / name).read_text(encoding='utf-8'))
    return '\n'.join(javascript_blocks(source))


@pytest.fixture
def trapped(browser):
    """A page with request interception, the innerHTML recorder, and helpers."""
    page = browser.new_page()
    attempted = []

    def _route(route):
        attempted.append(route.request.url)
        is_image = route.request.resource_type == 'image'
        route.fulfill(status=200,
                      content_type='image/png' if is_image else 'text/html',
                      body=PIXEL if is_image else b'')

    page.route('**/*', _route)
    page.set_content(F38_DOM)
    page.evaluate(RECORD_INNER_HTML)
    page.add_script_tag(content=_read('dom-safety.js'))
    page.evaluate('window.__XSS_FIRED = 0;')
    page._attempted = attempted
    return page


def _load_template(page, name):
    page.add_script_tag(content=_template_script(name))
    return page


def _emitted(page):
    return page.evaluate('() => (window.__rawHtml || []).join("")')


def _srcs(page, selector):
    return page.eval_on_selector_all(
        selector + ' img', 'els => els.map(e => e.getAttribute("src"))')


def _f38_episode(**kw):
    row = {
        'episode_number': 3, 'name': 'Pilot', 'overview': 'A synopsis.',
        'still_path': '/still1.jpg', 'poster_path': None, 'air_date': '2026-01-01',
        'runtime': 42, 'vote_average': 7.5, 'watched': False,
        'days_until_air': 1, 'episode_overview': 'Episode overview.',
        'episode_name': 'Pilot', 'show_name': 'Some Show', 'show_id': 9,
        'season_number': 1, 'watch_url': None, 'progress_percent': 10,
        'status': 'watching', 'last_episode': None, 'next_episode': None,
    }
    row.update(kw)
    return row


# ── tv_season_detail.html — the reported sibling off-host poster issue ──────

@pytest.fixture
def season(trapped):
    return _load_template(trapped, 'tv_season_detail.html')


def _render_season(page, episodes):
    page.evaluate(
        """(eps) => {
            seasonData = {episodes: eps};
            watchedEpisodes = new Set();
            renderEpisodes();
        }""", episodes)


def test_season_relative_still_uses_the_fixed_tmdb_origin(season):
    _render_season(season, [_f38_episode(still_path='/abc.jpg')])
    srcs = _srcs(season, '#episodes-list')
    assert srcs == ['https://image.tmdb.org/t/p/w300/abc.jpg'], srcs


def test_season_valid_absolute_tmdb_still_is_kept(season):
    _render_season(season, [
        _f38_episode(still_path='https://image.tmdb.org/t/p/w500/x.jpg')])
    srcs = _srcs(season, '#episodes-list')
    assert srcs == ['https://image.tmdb.org/t/p/w500/x.jpg'], srcs


def test_season_off_host_still_is_refused_and_never_requested(season):
    """The reported issue: a stored absolute URL must stay on the TMDb origin."""
    _render_season(season, [
        _f38_episode(still_path=OFF_HOST),
        _f38_episode(still_path='https://image.tmdb.org.evil.example/x.jpg'),
        _f38_episode(still_path='https://user:pw@image.tmdb.org/x.jpg'),
        _f38_episode(still_path='//evil.example/p.png'),
    ])
    html = _emitted(season)
    for hostile in ('attacker.example', 'evil.example', 'user:pw@'):
        assert hostile not in html, 'emitted a rejected value %r' % hostile
    assert _srcs(season, '#episodes-list') == ['/static/images/no-image.png'] * 4
    for host in ('attacker.example', 'evil.example'):
        assert not any(host in u for u in season._attempted), (
            'a request left for %s: %r' % (host, season._attempted))


@pytest.mark.parametrize('value', [
    'javascript:alert(1)', '/../../etc/passwd', '/x?y.jpg', '', None,
])
def test_season_malformed_still_falls_back(season, value):
    _render_season(season, [_f38_episode(still_path=value)])
    assert _srcs(season, '#episodes-list') == ['/static/images/no-image.png']
    assert _fired(season) == 0, 'SCRIPT EXECUTED'


def test_season_still_cannot_break_out_of_the_src_attribute(season):
    _render_season(season, [
        _episode(
            still_path='x" onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1')])
    assert _fired(season) == 0, 'SCRIPT EXECUTED'
    assert season.eval_on_selector_all(
        '#episodes-list img[onerror^="window.__XSS_FIRED"]',
        'els => els.length') == 0, 'the payload produced a live onerror handler'


def test_season_overview_renders_as_text_not_markup(season):
    payload = '<img src=x onerror="window.__XSS_FIRED=1"> & "quoted"'
    _render_season(season, [_f38_episode(overview=payload)])
    assert _fired(season) == 0, 'SCRIPT EXECUTED'
    assert season.eval_on_selector_all(
        '#episodes-list p img', 'els => els.length') == 0
    assert 'onerror' in season.inner_text('#episodes-list')


def test_season_episode_name_renders_as_text(season):
    """The multi-line text interpolation the static rule cannot see."""
    payload = '<script>window.__XSS_FIRED=1</script>'
    _render_season(season, [_f38_episode(name=payload)])
    assert _fired(season) == 0, 'SCRIPT EXECUTED'
    assert '<' in season.inner_text('#episodes-list')


def test_season_empty_state_is_preserved(season):
    _render_season(season, [])
    assert season.eval_on_selector_all(
        '#episodes-list img', 'els => els.length') == 0


# ── tv_upcoming.html ─────────────────────────────────────────────────────────

@pytest.fixture
def upcoming(trapped):
    return _load_template(trapped, 'tv_upcoming.html')


def _render_upcoming(page, episodes):
    page.evaluate("(eps) => { allEpisodes = eps; renderEpisodes('all'); }",
                  episodes)


def test_upcoming_off_host_still_is_refused_and_never_requested(upcoming):
    _render_upcoming(upcoming, [
        _f38_episode(still_path=OFF_HOST), _f38_episode(still_path='/ok.jpg')])
    srcs = _srcs(upcoming, '#episodes-container')
    assert srcs[0] == '/static/images/no-poster.png', srcs
    assert srcs[1] == 'https://image.tmdb.org/t/p/w300/ok.jpg', srcs
    assert not any('attacker.example' in u for u in upcoming._attempted)


def test_upcoming_overview_and_name_render_as_text(upcoming):
    payload = '<img src=x onerror="window.__XSS_FIRED=1"> & "q"'
    _render_upcoming(upcoming, [
        _f38_episode(episode_overview=payload, episode_name=payload,
                     show_name=payload, still_path=None)])
    assert _fired(upcoming) == 0, 'SCRIPT EXECUTED'
    assert upcoming.eval_on_selector_all(
        '#episodes-container p img', 'els => els.length') == 0


def test_upcoming_still_cannot_break_out_of_the_src_attribute(upcoming):
    _render_upcoming(upcoming, [
        _f38_episode(still_path='x" onerror="window.__XSS_FIRED=1')])
    assert _fired(upcoming) == 0, 'SCRIPT EXECUTED'


# ── tv_episode_detail.html ───────────────────────────────────────────────────

@pytest.fixture
def episode_detail(trapped):
    return _load_template(trapped, 'tv_episode_detail.html')


def _render_credits(page, cast, crew=()):
    page.evaluate(
        """(data) => {
            episodeData = {credits: {guest_stars: data.cast, crew: data.crew}};
            renderCredits();
        }""", {'cast': cast, 'crew': list(crew)})


def _person(**kw):
    row = {'id': 3, 'name': 'Actor Name', 'character': 'Detective',
           'profile_path': '/face.jpg'}
    row.update(kw)
    return row


def test_episode_detail_profile_path_uses_the_fixed_origin(episode_detail):
    _render_credits(episode_detail, [_person()])
    assert _srcs(episode_detail, '#credits-content') == [
        'https://image.tmdb.org/t/p/w185/face.jpg']


def test_episode_detail_off_host_profile_is_refused(episode_detail):
    _render_credits(episode_detail, [_person(profile_path=OFF_HOST)])
    assert 'attacker.example' not in _emitted(episode_detail)
    assert _srcs(episode_detail, '#credits-content') == [
        '/static/images/no-avatar.png']
    assert not any('attacker.example' in u for u in episode_detail._attempted)


def test_episode_detail_name_and_character_render_as_text(episode_detail):
    payload = '<img src=x onerror="window.__XSS_FIRED=1"> & "q"'
    _render_credits(
        episode_detail, [_person(name=payload, character=payload)])
    assert _fired(episode_detail) == 0, 'SCRIPT EXECUTED'
    # exactly one <img>: the avatar. The payload must not have become one.
    assert episode_detail.eval_on_selector_all(
        '#credits-content img', 'els => els.length') == 1


# ── lists_discover.html — owner-supplied cover image ────────────────────────

@pytest.fixture
def lists(trapped):
    # F48: the discover card's cover is now drawn by the collage renderer, so
    # this harness must load it. Without it `createListCard` hits the
    # "renderer missing" guard and every assertion below would pass against a
    # degraded page rather than the real one.
    trapped.add_script_tag(content=_read('list-cover.js'))
    return _load_template(trapped, 'lists_discover.html')


def _card_html(page, cover):
    return page.evaluate(
        """(cover) => {
            const card = createListCard({
                id: 1, slug: 'mine', name: 'My List', cover_image: cover,
                user: {username: 'owner', profile_picture: null},
            });
            return card.outerHTML;
        }""", cover)


def test_lists_relative_cover_image_is_preserved(lists):
    html = _card_html(lists, '/static/images/x.jpg')
    assert 'src="/static/images/x.jpg"' in html, html


def test_lists_off_host_cover_image_is_refused_and_never_requested(lists):
    """cover_image is owner-supplied: arbitrary user input, not catalogue data."""
    html = _card_html(lists, OFF_HOST)
    assert 'attacker.example' not in html, html
    assert not any('attacker.example' in u for u in lists._attempted)


def test_lists_javascript_cover_image_is_refused(lists):
    html = _card_html(lists, 'javascript:window.__XSS_FIRED=1')
    assert 'javascript:' not in html, html
    assert _fired(lists) == 0, 'SCRIPT EXECUTED'


def test_lists_placeholder_is_used_when_cover_is_unusable(lists):
    html = _card_html(lists, OFF_HOST)
    assert 'fa-film' in html, 'the placeholder block should be used'


# ── chrome.js command palette ────────────────────────────────────────────────
#
# Driven the way a user reaches it: Ctrl+K opens the palette, typing fires the
# search, and `fetch` is stubbed with a TMDb-shaped autocomplete response.

def _palette(page, media):
    page.add_script_tag(content=_read('chrome.js'))
    # chrome.js registers its listeners inside a DOMContentLoaded handler, and
    # set_content() has already fired that event by the time the script is
    # added. Replay it so the palette is actually wired.
    page.evaluate("() => document.dispatchEvent(new Event('DOMContentLoaded'))")
    page.wait_for_timeout(50)
    page.evaluate(
        """(media) => {
            window.fetch = () => Promise.resolve({
                ok: true, json: () => Promise.resolve(media),
            });
        }""", media)
    page.evaluate("""() => {
        document.dispatchEvent(new KeyboardEvent('keydown',
            {key: 'k', ctrlKey: true, bubbles: true}));
    }""")
    page.wait_for_timeout(80)
    page.fill('.cmdk-input', 'ab')
    # chrome.js debounces the input handler by 180ms before searching, and the
    # search itself awaits fetch. Waiting less made these tests pass vacuously:
    # with no results rendered, "no attacker.example in the HTML" is trivially
    # true. The non-empty assertion below is what stops that recurring.
    page.wait_for_timeout(400)
    html = page.eval_on_selector('.cmdk-results', 'el => el.innerHTML')
    assert html.strip(), (
        'the palette rendered no results, so this test would pass vacuously')
    return html


def test_palette_off_host_poster_is_refused(trapped):
    """chrome.js built `https://image.tmdb.org/t/p/w92${path}` unconditionally."""
    html = _palette(trapped, {'movies': [{'id': 1, 'title': 'Alien',
                                         'poster_path': OFF_HOST}],
                              'shows': [], 'people': []})
    assert 'attacker.example' not in html, html
    assert not any('attacker.example' in u for u in trapped._attempted)


def test_palette_title_renders_as_text(trapped):
    html = _palette(trapped, {
        'movies': [{'id': 1,
                    'title': '<img src=x onerror="window.__XSS_FIRED=1">',
                    'poster_path': None}],
        'shows': [], 'people': []})
    assert _fired(trapped) == 0, 'SCRIPT EXECUTED'
    # Escaped, not stripped: the title must still READ as the text the user
    # typed, it just must not have become an element.
    assert '&lt;img src=x' in html, html
    assert trapped.eval_on_selector_all(
        '.cmdk-results img[src="x"]', 'els => els.length') == 0, (
        'the title became a live element')


# ── watch_tv.html ───────────────────────────────────────────────────────────
#
# Added after mutation testing showed the watch_tv still regression survived:
# the static rule caught it, but nothing executed this render path.

@pytest.fixture
def watch_tv(trapped):
    return _load_template(trapped, 'watch_tv.html')


def _render_watch_tv(page, episodes):
    """Drive watch_tv's renderEpisodes().

    The `catch` is deliberate and narrow. `const seasons = {{ show.seasons |
    tojson }}` becomes `null` once Jinja is stripped, and the "next episode"
    block at the end of renderEpisodes calls `.find()` on it — AFTER
    `list.innerHTML` has already been assigned. So the throw is post-render and
    does not affect what is under test. Each assertion below checks the rendered
    output rather than the absence of an exception, so a genuine render failure
    would still fail the test.
    """
    page.evaluate(
        """(eps) => {
            allEpisodes = {1: eps};
            try { renderEpisodes(1); } catch (err) { window.__wtvErr = String(err); }
        }""", episodes)


def test_watch_tv_relative_still_uses_the_fixed_origin(watch_tv):
    _render_watch_tv(watch_tv, [_f38_episode(still_path='/still.jpg')])
    assert _srcs(watch_tv, '#episodes-list') == [
        'https://image.tmdb.org/t/p/w185/still.jpg']


def test_watch_tv_off_host_still_is_refused_and_never_requested(watch_tv):
    _render_watch_tv(watch_tv, [_f38_episode(still_path=OFF_HOST)])
    assert 'attacker.example' not in _emitted(watch_tv)
    assert _srcs(watch_tv, '#episodes-list') == []
    assert not any('attacker.example' in u for u in watch_tv._attempted)


def test_watch_tv_still_cannot_break_out_of_the_src_attribute(watch_tv):
    _render_watch_tv(watch_tv, [
        _f38_episode(
            still_path='x" onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1')])
    assert _fired(watch_tv) == 0, 'SCRIPT EXECUTED'


def test_watch_tv_placeholder_is_used_when_still_is_absent(watch_tv):
    _render_watch_tv(watch_tv, [_f38_episode(still_path=None)])
    assert _srcs(watch_tv, '#episodes-list') == []
    assert 'fa-play' in watch_tv.inner_html('#episodes-list')


def test_watch_tv_empty_state_is_preserved(watch_tv):
    _render_watch_tv(watch_tv, [])
    assert 'No episodes found' in watch_tv.inner_html('#episodes-list')


# ── F41: a rejected cover reaches the placeholder and fetches nothing ─────────
#
# `createListCard` returns a DETACHED element, so `test_lists_off_host_...`
# proves only that the markup is clean: an unattached <img> never issues a
# request, which makes its "never requested" half vacuous. These attach the
# card to the document so a surviving `src` really does produce an intercepted
# fetch attempt — and every negative case is paired with a positive control
# showing the harness would notice one.
#
# The request trap still answers everything locally (PIXEL / empty html), so
# nothing is ever sent to a remote host, approved or not.

F41_REJECTED_COVERS = [
    '/x?y.jpg',                                        # query in a path
    '/x#frag',                                         # fragment in a path
    'https://image.tmdb.org./x.jpg',                   # trailing-dot host
    'https://image.tmdb.org:8443/x.jpg',               # non-default port
    '//evil.example/pixel.png',                        # protocol-relative
    'https://user:pw@image.tmdb.org/x.jpg',            # credentials
    'https://image.tmdb.org.evil.example/x.jpg',       # deceptive host
    OFF_HOST,                                          # unapproved host
    'http://image.tmdb.org/x.jpg',                     # not https
    'data:image/png;base64,AAAA',                      # data URL
    'javascript:window.__XSS_FIRED=1',                 # executable scheme
    '/../../etc/passwd',                               # traversal
    '/static\\evil.jpg',                               # backslash
    '/static/a b.jpg',                                 # whitespace
    '/static/a\x01b.jpg',                              # control character
    'x.jpg',                                           # bare relative
]

F41_APPROVED_HOST = 'image.tmdb.org'


def _attach_card(page, cover):
    """Build a card AND put it in the document, so an <img> really loads."""
    return page.evaluate(
        """(cover) => {
            const host = document.createElement('div');
            host.id = 'f41-host';
            document.body.appendChild(host);
            host.appendChild(createListCard({
                id: 1, slug: 'mine', name: 'My List', cover_image: cover,
                user: {username: 'owner', profile_picture: null},
            }));
            const img = host.querySelector('img');
            return {
                src: img ? img.getAttribute('src') : null,
                // F48: the placeholder is now the collage renderer's
                // whole-cover state. `fq-cover--empty` is unique to it --
                // `fq-cover-tile--empty` marks individual missing posters, and
                // `fa-film` is NOT a usable marker because the card body
                // renders an `fa-film` icon for the item count too.
                placeholder: !!host.querySelector('.fq-cover--empty'),
                // The card must not have fallen back to loading a real cover.
                collageTiles: host.querySelectorAll('.fq-cover-tile').length,
                html: host.innerHTML,
            };
        }""", cover)


@pytest.mark.parametrize('cover', F41_REJECTED_COVERS,
                         ids=[repr(c)[:32] for c in F41_REJECTED_COVERS])
def test_rejected_cover_renders_the_placeholder_and_fetches_nothing(
        lists, cover):
    """A refused cover produces no <img> at all, so no request can originate."""
    before = len(lists._attempted)
    result = _attach_card(lists, cover)
    lists.wait_for_timeout(200)
    fresh = lists._attempted[before:]

    assert result['src'] is None, (
        'a rejected cover produced an <img src>: %r' % cover)
    assert result['placeholder'], (
        'the placeholder block should be used for %r' % cover)
    assert not fresh, 'a rejected cover triggered a request: %r' % (fresh,)
    assert cover not in result['html']
    assert _fired(lists) == 0, 'SCRIPT EXECUTED'


def test_the_harness_would_notice_a_real_cover_request(lists):
    """Control: an approved ABSOLUTE cover does produce an intercepted fetch.

    Without this, `assert not fresh` above could pass because the harness
    records nothing at all rather than because the cover was refused.
    """
    before = len(lists._attempted)
    result = _attach_card(lists,
                          'https://image.tmdb.org/t/p/w500/abc.jpg')
    lists.wait_for_timeout(400)
    fresh = lists._attempted[before:]

    assert result['src'] == 'https://image.tmdb.org/t/p/w500/abc.jpg'
    assert [u for u in fresh if F41_APPROVED_HOST in u], (
        'no request recorded for an approved cover; the F41 negative '
        'assertion above would be vacuous')


#: (input cover, rendered src). `:443` is accepted but `new URL().href`
#: canonicalises the default port away — the same URL, so both layers accept
#: it; only the stored spelling and the rendered spelling differ.
F41_SUPPORTED_COVERS = [
    ('/static/images/x.jpg', '/static/images/x.jpg'),
    ('/lists/1', '/lists/1'),
    ('https://image.tmdb.org/t/p/w500/abc.jpg',
     'https://image.tmdb.org/t/p/w500/abc.jpg'),
    ('https://image.tmdb.org:443/t/p/w500/abc.jpg',
     'https://image.tmdb.org/t/p/w500/abc.jpg'),
]


@pytest.mark.parametrize('cover,expected', F41_SUPPORTED_COVERS,
                         ids=['rel', 'bare-rel', 'tmdb', 'tmdb-443'])
def test_supported_covers_are_preserved_after_the_f41_alignment(
        lists, cover, expected):
    """Tightening must not cost the ordinary TMDb poster paths."""
    before = len(lists._attempted)
    result = _attach_card(lists, cover)
    lists.wait_for_timeout(200)

    assert result['src'] == expected, (
        'a supported cover was refused: %r -> %r' % (cover, result['src']))
    assert not result['placeholder']
    assert not [u for u in lists._attempted[before:]
                if u.split('//')[-1].split('/')[0]
                not in (F41_APPROVED_HOST,)]
