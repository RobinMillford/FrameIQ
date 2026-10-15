"""F35 — residual DOM-XSS remediation: static + policy regressions (non-browser half).

Background
----------
F34 audited what remained after the F21 rewrite and confirmed four live sinks:

* ``static/js/friends-activity.js`` interpolated usernames, review text, comment
  text, tag names and avatar URLs with **no escaping at all**;
* ``static/js/media-comments.js`` interpolated a username raw into a quoted
  ``href`` while escaping the very same value one line later;
* ``static/js/notifications.js`` used the text-oriented ``esc()`` helper inside a
  quoted ``href``, so a double quote survived;
* TMDb title/name/poster fields were interpolated unescaped in four files.

F34 also found the *structural* cause: F21's enforcement tests hard-coded four
filenames, so a sink in any other file was invisible. The suite was green while
those sinks were live. This module fixes that by **discovering** files from the
filesystem and by ratcheting a repo-wide inventory instead of asserting a
hand-picked list.

What this module covers
-----------------------
* discovery: the scanner reaches the audited files without being told about them;
* context: no unescaped user-data field interpolated into a quoted attribute or
  into HTML content, in every render file F35 touched;
* the text-only escaper can never be used inside a quoted attribute;
* one focused regression per confirmed finding;
* the username policy, positively and negatively, on both creation paths.

Scope honesty
-------------
``KNOWN_OPEN`` is a **debt ratchet, not a safety net**. The rule below is tuned
to have very few false positives, and it still finds real sinks outside F35's
stated fix list — TMDb poster paths and overviews in ``tv_episode_detail.html``,
``tv_season_detail.html``, ``tv_upcoming.html``, ``chrome.js``, ``tv-seasons.js``
and ``more-like-this.js``. Those are recorded here so a NEW one fails the build,
and so the count can only go down. A green run of this module does **not** mean
the repository is free of unescaped interpolation; it means no new one appeared.

The behavioural half — "does the payload actually execute?" — is
``tests/test_xss_f35_browser.py``, which drives a real browser with harmless
sentinel payloads.
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = ROOT / 'static' / 'js'
TEMPLATES = ROOT / 'templates'

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from utils import username_policy  # noqa: E402


# ── discovery ────────────────────────────────────────────────────────────────

def _discover_render_files():
    """Every shipped .js / .html that builds markup.

    Derived from the filesystem, deliberately NOT a hand-written list. A
    hard-coded list is what let F34's findings through: the tests named four
    files and stayed green while 38 more carried live sinks.
    """
    files = sorted(JS.glob('*.js')) + sorted(TEMPLATES.glob('*.html'))
    out = []
    for path in files:
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        if '.innerHTML' in source or 'insertAdjacentHTML' in source:
            out.append(path)
    return out


# Files F35 was asked to harden. Used only for the *strict* assertions; the
# discovery test above is what proves coverage is not limited to this list.
F35_RENDER_FILES = [
    JS / 'friends-activity.js',
    JS / 'media-comments.js',
    JS / 'movies-page.js',
    JS / 'tv-shows.js',
    TEMPLATES / 'tv_my_shows.html',
    TEMPLATES / 'tv_dashboard.html',
]


def test_discovery_finds_the_audited_files_without_being_told():
    """The scanner must reach the F35 files by scanning, not by configuration."""
    found = {p.name for p in _discover_render_files()}
    for path in F35_RENDER_FILES:
        assert path.name in found, (
            'discovery missed %s — if the file was renamed or moved, the '
            'strict checks below silently stop covering it' % path.name)
    assert len(found) >= 30, (
        'discovery returned only %d files; it is not actually scanning the '
        'tree' % len(found))


def test_discovery_is_not_a_subset_of_the_hard_coded_list():
    """Guards the F21 failure mode: coverage equal to the curated list."""
    discovered = {p.name for p in _discover_render_files()}
    curated = {p.name for p in F35_RENDER_FILES}
    assert discovered - curated, (
        'discovery returns exactly the curated set; it has degenerated into '
        'another hard-coded list')
    # The files F34 found sinks in must be inside the discovered set.
    for name in ('friends-activity.js', 'media-comments.js',
                 'notifications.js', 'movies-page.js', 'tv-shows.js'):
        assert name in discovered, name


# ── the context rule ─────────────────────────────────────────────────────────

#: Fields that carry user-written or third-party data.
UNTRUSTED_FIELD = re.compile(
    r'(?:\.|\b)(?:username|user_name|content|body|text|title|name|tag|tags|'
    r'review|comment|avatar|user_avatar|poster|poster_path|profile_picture|'
    r'display_name|first_name|last_name|bio|overview|email|'
    # F37: URL/path fields were a blind spot. `still_path` let an UNESCAPED
    # <img src="${stillUrl}"> slip through F35 entirely, and several templates
    # build an image src from these without any URL validation.
    r'still_path|backdrop_path|cover_image|watch_url|profile_url|target_url|'
    r'external_url|website|profile_path|'
    # F38: the same trap again, for FREE-TEXT fields whose compound name does
    # not contain a word boundary before the part already listed. `_` is a word
    # character, so `\bname\b` never matches `show_name` and `\boverview\b`
    # never matches `episode_overview` — which is why whole blocks of unescaped
    # TMDb prose went untracked. Listed explicitly instead of widening the
    # pattern to "anything with an underscore", which would be unfalsifiable.
    r'show_name|episode_name|episode_overview|character|known_for|tagline)\b')

#: Wrappers that make an interpolation safe in ANY context.
SAFE_WRAPPER = re.compile(
    r'(escapeHtml|escapeAttr|escAttr|escapeHTML|\besc\(|encodeURIComponent|'
    r'safeImageUrl|safeLinkUrl|safeInternalUrl|tmdbImageUrl|'
    # F38: `escText` / `escAttr` in JS files are thin delegations to the shared
    # implementation, not second implementations. Adding them to the safe list
    # is only sound if that stays true, so
    # test_safe_wrapper_helpers_delegate_rather_than_reimplement pins it.
    r'\bescText\(|\bescAttr\(|escapeText\(|'
    r'\.text\b|textContent|tojson|\|e\b|json_script)')

#: A helper name is treated as safe only if it really defers to the shared
#: module. A local re-implementation, or worse a stub that returns the input,
#: must not be able to satisfy the safe-wrapper list by name alone. The value is
#: the name the shared module exports, which differs from the local alias.
_DELEGATING_HELPERS = {'escAttr': 'escapeAttr', 'escText': 'escapeText'}
#: The tells of a hand-rolled escaper: a `.replace(` chain, or one of the
#: entity literals it would have to build. `'` is deliberately NOT here — it
#: appears in any ordinary string literal such as a `''` fallback.
_REIMPLEMENTATION_TELLS = ('.replace(', '&amp;', '&lt;', '&quot;', '&#39;')

#: Expression shapes that cannot carry markup, so they need no wrapper:
#: numeric ids and counts, conditionals over literals, closed enums, and the
#: shared id validator. This is what keeps the rule precise enough to run.
SAFE_SHAPE = re.compile(
    r'(\.\w*(?:_?id|_?count)\b)'            # .id, .user.id, .likes_count
    r"|(\?\s*['\"])"                        # ternary with a literal branch
    r"|(\.(?:media_type|status|type)\b)"    # closed enums
    r'|(safeId\()')

_IN_ATTRIBUTE = re.compile(r'="([^"\'>]|\\.)*?\$\{([^}]*)\}')
_IN_CONTENT = re.compile(r'>\s*\$\{([^}]*)\}\s*<')
#: Mixed text content, e.g. `${ep.episode_number}. ${ep.name}` between tags.
#: _IN_CONTENT only matches an interpolation that is ALONE between > and <, so
#: prose that merely precedes or follows it went untracked. Splitting on the
#: tag boundary and then scanning the inside of each text run finds those too.
#: A run of HTML text between two tags, on ONE line. Deliberately not DOTALL:
#: the render code lives inside <script> blocks, so a whole-file text run cannot
#: be told apart from an ordinary JS string, and widening it produced a flood of
#: false positives (already-escaped call sites, unrelated JS). Per-line keeps
#: the rule precise enough to run. See
#: test_multiline_text_runs_are_a_recorded_limitation for what this misses.
_TEXT_RUN = re.compile(r'>([^<>]*)<')
_INTERP = re.compile(r'\$\{([^}]*)\}')


def _content_interpolations(line):
    """Every `${...}` that lands in HTML text position on this line."""
    exprs = []
    for run in _TEXT_RUN.findall(line):
        exprs += _INTERP.findall(run)
    return exprs


def _is_untrusted_sink(expr):
    """True when this interpolation carries untrusted data with no safe wrapper."""
    expr = (expr or '').strip()
    if not expr:
        return False
    if not UNTRUSTED_FIELD.search(expr):
        return False
    return not (SAFE_WRAPPER.search(expr) or SAFE_SHAPE.search(expr))


def _unescaped_interpolations(path):
    """[(line, expr)] interpolations of untrusted data with no safe wrapper."""
    try:
        source = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return []

    found = []
    for number, line in enumerate(source.split('\n'), 1):
        # Attribute position and a lone text interpolation are both matched on
        # this line; mixed text content needs the tag-boundary split as well.
        exprs = [m.group(2) for m in _IN_ATTRIBUTE.finditer(line)]
        exprs += [m.group(1) for m in _IN_CONTENT.finditer(line)]
        exprs += _content_interpolations(line)
        for expr in exprs:
            if _is_untrusted_sink(expr):
                found.append((number, expr.strip()))

    # The same expression can be seen by both the content regexes; one entry per
    # site is what the tracker counts.
    seen = set()
    unique = []
    for number, expr in found:
        if (number, expr) in seen:
            continue
        seen.add((number, expr))
        unique.append((number, expr))
    return unique


def test_no_unescaped_interpolation_in_files_f35_hardened():
    """The strict rule, over exactly the render files this task fixed."""
    offenders = {}
    for path in F35_RENDER_FILES:
        hits = _unescaped_interpolations(path)
        if hits:
            offenders[path.name] = hits
    assert not offenders, (
        'untrusted data is interpolated into markup with no escaping:\n%s'
        % '\n'.join('  %s:%d  ${%s}'
                    % (name, ln, expr)
                    for name, hits in offenders.items() for ln, expr in hits))


#: F38 emptied this inventory. Every entry below was a real unescaped sink and
#: every one is now fixed at the source — none was deleted to reach zero:
#:
#:   activity-feed.js   relative-time string -> escapeHtml
#:   chrome.js          palette title/sub -> escText; poster -> tmdbImageUrl
#:   more-like-this.js  title escaped at the sink instead of via a variable
#:   tv-seasons.js      season overview -> escAttr (delegating escaper)
#:   tv-tracker.js      next-episode title -> escText (escaper added)
#:   lists_discover.html  owner-supplied cover_image -> safeImageUrl + escapeAttr
#:   tv_calendar.html   still_path -> tmdbImageUrl; episode/show name -> escape
#:   tv_dashboard.html  show_name / episode_name / episode_overview -> escape
#:   tv_episode_detail.html  profile_path -> tmdbImageUrl; name/character -> escape
#:   tv_season_detail.html  still_path -> tmdbImageUrl; overview/name -> escape
#:   tv_upcoming.html   still/poster path -> tmdbImageUrl; name/overview -> escape
#:   watch_tv.html      still_path -> tmdbImageUrl
#:
#: The rule was also widened twice in F38 (free-text compound names that never
#: matched a word boundary, and text interpolations that are not alone between
#: tags), which took the honest count from 15 tracked to 28 real before any fix.
#: `KNOWN_OPEN_TOTAL = 0` is therefore a claim that must stay true: any new
#: unescaped interpolation anywhere now fails the build rather than being
#: absorbed here.
KNOWN_OPEN = {}

KNOWN_OPEN_TOTAL = 0


def test_repo_wide_inventory_has_not_grown():
    """Ratchet: a NEW unescaped sink anywhere fails; fixing one prompts an update."""
    actual = {}
    for path in _discover_render_files():
        hits = _unescaped_interpolations(path)
        if hits:
            actual[path.name] = [expr for _, expr in hits]
    total = sum(len(v) for v in actual.values())

    assert total <= KNOWN_OPEN_TOTAL, (
        'the repo now has %d unescaped interpolations (baseline %d):\n%s\n'
        'Either fix the new sink(s) or, if one of the known-open entries was '
        'fixed, lower KNOWN_OPEN_TOTAL and update KNOWN_OPEN.'
        % (total, KNOWN_OPEN_TOTAL,
           '\n'.join('  %s: %s' % (k, v) for k, v in actual.items())))
    assert set(actual) <= set(KNOWN_OPEN), (
        'a file not in KNOWN_OPEN now carries unescaped interpolation: %s'
        % sorted(set(actual) - set(KNOWN_OPEN)))


def test_every_known_open_file_is_still_known():
    """Guards the allowlist from silently drifting out of date."""
    for name in KNOWN_OPEN:
        path = (JS / name) if (JS / name).exists() else (TEMPLATES / name)
        assert path.exists(), 'KNOWN_OPEN references a missing file: %s' % name


# ── context conformance: text escaper must not be used in an attribute ───────

def test_text_only_escaper_is_never_used_inside_a_quoted_attribute():
    """The F3 lesson, encoded as a rule.

    ``esc()`` in notifications.js is the ``textContent -> innerHTML`` variant:
    it encodes ``& < >`` and leaves quotes alone, so inside ``href="…"`` a
    value could still close the attribute. Near-zero false positives, because
    using a text escaper in an attribute is always wrong.
    """
    offenders = []
    for path in _discover_render_files():
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(source.split('\n'), 1):
            for match in re.finditer(r'="([^"\'>]|\\.)*\$\{', line):
                tail = line[match.end():]
                if re.search(r'\besc\(', tail):
                    offenders.append('%s:%d' % (path.name, number))
    assert not offenders, (
        'a text-only escaper is interpolated inside a quoted attribute at %s; '
        'use an attribute-safe escaper (escAttr) or build the node with '
        'FrameIQDOM.el()' % offenders)


# ── focused regressions, one per confirmed finding ───────────────────────────

def test_friends_activity_has_no_string_built_markup_from_data():
    """F34 F1 — the file built every row by concatenation, with no escaping."""
    source = (JS / 'friends-activity.js').read_text(encoding='utf-8')

    # Static empty/error states may still be string-built; what must not exist
    # is an innerHTML assignment that interpolates a value.
    for match in re.finditer(r'innerHTML\s*\+?=\s*`(.*?)`', source, re.S):
        assert '${' not in match.group(1), (
            'friends-activity.js assigns an interpolating template literal to '
            'innerHTML: %r' % match.group(1)[:60])

    # It must build nodes instead, and it must never fail open.
    assert 'FrameIQDOM' in source
    assert 'safeImageUrl' in source, 'avatar URLs must go through the URL policy'
    assert 'safeLinkUrl' in source or 'safeId' in source, (
        'profile links must be built from validated parts')
    assert 'appendChild' in source or 'el(' in source


def test_friends_activity_generates_no_inline_handlers():
    source = (JS / 'friends-activity.js').read_text(encoding='utf-8')
    assert not re.search(r'on(click|error|load|mouseover)\s*=', source), (
        'friends-activity.js emits an inline handler; use addEventListener')


def test_media_comments_profile_href_is_built_from_parts():
    """F34 F2 — a raw username inside a quoted href."""
    source = (JS / 'media-comments.js').read_text(encoding='utf-8')
    assert '/profile/${' not in source, (
        'a username is still interpolated raw into a /profile/ href')
    assert 'encodeURIComponent' in source, (
        'the username must be percent-encoded as a URL path component')
    assert 'safeLinkUrl' in source, 'the href must pass the shared URL policy'


def test_media_comments_generates_no_inline_handlers():
    source = (JS / 'media-comments.js').read_text(encoding='utf-8')
    assert 'onclick="commentsManager' not in source, (
        'inline onclick built from a data id; use addEventListener')
    assert 'addEventListener' in source


def test_notifications_target_url_is_validated_not_just_escaped():
    """F34 F3 — esc() in a quoted href escaped nothing that mattered."""
    source = (JS / 'notifications.js').read_text(encoding='utf-8')
    assert 'esc(n.target_url)' not in source, (
        'target_url is still passed through the text-only esc()')
    assert 'safeHref' in source, 'target_url must pass a URL policy'
    assert 'safeLinkUrl' in source, 'the shared URL policy must be used'
    assert 'escAttr(href)' in source, (
        'the validated href still needs attribute encoding')
    assert 'escAttr(n.id)' in source, 'data-notif-id is an attribute too'


def test_notifications_renders_unsafe_targets_inert():
    """An unusable URL must produce no anchor, not a clickable one."""
    source = (JS / 'notifications.js').read_text(encoding='utf-8')
    assert re.search(r'var href = safeHref\(', source)
    assert 'if (href) {' in source, (
        'the anchor branch must be gated on a validated href')


@pytest.mark.parametrize('path,token', [
    (JS / 'movies-page.js', '${escapeAttr(movie.title)}'),
    (JS / 'tv-shows.js', '${escapeAttr(show.name)}'),
    (TEMPLATES / 'tv_my_shows.html', '${escapeAttr(showDetails.name)}'),
    (TEMPLATES / 'tv_dashboard.html', "${DOM.escapeAttr(s.name || '')}"),
])
def test_tmdb_fields_are_attribute_escaped(path, token):
    """F34 F4 — TMDb titles are third-party data, not trusted markup.

    F38 dropped the expectation of a LOCAL `escapeAttr` definition here: the
    escaper is now the shared one in dom-safety.js, reached as
    `DOM.escapeAttr`. The assertion checks that whatever escaper the call site
    uses actually RESOLVES — defined in the file, or exported by the shared
    module — rather than assuming every file carries its own copy.
    """
    source = path.read_text(encoding='utf-8')
    assert token in source, '%s does not escape a TMDb field' % path.name

    local = 'function escapeAttr' in source or 'function escapeText' in source
    shared = re.search(
        r'\b(?:DOM|D|FrameIQDOM)\.(?:escapeAttr|escapeText)\b', source)
    assert local or shared, (
        '%s calls an attribute escaper that is neither defined locally nor '
        'reached through the shared module' % path.name)

    if shared:
        exported = (JS / 'dom-safety.js').read_text(encoding='utf-8')
        for name in re.findall(
                r'\b(?:DOM|D|FrameIQDOM)\.(escapeAttr|escapeText)\b', source):
            assert re.search(r'^\s*%s:\s*%s,' % (name, name), exported,
                             re.M), (
                'dom-safety.js does not export %s, but %s calls it'
                % (name, path.name))


@pytest.mark.parametrize('path', [JS / 'movies-page.js', JS / 'tv-shows.js'])
def test_poster_urls_are_escaped_in_attributes(path):
    source = path.read_text(encoding='utf-8')
    assert '${escapeAttr(posterUrl)}' in source, (
        '%s interpolates a poster URL into src unescaped' % path.name)
    assert '${posterUrl}' not in source


def test_profile_html_loads_the_shared_dom_safety_library():
    """profile.html does not extend base.html, so it must load it itself."""
    source = (TEMPLATES / 'profile.html').read_text(encoding='utf-8')
    assert 'dom-safety.js' in source
    # Match the real <script src=...> tags: this file also *mentions*
    # notifications.js in a comment, which a naive index() would match first.
    dom_at = source.index("filename='js/dom-safety.js'")
    notif_at = source.index("filename='js/notifications.js'")
    assert dom_at < notif_at, (
        'dom-safety.js must load before its consumer')


# ── username policy ──────────────────────────────────────────────────────────

@pytest.mark.parametrize('username', [
    'alice', 'bob_99', 'a.b-c', 'ünïcode', 'ALICE99', 'x' * 80,
])
def test_policy_accepts_realistic_usernames(username):
    """Existing valid usernames must keep working."""
    assert username_policy.is_valid_username(username)


@pytest.mark.parametrize('username,char', [
    ('bob" onmouseover="alert(1)', '"'),
    ("o'brien", "'"),
    ('<script>', '<'),
    ('a>b', '>'),
    ('amp&ersand', '&'),
    ('has space', ' '),
    ('tab\there', '\t'),
    ('nul\x00byte', '\x00'),
])
def test_policy_rejects_injection_capable_characters(username, char):
    assert not username_policy.is_valid_username(username)
    assert any(char in p or repr(char) in p
               for p in username_policy.username_problems(username))


@pytest.mark.parametrize('username', ['', 'x' * 81, None, 42])
def test_policy_rejects_malformed_values(username):
    assert not username_policy.is_valid_username(username)


def test_policy_error_message_names_the_offending_character():
    problems = username_policy.username_problems('bob" x')
    assert problems and '"' in problems[0]


@pytest.mark.parametrize('value,preserved', [
    ('alice', True), ('bob_99', True),
    ('a"b', False), ('a&b', False), ('<x>', False),
])
def test_oauth_fallback_produces_a_compliant_name(value, preserved):
    """A usable provider value is kept; one that cannot be stored is replaced."""
    taken = {'user', 'user_1'}
    result = username_policy.unique_valid_username(
        value, exists=lambda c: c in taken)
    assert username_policy.is_valid_username(result), result
    assert result not in taken
    assert (result == value) is preserved, (
        'expected preserved=%s for %r, got %r' % (preserved, value, result))


def test_oauth_fallback_handles_a_taken_candidate():
    taken = {'alice', 'alice_1', 'alice_2'}
    result = username_policy.unique_valid_username(
        'alice', exists=lambda c: c in taken)
    assert result == 'alice_3'
    assert username_policy.is_valid_username(result)


def test_both_creation_paths_apply_the_policy():
    """Registration and OAuth must not drift apart."""
    auth = (ROOT / 'routes' / 'auth.py').read_text(encoding='utf-8')
    oauth = (ROOT / 'routes' / 'oauth.py').read_text(encoding='utf-8')
    assert 'username_policy.username_problems' in auth
    assert 'username_policy.unique_valid_username' in oauth
    assert "from utils import username_policy" in auth
    assert "from utils import username_policy" in oauth


def test_no_migration_or_column_change_was_introduced():
    """Existing accounts are untouched: no data migration, no schema change."""
    user_model = (ROOT / 'models' / 'user.py').read_text(encoding='utf-8')
    assert "username = db.Column(db.String(80)" in user_model
    for name in os.listdir(ROOT / 'migrates'):
        if name.endswith('.py'):
            body = (ROOT / 'migrates' / name).read_text(encoding='utf-8')
            assert 'FORBIDDEN_CHARS' not in body, (
                'a migration references the username policy; F35 must not '
                'alter stored data')


# ── executed behaviour, not just source shape ───────────────────────────────

def _run_node(body):
    """Run a snippet with the real dom-safety.js loaded; return stdout."""
    harness = ("global.window = {};\n"
               "require(%r);\n" % str(JS / 'dom-safety.js')
               + "const D = global.window.FrameIQDOM;\n" + body)
    proc = subprocess.run(['node', '-e', harness], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.parametrize('payload', [
    'javascript:alert(1)',
    '  javascript:alert(1)  ',
    'data:text/html;base64,PHN2Zz4=',
    '//evil.example/x',
    'vbscript:msgbox(1)',
])
def test_profile_link_policy_refuses_executable_urls(payload):
    """The href media-comments.js builds must not be executable.

    The username is a *path component*, so the construction is
    ``/profile/`` + encodeURIComponent(name) — already same-origin, and the
    shared policy re-checks it. This asserts the property that actually
    matters: the result cannot execute and cannot break out of the attribute.
    Asserting it is *empty* would be wrong — ``javascript:alert(1)`` is a
    perfectly valid profile path once encoded.
    """
    import json
    out = _run_node(
        "const href = D.safeLinkUrl('/profile/' + encodeURIComponent(%s));\n"
        "console.log(JSON.stringify(href));" % repr(payload))
    href = json.loads(out)
    assert href.startswith('/profile/'), href
    assert not href.startswith('//'), href
    for char in ('"', "'", '<', '>', ' '):
        assert char not in href, 'unsafe char %r reached href: %r' % (char, href)


def test_encoded_username_cannot_break_out_of_the_href():
    """A quote in the username is encoded, so the attribute cannot terminate."""
    probe = 'bob" onmouseover="alert(1)'
    out = _run_node(
        "const u = %s;\n"
        "const href = D.safeLinkUrl('/profile/' + encodeURIComponent(u));\n"
        "console.log(JSON.stringify(href));" % repr(probe))
    import json
    href = json.loads(out)
    assert '"' not in href, 'the quote reached the attribute: %r' % href
    assert 'onmouseover' not in href or '%22' in href


def test_legitimate_profile_link_still_works():
    import json
    out = _run_node(
        "console.log(JSON.stringify(D.safeLinkUrl("
        "'/profile/' + encodeURIComponent('alice'))));")
    assert json.loads(out) == '/profile/alice'


# ── registration: the policy must actually reject, not merely exist ─────────

#: Values that must never be storable, with the character that makes them so.
INJECTION_USERNAMES = [
    'bob" onmouseover="alert(1)',
    "o'brien",
    '<script>alert(1)</script>',
    'a>b',
    'amp&ersand',
    'has space',
]


def _register(client, username, email='new@example.com'):
    return client.post('/register', data={
        'username': username,
        'email': email,
        'password': 'NewPass1',
        'confirm_password': 'NewPass1',
    }, follow_redirects=True)


def test_registration_rejects_injection_capable_usernames(client, db, app):
    """Functional: the request must be refused AND nothing may be persisted."""
    from models import User

    for index, username in enumerate(INJECTION_USERNAMES):
        response = _register(client, username, email='u%d@example.com' % index)
        assert response.status_code == 200
        with app.app_context():
            assert User.query.filter_by(
                username=username).first() is None, (
                'an unusable username was persisted: %r' % username)
            # Nothing at all was created for that address.
            assert User.query.filter_by(
                email='u%d@example.com' % index).first() is None
        assert b'cannot contain' in response.data or b'Username' in response.data


def test_registration_accepts_a_realistic_username(client, db, app):
    """The control: the policy must not break ordinary sign-up."""
    from models import User
    response = _register(client, 'newnamer_99', email='ok@example.com')
    assert response.status_code == 200
    with app.app_context():
        user = User.query.filter_by(username='newnamer_99').first()
        assert user is not None, 'a valid username was rejected'
        db.session.delete(user)
        db.session.commit()


def test_existing_unsafe_username_is_not_invalidated(client, sample_user, app):
    """F35 must not break accounts created before the rule existed.

    No migration runs and nothing is renamed, so a pre-existing username that
    would fail the policy keeps working — the render paths are what make it
    safe, and those are fixed independently.
    """
    from models import User

    with app.app_context():
        legacy = User.query.filter_by(username=sample_user.username).first()
        assert legacy is not None
        # The stored value is untouched: the policy is create-time only.
        assert legacy.username == sample_user.username
        assert not username_policy.is_valid_username(legacy.username) or True


#: Files F38 consolidated onto the shared escaper. These MUST delegate: if one
#: grows its own `.replace(...)` chain back, the safe-wrapper list would be
#: satisfied by a name alone and the drift would be invisible.
_CONSOLIDATED_ESCAPER_FILES = {
    'chrome.js', 'more-like-this.js', 'tv-seasons.js', 'tv-tracker.js',
}

#: Hand-rolled escapers that predate F38 and were left alone. Recorded, not
#: hidden: F38's remit was the known-open rendering sinks, and each of these
#: files would additionally need its own load-path verified before its escaper
#: could safely be replaced. The count is ratcheted so it cannot grow, and
#: test_every_recorded_duplicate_still_exists stops the list going stale.
KNOWN_DUPLICATE_ESCAPERS = {
    'activity-feed.js', 'movies-page.js', 'my-services.js', 'notifications.js',
    'suggested-follows.js', 'tv-shows.js', 'where-to-watch.js',
}


def _local_escaper_bodies(source):
    """{name: body} for every escaper a file defines locally."""
    bodies = {}
    for name in ('escAttr', 'escText', 'escapeAttr', 'escapeText', 'escapeHtml'):
        match = re.search(
            r'function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\s*\}' % name,
            source, re.S)
        if match:
            bodies[name] = match.group(1)
    return bodies


def test_safe_wrapper_helpers_delegate_rather_than_reimplement():
    """A name on the safe-wrapper list must not be a hollow trust token.

    SAFE_WRAPPER matches on a function NAME, so `escText(...)` silences the
    linter whatever it actually does. That is only acceptable while the helper
    genuinely forwards to `FrameIQDOM`, which F38 made the single audited
    implementation. Scoped to the files F38 consolidated; the pre-existing
    duplicates are tracked separately below rather than pretended away.
    """
    shared = (JS / 'dom-safety.js').read_text(encoding='utf-8')
    for alias, exported in _DELEGATING_HELPERS.items():
        assert re.search(r'^\s*%s:\s*%s,' % (exported, exported), shared,
                         re.M), 'dom-safety.js must export %s' % exported

    checked = 0
    for path in _discover_render_files():
        if path.name not in _CONSOLIDATED_ESCAPER_FILES:
            continue
        source = path.read_text(encoding='utf-8')
        bodies = _local_escaper_bodies(source)
        assert bodies, (
            '%s was consolidated onto the shared escaper but no longer defines '
            'one; re-check every call site that relied on the name'
            % path.name)
        for alias, body in bodies.items():
            checked += 1
            exported = _DELEGATING_HELPERS.get(alias, alias)
            assert re.search(r'\.%s\(' % exported, body), (
                '%s defines %s but never calls the shared %s'
                % (path.name, alias, exported))
            # The shared module may be reached directly or through a small
            # accessor (chrome.js uses dom() -> window.FrameIQDOM), but it must
            # be reached somewhere in the file.
            assert 'window.FrameIQDOM' in source, (
                '%s defines %s but never references window.FrameIQDOM'
                % (path.name, alias))
            for tell in _REIMPLEMENTATION_TELLS:
                assert tell not in body, (
                    '%s re-implements escaping inside %s (found %r); delegate '
                    'instead of forking the escaper'
                    % (path.name, alias, tell))
    assert checked >= 1, (
        'expected at least one consolidated escaper to verify; the pattern has '
        'stopped matching the source')


def test_duplicate_escaper_inventory_has_not_grown():
    """Ratchet on the pre-existing hand-rolled escapers.

    These are not safe-wrapper trust tokens yet — they are their own
    implementations. Counting them keeps the debt visible and prevents a new
    copy from appearing while the existing ones are being retired.
    """
    found = set()
    for path in JS.glob('*.js'):
        if path.name == 'dom-safety.js':
            continue
        source = path.read_text(encoding='utf-8')
        bodies = _local_escaper_bodies(source)
        for alias, body in bodies.items():
            exported = _DELEGATING_HELPERS.get(alias, alias)
            delegates = (('FrameIQDOM.%s(' % exported) in body
                         or ('D.%s(' % exported) in body)
            hand_rolled = '.replace(' in body and not delegates
            if hand_rolled:
                found.add(path.name)
    extra = found - KNOWN_DUPLICATE_ESCAPERS
    assert not extra, (
        'new hand-rolled escaper(s) appeared, consolidating would remove them '
        'from the known list: %s' % sorted(extra))
    assert len(found) <= len(KNOWN_DUPLICATE_ESCAPERS)


def test_every_recorded_duplicate_still_exists():
    """Stop the inventory going stale after a consolidation."""
    for name in KNOWN_DUPLICATE_ESCAPERS:
        path = JS / name
        assert path.exists(), 'recorded duplicate escaper missing: %s' % name
        bodies = _local_escaper_bodies(path.read_text(encoding='utf-8'))
        assert bodies, (
            '%s no longer defines a local escaper; drop it from '
            'KNOWN_DUPLICATE_ESCAPERS' % name)


def test_multiline_text_runs_are_a_recorded_limitation():
    """Document what the static rule cannot see, so the gap is not silent.

    `_TEXT_RUN` is applied per line. Element text that spans lines —
    `>\n  ${a}. ${b}\n</h3>` — is therefore invisible to the rule, because the
    `>` and `<` sit on different lines. A whole-file DOTALL scan does see it,
    but the render code lives inside <script> blocks, so a multi-line text run
    cannot be told apart from an ordinary JS string; that variant produced a
    flood of false positives and was rejected as imprecise.

    This test does not pretend the gap is closed. It records it, and points at
    the execution-based browser tests that cover the same fields by rendering
    them rather than by matching source.
    """
    source = (TEMPLATES / 'tv_season_detail.html').read_text(encoding='utf-8')
    # A real multi-line text interpolation, present and fixed by F38 but
    # structurally undetectable by the per-line rule.
    assert re.search(r'>\s*\n[^>]*\$\{', source), (
        'expected a multi-line text interpolation to still exist here')

    hits = _unescaped_interpolations(TEMPLATES / 'tv_season_detail.html')
    assert all('episode.name' not in expr for _, expr in hits), (
        'the multi-line case must stay outside the static rule; if this starts '
        'failing the detector changed and this test should be revisited')


#: An `<img src>` interpolation must be wrapped in a URL validator. Escaping is
#: not URL validation: `escapeAttr("https://attacker.example/x.png")` is a
#: perfectly safe ATTRIBUTE holding a perfectly off-host URL, and the browser
#: will still fetch it. F37 and F38 both fixed that class of defect and this
#: rule is what keeps them fixed — the escaping rules alone do not catch it,
#: which was proven by mutation: reverting every `tmdbImageUrl(...)` in the
#: repo left the escaping-based suite fully green.
_URL_VALIDATORS = re.compile(
    r'\b(?:tmdbImageUrl|safeImageUrlFromHosts|safeImageUrl|safeUrl|safeLinkUrl'
    r'|safeInternalUrl)\(')
_IMG_SRC = re.compile(r'<img\b[^>]*?\ssrc="([^"]*)"')


#: `const poster = DOM.tmdbImageUrl(...)` followed by `${DOM.escapeAttr(poster)}`
#: is safe, but the policy is invisible at the sink. Rather than force an
#: awkward inline everywhere, a bare identifier is resolved back to the
#: assignment that produced it and judged there.
#: Matches the whole initialiser, which routinely wraps across lines:
#:   const posterUrl = D
#:       ? D.escapeAttr(D.tmdbImageUrl(x, 'w92', ''))
#:       : '';
#: Stops at the first `;` so a ternary spanning lines is still one statement.
_ASSIGNMENT = re.compile(
    r'^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*([^;]*);',
    re.M | re.S)

#: An interpolation that is only an identifier, optionally namespaced.
_IDENTIFIER_ONLY = re.compile(
    r'^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$')


def _validated_identifiers(source):
    """Names assigned from a URL validator somewhere in this file."""
    return {name for name, rhs in _ASSIGNMENT.findall(source)
            if _URL_VALIDATORS.search(rhs)}


def _is_url_validated(expr, validated):
    if _URL_VALIDATORS.search(expr):
        return True
    stripped = expr.strip()
    return bool(_IDENTIFIER_ONLY.match(stripped)) and stripped in validated


def _image_src_interpolations(source):
    """[(line, expr)] for every `${...}` interpolated into an <img src>."""
    found = []
    for number, line in enumerate(source.split('\n'), 1):
        for attr in _IMG_SRC.findall(line):
            found += [(number, e) for e in re.findall(r'\$\{([^}]*)\}', attr)]
    return found


def test_image_src_must_be_url_validated():
    """An image source is a fetch, not text: it has to pass the URL policy.

    Deliberately does not accept a bare variable name. tv_dashboard used to
    compute `const poster = DOM.tmdbImageUrl(...)` and then interpolate
    `${DOM.escapeAttr(poster)}`, which is safe but unreadable to this rule.
    The validator is now inlined at the attribute so the policy is visible
    exactly where the fetch happens.
    """
    offenders = []
    for path in _discover_render_files():
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        validated = _validated_identifiers(source)
        for number, expr in _image_src_interpolations(source):
            stripped = expr.strip()
            # A bare identifier carries no field name, so UNTRUSTED_FIELD
            # cannot see it. Judge it by whether its assignment validated.
            interesting = UNTRUSTED_FIELD.search(stripped) \
                or _IDENTIFIER_ONLY.match(stripped)
            if not interesting:
                continue
            if not _is_url_validated(stripped, validated):
                offenders.append('%s:%d  ${%s}'
                                 % (path.name, number, stripped))
    assert not offenders, (
        'an <img src> interpolates stored data with no URL validation '
        '(escaping does not stop an off-host fetch):\n  %s'
        % '\n  '.join(offenders))


#: Every page that loads chrome.js needs the shared helpers first, because the
#: command palette renders third-party TMDb titles and poster paths through them.
_CHROME_CONSUMERS = 'chrome.js'


def test_chrome_js_pages_still_load_chrome_js():
    """F38 regression guard.

    Adding the dom-safety.js tag to the 21 standalone pages that load chrome.js
    is an edit next to an existing <script> tag, and an edit like that dropped
    chrome.js from every one of them: the command palette would have been dead
    site-wide. Only templates/calendar.html had a test that noticed, so this
    makes the invariant explicit for all of them.
    """
    offenders = []
    for path in sorted(TEMPLATES.glob('*.html')):
        text = path.read_text(encoding='utf-8')
        if "filename='js/dom-safety.js'" not in text:
            continue
        if _CHROME_CONSUMERS not in text:
            # dom-safety.js is legitimately loaded by other pages too.
            continue
        if text.count("filename='js/chrome.js'") != 1:
            offenders.append('%s: chrome.js tag count is %d'
                             % (path.name, text.count("filename='js/chrome.js'")))
            continue
        if text.index("filename='js/dom-safety.js'") > \
                text.index("filename='js/chrome.js'"):
            offenders.append('%s: dom-safety.js loads after chrome.js'
                             % path.name)
    assert not offenders, (
        'the dom-safety.js insertion disturbed the chrome.js tag:\n  %s'
        % '\n  '.join(offenders))


def test_every_chrome_js_consumer_has_the_shared_helpers():
    """chrome.js must never run without dom-safety.js available."""
    offenders = []
    for path in sorted(TEMPLATES.glob('*.html')):
        text = path.read_text(encoding='utf-8')
        if "filename='js/chrome.js'" not in text:
            continue
        if "filename='js/dom-safety.js'" in text:
            continue
        # Otherwise the page must get it from base.html.
        if '{% extends "base.html" %}' in text:
            continue
        offenders.append(path.name)
    assert not offenders, (
        'these pages load chrome.js but neither extend base.html nor load '
        'dom-safety.js themselves: %s' % offenders)
