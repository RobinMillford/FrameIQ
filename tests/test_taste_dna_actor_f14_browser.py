"""Browser tests for the F14 "Actors in your taste" section.

These execute the REAL ``static/js/taste-dna.js`` in a real headless Chromium.
Following the convention established by ``tests/test_explore_browser.py``, the
page is a standalone ``set_content`` harness with ``fetch`` stubbed inside the
page — so there is NO Flask server, NO database and NO network. Nothing here
imports the application, so the production database can never be reached.

The served HTML is the REAL ``templates/profile.html`` — the Taste DNA
definition is extracted from it so the JS binds to the actual element ids the
template ships, not to ids invented by the test. If a template id were renamed,
this harness would stop binding and the section would never render, so the test
would fail rather than silently pass.

What a browser proves that a source assertion cannot: that the section actually
renders, that resolvable actors become real focusable anchors pointing at the
existing /actor/<id> route, that an unresolvable actor stays plain text instead
of becoming an inert or wrong link, and that the honest "cast not enriched" state
does not tell the user they have no actor preferences.
"""
import json
import pathlib
import re

import pytest

playwright_api = pytest.importorskip(
    'playwright.sync_api', reason='playwright not installed')

from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / 'static/js/taste-dna.js').read_text(encoding='utf-8')
PROFILE = (ROOT / 'templates/profile.html').read_text(encoding='utf-8')


def _taste_dna_markup():
    """Extract the real Taste DNA container from the shipped template.

    Taking the real markup (not a hand-built copy) is what makes this an honest
    browser test: it fails if the template and the JS ever disagree about ids.
    """
    start = PROFILE.index('id="taste-dna-section"')
    start = PROFILE.rindex('<div', 0, start)
    # Walk <div> depth from the opening tag to find its true closing tag —
    # the block contains nested <dl>/<dt>/<dd>, so a text search would stop
    # at the wrong place.
    depth = 0
    end = None
    for m in re.finditer(r'</?div\b[^>]*>', PROFILE[start:]):
        depth += -1 if m.group(0).startswith('</') else 1
        if depth == 0:
            end = start + m.end()
            break
    assert end is not None, 'could not find the taste-dna-section boundary'
    body = PROFILE[start:end]
    # The block is a Jinja <dl>; drop any remaining template syntax so the
    # browser gets valid HTML.
    body = re.sub(r'\{[%{].*?[%}]\}', '', body, flags=re.S)
    return body


STUB = """<script>
window.fetch = function () {
  return Promise.resolve({ ok: true,
    json: function () { return Promise.resolve(__PAYLOAD__); } });
};
</script>"""


def html_for(payload):
    """Real template markup + real JS, with fetch stubbed BEFORE the JS runs.

    The stub is an inline <script> ahead of taste-dna.js rather than an
    init-script, so the very first DOMContentLoaded already sees the stub and
    there is no race and no second navigation.

    Built by concatenation, not %-formatting: the real Tailwind markup is full
    of bare `%` characters that would corrupt a format string.
    """
    # Tailwind's own `.hidden` semantics, so visibility assertions mean what
    # they mean in production instead of passing on an unstyled harness.
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<title>Taste DNA harness</title>'
            '<style>.hidden{display:none}</style></head><body>'
            + _taste_dna_markup()
            + STUB.replace('__PAYLOAD__', json.dumps(payload))
            + '<script>' + JS + '</script></body></html>')


@pytest.fixture(scope='module')
def _browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def taste(_browser):
    """Factory: render the real JS with a given API payload."""
    pages = []

    def make(payload, *, wait=True):
        page = _browser.new_context().new_page()
        pages.append(page)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content(html_for(payload))
        if wait:
            page.wait_for_selector('#taste-dna-content:not(.hidden)',
                                   timeout=5000)
        page.f14_errors = errors
        return page

    yield make
    for page in pages:
        page.close()


BASE = {
    'available': True,
    'level': 'strong',
    'confidence': 0.73,
    'titles_analyzed': 42,
    'top_genres': [{'name': 'Drama', 'strength': 'high'}],
    'top_directors': [{'name': 'Denis Villeneuve', 'strength': 'high'}],
    'eras': [{'name': '2010s', 'strength': 'high'}],
}


def _payload(**extra):
    return dict(BASE, **extra)


# ── rendering ───────────────────────────────────────────────────────────────

def test_actor_section_renders(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'Cillian Murphy', 'strength': 'high',
                     'url': '/actor/1000081'}]))
    assert page.is_visible('#taste-dna-actors-block')
    assert page.inner_text('#taste-dna-actors').strip() \
        == 'Cillian Murphy'


def test_actor_link_points_at_the_existing_route(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'Someone', 'strength': 'high',
                     'url': '/actor/4242'}]))
    hrefs = page.eval_on_selector_all(
        '#taste-dna-actors a', 'els => els.map(e => e.getAttribute("href"))')
    assert hrefs == ['/actor/4242']


def test_actor_link_is_a_real_focusable_anchor(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'Someone', 'strength': 'high',
                     'url': '/actor/4242'}]))
    assert page.eval_on_selector(
        '#taste-dna-actors a', 'e => e.tagName') == 'A'
    assert page.eval_on_selector(
        '#taste-dna-actors a', 'e => e.tabIndex') >= 0
    page.focus('#taste-dna-actors a')
    assert page.evaluate('document.activeElement.tagName') == 'A'
    assert page.eval_on_selector(
        '#taste-dna-actors a',
        'e => getComputedStyle(e).outlineStyle') is not None


def test_unresolvable_actor_is_plain_text_not_a_link(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'Ambiguous Person', 'strength': 'high'}]))
    assert page.is_visible('#taste-dna-actors-block')
    assert page.inner_text('#taste-dna-actors').strip() == 'Ambiguous Person'
    assert page.eval_on_selector_all('#taste-dna-actors a', 'e => e.length') == 0


def test_mixed_linked_and_unlinked_actors(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'Linked', 'strength': 'high',
                     'url': '/actor/7'},
                    {'name': 'Unlinked', 'strength': 'low'}]))
    assert page.eval_on_selector_all('#taste-dna-actors a', 'e => e.length') == 1
    text = page.inner_text('#taste-dna-actors')
    assert 'Linked' in text and 'Unlinked' in text


def test_malformed_actor_entries_do_not_break_the_page(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{}, {'strength': 'high'}, None,
                    {'name': 'Fine', 'strength': 'low'}]))
    assert page.f14_errors == []
    assert page.is_visible('#taste-dna-section')
    assert 'Fine' in page.inner_text('#taste-dna-actors')


# ── empty / unavailable ─────────────────────────────────────────────────────

def test_unenriched_state_renders_the_coverage_message(taste):
    """'no_cast' = cast metadata is still limited (a FrameIQ-side gap)."""
    page = taste(_payload(actors_status='no_cast'))
    assert page.is_visible('#taste-dna-actors-empty-block')
    assert not page.is_visible('#taste-dna-actors-block')
    text = page.inner_text('#taste-dna-actors-empty')
    assert 'cast metadata is still limited' in text
    assert 'preferences' not in text.lower()


def test_no_qualifying_actors_renders_a_different_message(taste):
    """'no_qualifying_actors' = cast data EXISTS, nothing positive yet.

    It must NOT reuse the coverage wording, which would wrongly claim the
    user's cast metadata is missing.
    """
    page = taste(_payload(actors_status='no_qualifying_actors'))
    assert page.is_visible('#taste-dna-actors-empty-block')
    text = page.inner_text('#taste-dna-actors-empty')
    assert 'No actors stand out yet' in text
    assert 'cast metadata is still limited' not in text


def test_the_two_empty_messages_are_actually_different(taste):
    a = taste(_payload(actors_status='no_cast')).inner_text(
        '#taste-dna-actors-empty')
    b = taste(_payload(actors_status='no_qualifying_actors')).inner_text(
        '#taste-dna-actors-empty')
    assert a != b


def test_unknown_status_stays_silent(taste):
    page = taste(_payload(actors_status='something_else'))
    assert not page.is_visible('#taste-dna-actors-empty-block')
    assert not page.is_visible('#taste-dna-actors-block')


def test_absent_actor_fields_stay_silent(taste):
    page = taste(_payload())
    assert not page.is_visible('#taste-dna-actors-block')
    assert not page.is_visible('#taste-dna-actors-empty-block')


def test_cold_start_hides_everything(taste):
    page = taste({'available': False, 'state': 'cold_start'}, wait=False)
    page.wait_for_selector('#taste-dna-coldstart:not(.hidden)', timeout=5000)
    assert page.is_visible('#taste-dna-section')
    assert not page.is_visible('#taste-dna-actors-block')
    assert not page.is_visible('#taste-dna-actors-empty-block')
    assert page.f14_errors == []


# ── regression: existing dimensions still render ────────────────────────────

def test_existing_sections_still_render(taste):
    page = taste(_payload(
        avoid_genres=[{'name': 'Horror', 'strength': 'low'}],
        media_preference={'movie': 'high', 'tv': 'moderate'},
        runtime={'min': 105, 'max': 145},
        actors_status='ready',
        top_actors=[{'name': 'Someone', 'strength': 'high',
                     'url': '/actor/1'}]))
    for block in ('taste-dna-genres-block', 'taste-dna-avoid-block',
                  'taste-dna-directors-block', 'taste-dna-eras-block',
                  'taste-dna-media-block', 'taste-dna-runtime-block',
                  'taste-dna-actors-block'):
        assert page.is_visible('#' + block), block
    assert page.inner_text('#taste-dna-basis') == 'Based on 42 titles'
    assert '105' in page.inner_text('#taste-dna-runtime')


def test_actor_chips_match_director_chip_visuals(taste):
    page = taste(_payload(
        top_directors=[{'name': 'Villeneuve', 'strength': 'moderate'}],
        actors_status='ready',
        top_actors=[{'name': 'Murphy', 'strength': 'moderate'}]))
    director = page.eval_on_selector(
        '#taste-dna-directors li', 'e => e.className')
    actor = page.eval_on_selector('#taste-dna-actors li', 'e => e.className')
    assert director == actor


def test_strength_label_is_shown_not_colour_only(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'Murphy', 'strength': 'moderate',
                     'url': '/actor/1'}]))
    assert 'moderate' in page.inner_text('#taste-dna-actors')


def test_no_javascript_errors_anywhere(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'A', 'strength': 'high', 'url': '/actor/1'},
                    {'name': 'B', 'strength': 'low'}]))
    assert page.f14_errors == []


def test_response_is_responsive(taste):
    page = taste(_payload(
        actors_status='ready',
        top_actors=[{'name': 'A', 'strength': 'high', 'url': '/actor/1'},
                    {'name': 'B', 'strength': 'moderate',
                     'url': '/actor/2'}]))
    for width, height in ((390, 844), (768, 1024), (1280, 900)):
        page.set_viewport_size({'width': width, 'height': height})
        box = page.eval_on_selector(
            '#taste-dna-actors-block', 'e => e.getBoundingClientRect().width')
        assert box > 0
        # No horizontal overflow at phone width.
        overflow = page.evaluate(
            'document.documentElement.scrollWidth - '
            'document.documentElement.clientWidth')
        assert overflow <= 1, 'overflow %d at %dpx' % (overflow, width)
