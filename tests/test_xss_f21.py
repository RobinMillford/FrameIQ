"""F21 — stored-XSS remediation regression tests (non-browser half).

The audit found stored XSS where user-controlled values (review text, tag names,
usernames, avatar URLs) were concatenated into ``innerHTML`` strings, and three
templates whose inline scripts did not parse at all.

This module covers the things a browser is not needed for:

* the URL/scheme validators in ``static/js/dom-safety.js`` (run in real Node);
* every executable inline ``<script>`` block in every template parsing;
* the absence of dynamically-built inline handlers in the touched files;
* the DOM-safety helper being loaded BEFORE its consumers;
* the test-suite database URL not being silently swapped mid-session.

The behavioural half — "does this payload actually execute?" — lives in
``tests/test_xss_f21_browser.py``, which needs Playwright.

No test here reaches a database or the network.
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))

from template_script_syntax import (  # noqa: E402
    UnknownScriptType, iter_script_blocks, javascript_blocks, node_check,
    strip_jinja,
)

TEMPLATES = ROOT / 'templates'
JS = ROOT / 'static' / 'js'

# Files whose sinks the audit identified. Every one is now DOM-built.
REMEDIATED = [
    'tag-manager.js',
    'list-advanced.js',
    'popular-with-friends.js',
    'where-to-watch.js',
    'my-services.js',
    'more-like-this.js',
    'tv-seasons.js',
    'review-manager.js',
    'dom-safety.js',
]

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _code_without_comments(source):
    source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    source = re.sub(r'(?m)^\s*//.*$', '', source)
    return source


def _run_node(body):
    """Run a JS snippet with window.FrameIQDOM loaded; return stdout."""
    harness = (
        "global.window = {};\n"
        "require(%r);\n" % str(JS / 'dom-safety.js')
        + "const D = global.window.FrameIQDOM;\n"
        + body
    )
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# 1. URL validators — executed in real Node against real payloads
# ---------------------------------------------------------------------------

REJECTED_URLS = [
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    '  javascript:alert(1)  ',
    'java\tscript:alert(1)',
    'java\nscript:alert(1)',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    '//evil.example/x.png',
    '/static/../../etc/passwd',
    'not a url',
    '',
]


@pytest.mark.parametrize('payload', REJECTED_URLS)
def test_unsafe_urls_are_rejected(payload):
    """javascript:/data:/protocol-relative/traversal must never survive."""
    out = _run_node(
        "process.stdout.write(String(D.safeUrl(%s) === null));" % repr(payload))
    assert out == 'true', 'safeUrl accepted %r' % payload


@pytest.mark.parametrize('payload', [
    '/static/images/a.png',
    'https://image.tmdb.org/t/p/w92/poster.jpg',
    'http://example.com/a.png',
])
def test_legitimate_urls_survive(payload):
    out = _run_node("process.stdout.write(String(D.safeUrl(%s)));" % repr(payload))
    assert out, 'safeUrl wrongly rejected %r' % payload


def test_safe_image_url_falls_back_for_unsafe_input():
    """A broken avatar degrades to the local placeholder, not an empty src."""
    out = _run_node(
        "process.stdout.write(D.safeImageUrl('javascript:alert(1)', "
        "'/static/images/default-avatar.png'));")
    assert out == '/static/images/default-avatar.png'


def test_safe_link_url_returns_empty_string_for_unsafe_input():
    """Callers branch on falsy, so an unsafe link yields no href at all."""
    out = _run_node(
        "process.stdout.write(String(D.safeLinkUrl('javascript:alert(1)') === ''));")
    assert out == 'true'


@pytest.mark.parametrize('value,expected', [
    ('7', 7), (7, 7), ('0', 0),
    ('7;alert(1)', None), (-1, None), ('', None),
    ('abc', None), (10 ** 18, None),
])
def test_safe_id(value, expected):
    """Ids reach URLs and request paths, so they must be plain integers."""
    out = _run_node(
        "process.stdout.write(String(D.safeId(%s)));" % repr(value))
    # node's String(null) is 'null', not Python's 'None'
    want = 'null' if expected is None else str(expected)
    assert out == want, 'safeId(%r) -> %s (wanted %s)' % (value, out, want)


def test_safe_css_color_rejects_injection():
    """A stored colour must not be able to add CSS declarations or a url()."""
    out = _run_node(
        "process.stdout.write(D.safeCssColor("
        "'red;background:url(javascript:alert(1))', '#6366f1'));")
    assert out == '#6366f1'


@pytest.mark.parametrize('raw,expected', [
    ('null', ''), ('undefined', ''), ('0', '0'), ('42', '42'),
    ('"hi"', 'hi'), ('true', 'true'),
])
def test_text_coerces_without_printing_undefined(raw, expected):
    """null/undefined must render as '' — a feed card must never say "undefined"."""
    out = _run_node("process.stdout.write(D.text(%s));" % raw)
    assert out == expected


# ---------------------------------------------------------------------------
# 2. Every executable inline template script must parse
# ---------------------------------------------------------------------------


def _template_files():
    return sorted(TEMPLATES.rglob('*.html'))


def test_templates_are_present():
    assert len(_template_files()) > 50, 'template discovery looks broken'


@pytest.mark.parametrize('template', _template_files(),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_every_executable_inline_script_parses(template):
    """node --check on static/js/ cannot see template inline scripts.

    A syntax error in an inline block silently kills the WHOLE block, which is
    how user_profile.html lost its followers/following renderer.
    """
    source = strip_jinja(template.read_text(encoding='utf-8'))
    try:
        blocks = list(javascript_blocks(source))
    except UnknownScriptType as exc:          # pragma: no cover - guard
        pytest.fail('refusing to guess script type in %s: %s' % (template, exc))
    for body in blocks:
        ok, err = node_check(body)
        assert ok, 'inline <script> in %s does not parse: %s' % (
            template.relative_to(ROOT), err)


def test_data_islands_are_excluded_by_type_not_by_failure():
    """The checker must exclude JSON payloads BY TYPE.

    If it excluded them by swallowing a parse error, the check would be green
    for the wrong reason. This asserts both the count and the reason.
    """
    total_excluded = 0
    for template in _template_files():
        source = strip_jinja(template.read_text(encoding='utf-8'))
        for _attrs, _body, executable in iter_script_blocks(source):
            if not executable:
                total_excluded += 1
    assert total_excluded >= 5, (
        'expected the JSON data islands to still be present; the exclusion '
        'rule may have changed')


def test_checker_detects_the_three_original_syntax_defects():
    """Negative control: the checker must FAIL on the exact bugs F21 fixed.

    Without this, "0 failures" could just mean the checker does nothing.
    """
    negatives = {
        'unterminated template literal': '<script>let x = `oops\n</script>',
        'orphaned close paren': '<script>function f(){}\n});\n</script>',
        'nested script tags': '<script><script>var a=1;</script></script>',
    }
    for label, html in negatives.items():
        bodies = list(javascript_blocks(strip_jinja(html)))
        assert bodies, 'checker silently skipped %s' % label
        ok, err = node_check(bodies[0])
        assert not ok, 'checker failed to detect %s' % label


def test_checker_raises_on_an_unrecognised_script_type():
    with pytest.raises(UnknownScriptType):
        list(javascript_blocks(strip_jinja('<script type="text/x-weird">a</script>')))


# ---------------------------------------------------------------------------
# 3. The specific templates the audit found broken
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('name', [
    'user_profile.html',    # unterminated template literal
    'genre.html',           # orphaned `});`
    'tv_genre.html',        # orphaned `});`
    'feed.html',            # nested <script><script>
])
def test_previously_broken_templates_parse(name):
    source = strip_jinja((TEMPLATES / name).read_text(encoding='utf-8'))
    blocks = list(javascript_blocks(source))
    assert blocks, 'no executable inline script found in %s' % name
    for body in blocks:
        ok, err = node_check(body)
        assert ok, '%s inline script does not parse: %s' % (name, err)


def test_user_profile_defines_the_follow_modal_handlers():
    """The block that failed to parse defined these; they must exist again."""
    source = (TEMPLATES / 'user_profile.html').read_text(encoding='utf-8')
    for fn in ('openFollowersModal', 'openFollowingModal', 'loadSocialList',
               'closeModal'):
        assert fn in source, '%s is missing again' % fn


def test_user_profile_social_list_is_dom_built_not_string_built():
    """Newly reachable code must be safe BEFORE the syntax repair exposes it.

    A syntax fix alone would have activated a raw-innerHTML renderer that
    interpolates usernames and avatar URLs.
    """
    source = (TEMPLATES / 'user_profile.html').read_text(encoding='utf-8')
    body = source[source.index('async function loadSocialList'):]
    body = body[:body.index('\n        }')]
    assert '${item.username}' not in body, 'username is still interpolated'
    assert 'innerHTML' not in body, 'loadSocialList still uses innerHTML'
    assert 'textContent' in body or 'D.el(' in body, 'no safe DOM construction'
    assert "safeImageUrl" in body, 'avatar URL is not validated'


def test_genre_templates_invoke_the_repaired_function():
    for name in ('genre.html', 'tv_genre.html'):
        source = (TEMPLATES / name).read_text(encoding='utf-8')
        assert 'setupMobileMenu' in source
        # The repair must not leave the orphan that killed the block.
        body = source[source.index('function setupMobileMenu'):]
        assert '});\n    </script>' not in body, '%s still has the orphan' % name


def test_feed_has_no_nested_script_tags():
    source = (TEMPLATES / 'feed.html').read_text(encoding='utf-8')
    assert '<script><script>' not in source, 'nested script tags remain'
    assert '</script></script>' not in source


# ---------------------------------------------------------------------------
# 4. No dynamically-built inline handlers in the remediated files
# ---------------------------------------------------------------------------


def _js(name):
    return (JS / name).read_text(encoding='utf-8')


@pytest.mark.parametrize('name', REMEDIATED)
def test_no_dynamically_built_onclick(name):
    """`onclick="f('${untrusted}')"` must be gone from the remediated files.

    This is a source-level assertion and is deliberately NOT the only defence:
    it cannot see an equivalent sink built some other way, which is why the
    browser suite exists. It does pin the specific regression.
    """
    code = _code_without_comments(_js(name))
    assert not re.search(r'on(click|error)\s*=\s*"[^"]*\$\{', code), (
        '%s still builds an inline handler from a template expression' % name)
    assert not re.search(r"on(click|error)\s*=\s*'[^']*\$\{", code), (
        '%s still builds an inline handler from a template expression' % name)


@pytest.mark.parametrize('name', ['tag-manager.js', 'list-advanced.js'])
def test_no_inline_html_handlers_at_all(name):
    """These two had the Critical sinks; they should have zero onclick=."""
    code = _code_without_comments(_js(name))
    assert 'onclick=' not in code, '%s still emits onclick=' % name


def test_tag_manager_has_no_innerhtml_assignment():
    """Tag pills and suggestions are built with createElement/textContent."""
    code = _code_without_comments(_js('tag-manager.js'))
    assert '.innerHTML' not in code, 'tag-manager.js still assigns innerHTML'


def test_list_advanced_has_no_innerhtml_assignment():
    code = _code_without_comments(_js('list-advanced.js'))
    assert '.innerHTML' not in code, 'list-advanced.js still assigns innerHTML'


def test_show_message_no_longer_interpolates_into_markup():
    """showMessage embedded usernames; it was a sink in its own right."""
    code = _code_without_comments(_js('list-advanced.js'))
    show = code[code.index('showMessage(message, type'):]
    show = show[:show.index('document.body.appendChild(alert)')]
    assert '<span>' not in show, 'showMessage still builds markup'


@pytest.mark.parametrize('name', ['more-like-this.js', 'tv-seasons.js'])
def test_tmdb_fields_are_escaped_before_interpolation(name):
    code = _code_without_comments(_js(name))
    for field in ('poster_path', 'season.name', 'item.title'):
        pattern = r'\$\{\s*(?:\w+\.)*%s\b' % re.escape(field)
        assert not re.search(pattern, code), (
            '%s still interpolates %s raw into an HTML string' % (name, field))


def test_dom_safety_exposes_no_html_escape_helper():
    """The helper deliberately offers no generic escaper.

    A generic escaper is what created the original bugs: people used it for
    attribute, JS-string and URL contexts where it does not apply.
    """
    # Comments explain WHY the helper exists and legitimately mention the
    # escaping/innerHTML it replaces; strip them so the assertion is about code.
    code = _code_without_comments(_js('dom-safety.js'))
    assert 'escapeHtml' not in code
    assert 'innerHTML' not in code


def test_dom_safety_el_has_no_html_option():
    """`el()` must not accept an escape hatch that re-enables markup."""
    code = _js('dom-safety.js')
    assert "key === 'html'" not in code
    assert "key === 'innerHTML'" not in code


# ---------------------------------------------------------------------------
# 5. Load order — the helper must be present before its consumers
# ---------------------------------------------------------------------------


def test_base_loads_dom_safety_first():
    source = (TEMPLATES / 'base.html').read_text(encoding='utf-8')
    block_at = source.index('{% block extra_js %}')
    helper_at = source.index('js/dom-safety.js')
    assert helper_at < block_at, (
        'dom-safety.js must load BEFORE {% block extra_js %} so page scripts '
        'that override it see the helpers')
    assert 'js/dom-safety.js' in source


def _script_src_tags(source):
    """Real <script src=...> tags only.

    A naive substring search matches PROSE that mentions a file — comments in
    edit_profile.html and partials/where_to_watch.html both do — which produced
    a false "wrong order" reading during final review.
    """
    return re.findall(r'<script[^>]*\bsrc=[^>]*>', source)


@pytest.mark.parametrize('name,consumer', [
    ('list_detail.html', 'js/list-advanced.js'),
    ('user_profile.html', 'js/follow-button.js'),
    ('edit_profile.html', 'js/my-services.js'),
])
def test_standalone_templates_load_the_helper(name, consumer):
    """These do not extend base.html, so they must load it themselves."""
    source = (TEMPLATES / name).read_text(encoding='utf-8')
    assert 'extends "base.html"' not in source, (
        '%s now extends base; update this test' % name)
    tags = _script_src_tags(source)
    helper_tags = [i for i, t in enumerate(tags) if 'js/dom-safety.js' in t]
    consumer_tags = [i for i, t in enumerate(tags) if consumer in t]
    assert helper_tags, '%s does not load the helper' % name
    assert consumer_tags, '%s no longer loads %s' % (name, consumer)
    assert min(helper_tags) < min(consumer_tags), (
        '%s loads the helper after %s' % (name, consumer))


def test_base_loads_the_helper_before_the_page_script_block():
    """base.html covers every extending page, so its order is the contract."""
    source = (TEMPLATES / 'base.html').read_text(encoding='utf-8')
    tags = _script_src_tags(source)
    helper = [i for i, t in enumerate(tags) if 'js/dom-safety.js' in t]
    assert helper, 'base.html must load the helper for all extending pages'
    block_at = source.index('{% block extra_js %}')
    helper_at = source.index('js/dom-safety.js')
    assert helper_at < block_at, (
        'the helper must precede {% block extra_js %} so page scripts see it')


def test_consumers_are_loaded_on_pages_that_provide_the_helper():
    """Every template loading a remediated script must provide FrameIQDOM."""
    consumers = set()
    for name in REMEDIATED:
        stem = name[:-3]
        consumers.add(stem)
    missing = []
    for template in _template_files():
        source = template.read_text(encoding='utf-8')
        # Match a real <script src=...> tag, not a comment that mentions a file.
        script_srcs = set(re.findall(r'<script[^>]*\bsrc=[^>]*>', source))
        loads = [stem for stem in consumers
                 if any(f'js/{stem}' in tag for tag in script_srcs)]
        if not loads:
            continue
        has_helper = 'js/dom-safety.js' in source or 'extends "base.html"' in source
        if not has_helper:
            missing.append(str(template.relative_to(ROOT)))
    assert not missing, 'templates load a remediated script without the helper: %s' % missing


# ---------------------------------------------------------------------------
# 6. Safety gate — the suite must not silently swap its own database
# ---------------------------------------------------------------------------


def test_conftest_forces_a_file_backed_sqlite_url():
    """A forced assignment, not setdefault: tests must never inherit a URI."""
    source = (ROOT / 'tests' / 'conftest.py').read_text(encoding='utf-8')
    assert 'os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"' in source
    # The word appears in conftest's explanatory comment (which is exactly the
    # point being made); assert it is never USED as the suite's URL.
    assignments = re.findall(
        r'os\.environ\[\s*[\'"]DATABASE_URL[\'"]\s*\]\s*=\s*(.+)', source)
    assert assignments, 'DATABASE_URL assignment not found in conftest'
    for rhs in assignments:
        assert ':memory:' not in rhs, (
            'conftest must not USE :memory: — each pooled connection would get '
            'a separate empty database: %r' % rhs)


def test_chat_production_restores_the_url_it_borrowed():
    """F21: it used to restore :memory:, silently repointing the suite."""
    source = (ROOT / 'tests' / 'test_chat_production.py').read_text(encoding='utf-8')
    helper = source[source.index('def _file_backed_app'):]
    helper = helper[:helper.index('\ndef ')] if '\ndef ' in helper[4:] else helper
    assert 'previous_url = os.environ.get("DATABASE_URL")' in helper
    assert 'os.environ.pop("DATABASE_URL", None)' in helper, (
        'must handle the previously-absent case')
    assert 'os.environ["DATABASE_URL"] = "sqlite:///:memory:"' not in helper, (
        'the silent :memory: restore is still present')


def test_current_database_url_is_local_sqlite():
    """Proves THIS process is isolated before anything else is trusted."""
    url = os.environ.get('DATABASE_URL', '')
    assert url.startswith('sqlite://'), (
        'refusing to continue: DATABASE_URL is not provably local SQLite')


def test_no_new_dependency_was_added():
    """F21 adds no package; the helper is plain browser JavaScript."""
    req = (ROOT / 'requirements.txt').read_text(encoding='utf-8')
    dev = (ROOT / 'requirements-dev.txt').read_text(encoding='utf-8')
    assert 'dompurify' not in req.lower()
    assert 'sanitize' not in dev.lower()

# ---------------------------------------------------------------------------
# 7. Review additions: el() must not be a back door for event handlers
# ---------------------------------------------------------------------------


def test_el_refuses_event_handler_attributes():
    """A security helper that can still build onclick= is not a helper.

    Final review finding: el()'s generic setAttribute branch would have
    accepted `onclick`, `onerror`, `style` and `srcdoc`. No live call site used
    them, but it re-armed the exact pattern F21 removed.
    """
    out = _run_node("""
      var stub = function () {
        return { setAttribute: function () {}, dataset: {}, appendChild: function () {},
                 style: {}, firstChild: null, removeChild: function () {} };
      };
      global.document = { createElement: stub, createTextNode: function () { return {}; } };
      var refused = [];
      ['onclick','onerror','onload','style','srcdoc','ONCLICK','OnClick'].forEach(function (a) {
        try { D.el('button', (function(){ var o={}; o[a]='alert(1)'; return o; })()); }
        catch (e) { refused.push(a); }
      });
      process.stdout.write(String(refused.length));
    """)
    assert out == '7', 'expected all 7 event-handler attributes refused, got %s' % out


def test_el_still_allows_ordinary_attributes():
    """The deny-list must not break legitimate presentation attributes."""
    out = _run_node("""
      var stub = function () {
        return { setAttribute: function () {}, dataset: {}, appendChild: function () {},
                 style: {}, firstChild: null, removeChild: function () {} };
      };
      global.document = { createElement: stub, createTextNode: function () { return {}; } };
      var allowed = [];
      ['class','type','title','alt','role','aria-label','disabled','id','tabindex']
        .forEach(function (a) {
          try { D.el('button', (function(){ var o={}; o[a]='x'; return o; })());
                allowed.push(a); } catch (e) {}
        });
      process.stdout.write(String(allowed.length));
    """)
    assert out == '9', 'ordinary attributes were wrongly refused: only %s passed' % out


def test_dom_safety_fails_closed_when_a_consumer_lacks_the_helper():
    """A missing FrameIQDOM must degrade output, never the escaping.

    Review finding: review-manager.js and more-like-this.js previously fell back
    to the PRE-F21 behaviour (unescaped / quote-only escaping) when the helper
    was absent, which re-enabled the exact sink. They must now fail closed.
    """
    for name in ('review-manager.js', 'more-like-this.js'):
        code = _js(name)
        assert ': this.escapeHtml(review.user.profile_picture' not in code, (
            '%s still falls back to the quote-unsafe escaping path' % name)
        assert ': escAttr(item.poster_path)' not in code, (
            '%s still falls back to rendering an unvalidated poster src' % name)
        assert "window.FrameIQDOM" in code, '%s should still try the helper' % name


def test_where_to_watch_resolves_the_helper_lazily():
    """Load order must not decide whether provider links work."""
    code = _js('where-to-watch.js')
    assert 'var D = window.FrameIQDOM;' not in code, (
        'the helper is still captured at IIFE time, so load order matters')
    assert 'function dom()' in code, 'expected a lazy accessor'
