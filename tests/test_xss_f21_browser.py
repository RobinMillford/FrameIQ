"""F21 — behavioural XSS regression tests in a real headless Chromium.

The source-level assertions in ``test_xss_f21.py`` cannot answer the only
question that matters: *does this payload actually execute?* These tests do.

Conventions inherited from ``test_explore_browser.py`` / the F14 browser suite:

* ``page.set_content`` — no Flask server, no database, no network.
* The REAL shipped files are executed (``dom-safety.js``, ``tag-manager.js``,
  ``list-advanced.js``, and the inline script extracted from the real
  ``user_profile.html``), so a rename or a re-introduction is caught.
* Payloads are harmless local sentinels. Nothing reads a cookie, touches a real
  account, or leaves the page.

THE SENTINEL
-------------
Every payload sets ``window.__XSS_FIRED = (window.__XSS_FIRED || 0) + 1``.
If any injected script runs, the counter increments and the test fails. The
counter is asserted alongside the DOM assertions, because "no element was
created" and "no script executed" are different claims and both must hold.

Calling convention
------------------
Data is passed with ``page.evaluate("(a) => fn(a)", payload)`` — an arrow
function expression, which is what Playwright's ``evaluate`` accepts when the
first argument is a string. Multi-statement bodies are installed via
``add_script_tag`` instead, never via ``%``-formatted strings (a mistake that
silently leaves a literal ``%s`` in the page).
"""
import pathlib
import sys

import pytest

playwright_api = pytest.importorskip(
    'playwright.sync_api', reason='playwright not installed')

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))
from template_script_syntax import (  # noqa: E402
    javascript_blocks, strip_jinja,
)

JS = ROOT / 'static' / 'js'

# Harmless sentinel: breaks out of a quoted attribute / inline handler /
# JS string, and records execution if it ever does.
PAYLOAD = "x'); window.__XSS_FIRED = (window.__XSS_FIRED||0)+1; //"
IMG_PAYLOAD = 'x" onerror="window.__XSS_FIRED = (window.__XSS_FIRED||0)+1'
EXEC_PAYLOAD = '<img src=x onerror="window.__XSS_FIRED=(window.__XSS_FIRED||0)+1">'
AVATAR_PAYLOAD = 'javascript:window.__XSS_FIRED=(window.__XSS_FIRED||0)+1'

SENTINEL = 'window.__XSS_FIRED || 0'


def _read(name):
    return (JS / name).read_text(encoding='utf-8')


def _inline_script(template_name, marker=None):
    """Extract a real inline <script> block from a shipped template."""
    source = strip_jinja(
        (ROOT / 'templates' / template_name).read_text(encoding='utf-8'))
    blocks = [b for b in javascript_blocks(source) if b.strip()]
    if marker:
        blocks = [b for b in blocks if marker in b] or blocks
    assert blocks, 'no executable inline script found in %s' % template_name
    return '\n;\n'.join(blocks)


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


# ---------------------------------------------------------------------------
# 1. Activity feed — review/comment text must stay text
# ---------------------------------------------------------------------------


@pytest.fixture
def feed_page(browser):
    page = _new(browser, '<div id="activity-stream"></div>')
    page.add_script_tag(content=_inline_script('feed_enhanced.html',
                                               'createActivityCard'))
    return page


# createActivityCard returns a node; the harness must append it to the page.
RENDER_CARD = """
(a) => document.getElementById('activity-stream')
        .appendChild(createActivityCard(a))
"""

REVIEW = {
    'type': 'review', 'username': PAYLOAD, 'user_avatar': None, 'rating': 8,
    'content': EXEC_PAYLOAD, 'media_type': 'movie', 'media_id': 550,
    'media_title': 'Fight Club', 'timestamp': '2026-01-01T00:00:00Z',
}


def test_review_content_renders_as_text(feed_page):
    feed_page.evaluate(RENDER_CARD, REVIEW)
    body = feed_page.inner_text('#activity-stream')
    assert 'onerror' in body, 'the payload should be visible as literal text'
    assert _fired(feed_page) == 0, 'SCRIPT EXECUTED'
    assert feed_page.eval_on_selector_all(
        '#activity-stream img[src="x"]', 'els => els.length') == 0


def test_comment_content_renders_as_text(feed_page):
    payload = dict(REVIEW, type='comment', rating=None,
                   content='<script>window.__XSS_FIRED=(window.__XSS_FIRED||0)+1</script>')
    feed_page.evaluate(RENDER_CARD, payload)
    assert 'window.__XSS_FIRED' in feed_page.inner_text('#activity-stream')
    assert _fired(feed_page) == 0, 'SCRIPT EXECUTED'


def test_tag_name_renders_as_text(feed_page):
    payload = dict(REVIEW, type='tag', rating=None, tag_name='<b>not bold</b>')
    feed_page.evaluate(RENDER_CARD, payload)
    assert feed_page.eval_on_selector_all(
        '#activity-stream strong b', 'els => els.length') == 0, \
        'tag name produced a real <b> element'
    assert '<b>not bold</b>' in feed_page.inner_text('#activity-stream')
    assert _fired(feed_page) == 0


def test_username_renders_as_text_and_does_not_break_the_alt(feed_page):
    payload = dict(REVIEW, type='like', rating=None, username=IMG_PAYLOAD)
    feed_page.evaluate(RENDER_CARD, payload)
    assert _fired(feed_page) == 0, 'SCRIPT EXECUTED'
    avatar = feed_page.get_attribute('#activity-stream img.user-avatar', 'alt')
    assert avatar is not None


def test_avatar_url_scheme_is_validated(feed_page):
    payload = dict(REVIEW, type='like', rating=None, user_avatar=AVATAR_PAYLOAD)
    feed_page.evaluate(RENDER_CARD, payload)
    src = feed_page.get_attribute('#activity-stream img.user-avatar', 'src')
    assert not src.startswith('javascript:'), 'javascript: avatar survived: %r' % src
    assert _fired(feed_page) == 0


def test_media_link_is_built_from_validated_parts(feed_page):
    payload = dict(REVIEW, type='like', rating=None, media_id='1/../../evil')
    feed_page.evaluate(RENDER_CARD, payload)
    hrefs = feed_page.eval_on_selector_all(
        '#activity-stream a', 'els => els.map(e => e.getAttribute("href"))')
    for href in hrefs:
        assert not href or 'javascript:' not in href


def test_navigation_still_works_for_legitimate_media(feed_page):
    payload = dict(REVIEW, content='Great film', username='someone')
    feed_page.evaluate(RENDER_CARD, payload)
    assert feed_page.get_attribute(
        '#activity-stream .activity-media a', 'href') == '/movie/550'
    assert 'Great film' in feed_page.inner_text('#activity-stream')
    assert '★' * 8 in feed_page.inner_text('#activity-stream')


# ---------------------------------------------------------------------------
# 2. Tag manager — suggestion buttons must not build inline handlers
# ---------------------------------------------------------------------------

TAG_MARKUP = """
<body data-media-id="1" data-media-type="movie" data-user-id="1">
  <div class="tag-input-container">
    <input id="tag-input">
    <div id="tag-suggestions" class="hidden"></div>
  </div>
  <div id="tags-display"></div>
  <div id="tag-error" class="hidden"></div>
  <div id="tag-success" class="hidden"></div>
</body>
"""

TAG_WIRING = """
window.__tm = new TagManager(1, 'movie');
window.__added = null; window.__removed = null;
window.__tm.tags = [];
window.__tm.renderTags();
window.__tm.searchTags = async () => [{id: 9, name: TAG_PAYLOAD, usage_count: 3}];
window.__tm.addTag = (n) => { window.__added = n; };
window.__tm.removeTag = (id) => { window.__removed = id; };
"""


@pytest.fixture
def tag_page(browser):
    page = _new(browser, TAG_MARKUP, scripts=('tag-manager.js',))
    page.evaluate('(p) => { window.TAG_PAYLOAD = p; }', PAYLOAD)
    page.evaluate(TAG_WIRING)
    return page


def test_tag_suggestion_shows_payload_as_text(tag_page):
    tag_page.evaluate("(q) => window.__tm.showSuggestions(q)", 'x')
    tag_page.wait_for_timeout(200)
    assert PAYLOAD in tag_page.inner_text('#tag-suggestions')
    assert _fired(tag_page) == 0, 'SCRIPT EXECUTED'


def test_tag_suggestion_button_has_no_inline_handler(tag_page):
    tag_page.evaluate("(q) => window.__tm.showSuggestions(q)", 'x')
    tag_page.wait_for_timeout(200)
    assert tag_page.eval_on_selector_all(
        '#tag-suggestions button[onclick]', 'els => els.length') == 0


def test_tag_suggestion_click_still_adds_the_tag(tag_page):
    """Behavioural proof the button still works without the inline handler."""
    tag_page.evaluate("(q) => window.__tm.showSuggestions(q)", 'x')
    tag_page.wait_for_timeout(200)
    tag_page.click('#tag-suggestions button')
    assert tag_page.evaluate('window.__added') == PAYLOAD, \
        'the click handler did not pass the real tag name'


def test_rendered_tag_pill_remove_button_calls_remove_tag(tag_page):
    tag_page.evaluate("(t) => { window.__tm.tags = t; window.__tm.renderTags(); }",
                      [{'id': 42, 'name': 'cult classic'}])
    assert 'cult classic' in tag_page.inner_text('#tags-display')
    assert tag_page.eval_on_selector_all(
        '#tags-display button[onclick]', 'els => els.length') == 0
    tag_page.click('#tags-display button')
    assert tag_page.evaluate('window.__removed') == 42


def test_tag_pill_with_hostile_name_is_text(tag_page):
    tag_page.evaluate("(t) => { window.__tm.tags = t; window.__tm.renderTags(); }",
                      [{'id': 7, 'name': PAYLOAD}])
    assert tag_page.eval_on_selector_all(
        '#tags-display script', 'els => els.length') == 0
    assert _fired(tag_page) == 0


def test_tag_pill_still_renders_its_svg_icon(tag_page):
    """The icon was previously inlined via innerHTML; DOM-built now."""
    tag_page.evaluate("(t) => { window.__tm.tags = t; window.__tm.renderTags(); }",
                      [{'id': 7, 'name': 'noir'}])
    assert tag_page.eval_on_selector_all(
        '#tags-display button svg path', 'els => els.length') == 1


# ---------------------------------------------------------------------------
# 3. Collaborators — the Critical stored-XSS sink
# ---------------------------------------------------------------------------

COLLAB_MARKUP = """
<div id="collaborators-list"></div>
<div id="list-categories"></div>
<div id="category-grid"></div>
<div id="analytics-panel"></div>
<div id="category-modal"></div>
<div data-list-id="1"></div>
"""

COLLAB_WIRING = """
window.__removed = null; window.__roleChanged = null;
window.__lm = new ListAdvancedManager(1);
window.__lm.removeCollaborator = (id, n) => { window.__removed = [id, n]; };
window.__lm.changeCollaboratorRole = (id) => { window.__roleChanged = id; };
window.__lm.loadCategories = async () => {};
window.__lm.trackView = async () => {};
window.__lm.loadAnalytics = async () => {};
"""

ADD_CARD = """
(c) => { document.getElementById('collaborators-list')
           .appendChild(window.__lm.createCollaboratorCard(c)); }
"""


@pytest.fixture
def collab_page(browser):
    page = _new(browser, COLLAB_MARKUP, scripts=('list-advanced.js',))
    page.evaluate(COLLAB_WIRING)
    return page


def test_collaborator_username_cannot_create_a_handler(collab_page):
    collab_page.evaluate(ADD_CARD, {
        'role': 'editor',
        'user': {'id': 5, 'username': PAYLOAD, 'profile_picture': None},
    })
    assert _fired(collab_page) == 0, 'SCRIPT EXECUTED'
    assert collab_page.eval_on_selector_all(
        '#collaborators-list [onclick]', 'els => els.length') == 0
    assert PAYLOAD in collab_page.inner_text('#collaborators-list')


def test_collaborator_remove_button_still_works(collab_page):
    collab_page.on('dialog', lambda dialog: dialog.accept())
    collab_page.evaluate(ADD_CARD, {
        'role': 'viewer',
        'user': {'id': 77, 'username': 'ada', 'profile_picture': None},
    })
    btn = collab_page.query_selector('#collaborators-list button[aria-label^="Remove"]')
    assert btn is not None, 'the remove button must still be rendered'
    btn.click()
    assert collab_page.evaluate('window.__removed') == [77, 'ada']


def test_collaborator_role_button_still_works(collab_page):
    collab_page.on('dialog', lambda dialog: dialog.accept('editor'))
    collab_page.evaluate(ADD_CARD, {
        'role': 'viewer',
        'user': {'id': 88, 'username': 'grace', 'profile_picture': None},
    })
    btn = collab_page.query_selector('#collaborators-list button[aria-label^="Change role"]')
    assert btn is not None
    btn.click()
    assert collab_page.evaluate('window.__roleChanged') == 88


def test_collaborator_avatar_url_is_validated(collab_page):
    collab_page.evaluate(ADD_CARD, {
        'role': 'viewer',
        'user': {'id': 9, 'username': 'x', 'profile_picture': AVATAR_PAYLOAD},
    })
    src = collab_page.get_attribute('#collaborators-list img', 'src')
    assert not src.startswith('javascript:')
    assert _fired(collab_page) == 0


def test_show_message_toast_treats_username_as_text(collab_page):
    collab_page.evaluate("(m) => window.__lm.showMessage(m, 'success')",
                         'Removed ' + PAYLOAD)
    toast = collab_page.query_selector('[role="status"], [role="alert"]')
    assert toast is not None, 'the toast must still render'
    assert PAYLOAD in toast.inner_text()
    assert _fired(collab_page) == 0


def test_category_colour_cannot_inject_css(collab_page):
    collab_page.evaluate("""
      (c) => { document.getElementById('list-categories')
                 .appendChild(window.__lm.createCategoryBadge(c, false)); }
    """, {'id': 3, 'name': 'classics',
          'color': 'red;background:url(javascript:alert(1))'})
    style = collab_page.get_attribute('#list-categories span', 'style') or ''
    assert 'url(' not in style, 'CSS injection survived: %r' % style
    assert 'classics' in collab_page.inner_text('#list-categories')


def test_category_badge_remove_button_still_works(collab_page):
    collab_page.evaluate("""
      window.__lm.removeCategory = (id, n) => { window.__removed = [id, n]; };
      (c) => { document.getElementById('list-categories')
                 .appendChild(window.__lm.createCategoryBadge(c, true)); }
    """, {'id': 12, 'name': 'classics', 'color': '#6366f1'})
    collab_page.click('#list-categories button')
    assert collab_page.evaluate('window.__removed') == [12, 'classics']


def test_analytics_viewer_username_is_text(collab_page):
    collab_page.evaluate("""
      (u) => window.__lm.displayAnalytics({
        analytics: {view_count: 3, unique_viewers: 2, share_count: 1, fork_count: 0},
        recent_viewers: [{username: u, viewed_at: '2026-01-01T00:00:00Z'}]
      })
    """, PAYLOAD)
    assert _fired(collab_page) == 0
    assert PAYLOAD in collab_page.inner_text('#analytics-panel')
    assert 'Total Views' in collab_page.inner_text('#analytics-panel')
    assert '3' in collab_page.inner_text('#analytics-panel')


# ---------------------------------------------------------------------------
# 4. where-to-watch — a javascript: provider link must not become a link
# ---------------------------------------------------------------------------


def test_javascript_provider_link_is_rejected(browser):
    page = _new(browser, '<div id="where-to-watch"><div id="wtw-body"></div></div>',
                scripts=('where-to-watch.js',))
    page.evaluate("""
      (bad) => { document.getElementById('wtw-body').innerHTML =
        '<a id="evil" href="' + window.FrameIQDOM.safeLinkUrl(bad) + '">x</a>'; }
    """, AVATAR_PAYLOAD)
    href = page.get_attribute('#evil', 'href')
    assert not href or not href.lower().startswith('javascript:'), \
        'javascript: href survived: %r' % href
    assert _fired(page) == 0


def test_https_provider_link_survives(browser):
    page = _new(browser, '<div id="where-to-watch"><div id="wtw-body"></div></div>',
                scripts=('where-to-watch.js',))
    page.evaluate("""
      document.getElementById('wtw-body').innerHTML =
        '<a id="ok" href="' +
        window.FrameIQDOM.safeLinkUrl('https://www.netflix.com/browse') + '">x</a>';
    """)
    assert page.get_attribute('#ok', 'href').startswith('https://')


def test_where_to_watch_has_no_inline_onerror(browser):
    page = _new(browser, '<div id="where-to-watch"><div id="wtw-body"></div></div>',
                scripts=('where-to-watch.js',))
    page.evaluate("""
      document.getElementById('wtw-body').innerHTML =
        '<img data-wtw-logo="Netflix" src="https://image.tmdb.org/x.jpg">';
    """)
    assert page.eval_on_selector_all('#wtw-body img[onerror]',
                                     'els => els.length') == 0


# ---------------------------------------------------------------------------
# 5. user_profile.html — the syntax repair must activate SAFE code
# ---------------------------------------------------------------------------

USER_PROFILE_SCRIPT = _inline_script('user_profile.html', 'loadSocialList')

PROFILE_SETUP = """
window.FollowButton = function (id, btn) {
  btn.setAttribute('data-follow-wired', 'true');
};
window.fetch = async () => ({ json: async () => (window.__FEED) });
"""


def _profile_page(browser, feed):
    page = _new(browser, '<div id="modal-content"></div>')
    page.evaluate('(f) => { window.__FEED = f; }', feed)
    page.add_script_tag(content=PROFILE_SETUP)
    page.add_script_tag(content=USER_PROFILE_SCRIPT)
    return page


def _feed(**over):
    base = {'followers': [{'id': 3, 'username': 'ada', 'profile_picture': None,
                           'total_reviews': 4, 'followers_count': 2,
                           'is_following': True}],
            'current_user_id': 999}
    base.update(over)
    return base


def test_user_profile_inline_script_defines_the_modal_handlers(browser):
    page = _profile_page(browser, _feed())
    for fn in ('loadSocialList', 'openFollowersModal', 'openFollowingModal',
               'closeModal'):
        assert page.evaluate('typeof %s' % fn) == 'function', \
            '%s is not defined — the block still fails to parse' % fn


def test_newly_enabled_social_list_is_not_executable(browser):
    page = _profile_page(browser, _feed(followers=[
        {'id': 3, 'username': PAYLOAD, 'profile_picture': None,
         'total_reviews': 1, 'followers_count': 1, 'is_following': False}]))
    page.evaluate('loadSocialList("/x")')
    page.wait_for_timeout(400)
    assert _fired(page) == 0, 'SCRIPT EXECUTED'
    assert page.eval_on_selector_all(
        '#modal-content [onclick]', 'els => els.length') == 0
    assert PAYLOAD in page.inner_text('#modal-content')


def test_social_list_avatar_url_is_validated(browser):
    page = _profile_page(browser, _feed(followers=[
        {'id': 3, 'username': 'ada', 'profile_picture': AVATAR_PAYLOAD,
         'total_reviews': 1, 'followers_count': 1, 'is_following': False}]))
    page.evaluate('loadSocialList("/x")')
    page.wait_for_timeout(400)
    src = page.get_attribute('#modal-content img', 'src')
    assert not src or not src.lower().startswith('javascript:')
    assert _fired(page) == 0


def test_social_list_still_renders_the_follow_button(browser):
    """The repair must not cost the feature its button."""
    page = _profile_page(browser, _feed())
    page.evaluate('loadSocialList("/x")')
    page.wait_for_timeout(400)
    btn = page.query_selector('#modal-content [data-follow-button]')
    assert btn is not None, 'the follow button must still render'
    assert 'Following' in btn.inner_text()
    assert btn.get_attribute('data-user-id') == '3'
    assert btn.get_attribute('data-follow-wired') == 'true', \
        'FollowButton must still be constructed for the new row'
    assert '4 reviews' in page.inner_text('#modal-content')


def test_social_list_omits_follow_button_for_the_viewer_themselves(browser):
    page = _profile_page(browser, _feed(current_user_id=3))
    page.evaluate('loadSocialList("/x")')
    page.wait_for_timeout(400)
    assert page.query_selector('#modal-content [data-follow-button]') is None


# ---------------------------------------------------------------------------
# 6. genre templates — repaired blocks must run and wire the menu
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('name', ['genre.html', 'tv_genre.html'])
def test_genre_template_menu_is_wired(browser, name):
    page = browser.new_page()
    page.set_content(
        '<button id="mobile-menu-button">menu</button>'
        '<div id="mobile-menu" class="hidden">x</div>')
    page.add_script_tag(content=_inline_script(name))
    assert page.evaluate('typeof setupMobileMenu') == 'function'
    page.click('#mobile-menu-button')
    page.wait_for_timeout(200)
    assert page.eval_on_selector_all(
        'body > div.fixed', 'els => els.length') >= 1, \
        'setupMobileMenu did not run'


def test_genre_template_is_safe_without_the_button(browser):
    """The repair must not throw on a page that lacks #mobile-menu-button."""
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.set_content('<div>no button here</div>')
    page.add_script_tag(content=_inline_script('genre.html'))
    page.wait_for_timeout(200)
    assert not errors, 'page error after repair: %s' % errors
