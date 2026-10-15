"""F41: the F39 policy and the F38 browser helper must reach the same verdict.

The corpus is the one F40 found divergent on; this is the regression guard for
that agreement. The server verdict comes from ``cover_image_policy``; the
browser verdict comes from *executing* ``safeImageUrlFromHosts`` — the exact
call ``templates/lists_discover.html`` makes — under node against the real
``static/js/dom-safety.js``. Nothing here re-implements either policy.

One entry is deliberately kept out of the agreement corpus; see
``LENGTH_GUARD`` and ``test_overlength_is_a_storage_guard_not_a_format_rule``.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOM_SAFETY = ROOT / 'static' / 'js' / 'dom-safety.js'

sys.path.insert(0, str(ROOT))
from utils import cover_image_policy  # noqa: E402

#: (value, accepted) — ``accepted`` is the verdict BOTH layers must produce.
#: Ordered as the contract is stated: what is accepted, then each rejected
#: format the F41 alignment was required to settle.
FORMAT_CORPUS = [
    # -- accepted --------------------------------------------------------
    ('/static/images/x.jpg', True),                        # root-relative
    ('/lists/1', True),                                    # bare root path
    ('https://image.tmdb.org/t/p/w500/abc.jpg', True),     # ordinary poster
    ('https://IMAGE.TMDB.ORG/t/p/w500/abc.jpg', True),     # host is case-blind
    ('https://image.tmdb.org:443/t/p/w500/abc.jpg', True),  # default port
    # -- rejected: the three F40 disagreements, now both-refuse --------
    ('https://image.tmdb.org./x.jpg', False),       # trailing-dot hostname
    ('https://image.tmdb.org:8443/x.jpg', False),   # non-default port
    ('/x?y.jpg', False),                            # query in a path
    # -- rejected: the rest of the contract ---------------------------
    ('/x#frag', False),                             # fragment in a path
    ('//evil.example/pixel.png', False),            # protocol-relative
    ('https://user:pw@image.tmdb.org/x.jpg', False),   # embedded credentials
    ('https://image.tmdb.org.evil.example/x.jpg', False),  # deceptive host
    ('https://attacker.example/pixel.png', False),  # unapproved host
    ('http://image.tmdb.org/x.jpg', False),         # not https
    ('data:image/png;base64,AAAA', False),          # data URL
    ('javascript:alert(1)', False),                 # executable scheme
    ('/../../etc/passwd', False),                   # traversal
    ('/static\\evil.jpg', False),                   # backslash
    ('/static/a b.jpg', False),                     # internal whitespace
    ('/static/a\x01b.jpg', False),                  # control character
    ('x.jpg', False),                               # bare relative
    ('', False),                                    # empty
]

#: Values the server refuses on *storage* grounds rather than URL format.
#: ``cover_image`` is ``db.String(500)``; the renderer has no column to honour
#: and no reason to carry a length limit of its own, so this is not part of the
#: format contract the two layers share.
LENGTH_GUARD = [
    '/a' * 400,                       # 800 chars
    'https://image.tmdb.org/' + 'b' * 500,
]

IDS = ['%s->%s' % (repr(v)[:26], 'accept' if ok else 'refuse')
       for v, ok in FORMAT_CORPUS]

_VERDICTS = None


def _browser_verdicts():
    """Run the renderer's own helper under node; cached across the module."""
    global _VERDICTS
    if _VERDICTS is None:
        values = [v for v, _ in FORMAT_CORPUS] + LENGTH_GUARD
        script = """
global.window = {};
require(%r);
const D = global.window.FrameIQDOM;
const out = {};
for (const v of %s) {
  // exactly what templates/lists_discover.html does
  out[String(v)] = !!D.escapeAttr(
    D.safeImageUrlFromHosts(String(v || ''), ['image.tmdb.org'], ''));
}
console.log(JSON.stringify(out));
""" % (str(DOM_SAFETY), json.dumps(values))
        proc = subprocess.run(['node', '-e', script], capture_output=True,
                              text=True, cwd=ROOT, timeout=120,
                              env=dict(os.environ))
        assert proc.returncode == 0, proc.stderr[-2000:]
        _VERDICTS = json.loads(proc.stdout)
    return _VERDICTS


@pytest.mark.parametrize('value,expected', FORMAT_CORPUS, ids=IDS)
def test_server_and_browser_policies_agree_on_the_edge_case_corpus(
        value, expected):
    """One value, two layers, one verdict.

    F40 found three inputs where the server and the renderer disagreed. Each
    was resolved by refusing, so neither layer can hold a value the other will
    not act on. This asserts agreement on every row of the corpus, not only on
    the three that once diverged — a future loosening on either side turns this
    red whichever one moves.
    """
    server = cover_image_policy.is_valid_cover_image(value)
    assert server is expected, (
        'server verdict changed for %r: accepted=%s, expected=%s'
        % (value, server, expected))

    browser = _browser_verdicts()[str(value)]
    assert browser is expected, (
        'browser verdict for %r is %s, expected %s'
        % (value, browser, expected))


def test_the_two_policies_do_not_diverge_anywhere_in_the_corpus():
    """Belt and braces: compare the two verdict maps as whole objects."""
    verdicts = _browser_verdicts()
    mismatches = {}
    for value, _ in FORMAT_CORPUS:
        server = cover_image_policy.is_valid_cover_image(value)
        browser = bool(verdicts[str(value)])
        if server != browser:
            mismatches[repr(value)] = {'server': server, 'browser': browser}
    assert not mismatches, (
        'the server and browser cover policies disagree: %s' % mismatches)


@pytest.mark.parametrize('value', LENGTH_GUARD,
                         ids=['rel-%d' % len(v) for v in LENGTH_GUARD])
def test_overlength_is_a_storage_guard_not_a_format_rule(value):
    """Documented, deliberate divergence.

    The server refuses these because ``cover_image`` is ``db.String(500)``.
    The renderer has no column to honour, and inventing a length limit inside
    a generic URL helper would be an arbitrary rule rather than a safety one —
    so it renders them instead. That is safe: both forms are either
    root-relative (same origin by construction) or on the already-approved
    TMDb host, so nothing off-host can be reached. What the guard prevents is
    a value the schema cannot hold, not a value that could fetch.
    """
    assert not cover_image_policy.is_valid_cover_image(value)
    problems = cover_image_policy.cover_image_problems(value)
    assert any('500' in p for p in problems), problems

    rendered = bool(_browser_verdicts()[str(value)])
    assert rendered is True, (
        'expected the documented divergence: the renderer has no length rule')


def test_the_renderer_helper_is_the_one_under_test():
    """Guard against testing a re-implementation of the policy.

    If ``lists_discover.html`` stopped calling ``safeImageUrlFromHosts``, the
    node probe above would still pass while the real render path did something
    else entirely.
    """
    discover = (ROOT / 'templates' / 'lists_discover.html').read_text(
        encoding='utf-8')
    assert 'safeImageUrlFromHosts' in discover
    assert "['image.tmdb.org']" in discover, (
        'the renderer allowlist changed; reconcile it with ALLOWED_HOSTS')
    assert cover_image_policy.ALLOWED_HOSTS == ('image.tmdb.org',)

    source = DOM_SAFETY.read_text(encoding='utf-8')
    assert 'safeImageUrlFromHosts: safeImageUrlFromHosts' in source, (
        'dom-safety.js no longer exports safeImageUrlFromHosts')
    # The port rule must live in both host-restricted helpers, so there is one
    # host policy in the module rather than a per-call-site fix. `hostname`
    # excludes the port, so without this an approved host on any port passes.
    assert source.count("parsed.port && parsed.port !== '443'") == 2, (
        'expected the port guard in safeImageUrlFromHosts and tmdbImageUrl')
    assert source.count("built.port && built.port !== '443'") == 1, (
        'expected the port guard on tmdbImageUrl\'s built URL')
    # And the query/fragment rule must be in the renderer's path branch,
    # matching the server's refusal of `/x?y.jpg`.
    assert ("raw.indexOf('?') !== -1" in source
            and "raw.indexOf('#') !== -1" in source), (
        'the query/fragment guard is missing from the root-relative branch')
