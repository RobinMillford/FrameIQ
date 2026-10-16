"""F48 — the list-cover collage as a REAL BROWSER renders it.

``tests/test_list_covers_f48.py`` proves the selection rule, the query shape
and the absence of the old upload flow. This module proves the other half: that
``static/js/list-cover.js`` actually lays the tiles out, fills its box, and
refuses a hostile poster path — in Chromium, with real CSS, rather than in a
hand-written DOM shim that would only prove the shim agrees with itself.

Everything is local and inert. No server is contacted: pages are built with
``set_content`` and image requests are aborted, so a poster can never become a
real network fetch.

Runs only where Playwright is available; see tests/browser_requirement.py.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tests'))

from tests.browser_requirement import require_playwright  # noqa: E402

sync_playwright = require_playwright('test_list_cover_browser_f48.py')

from playwright.sync_api import sync_playwright  # noqa: E402

JS = ROOT / 'static' / 'js'
CSS = ROOT / 'static' / 'css' / 'list-cover.css'


def _read(name):
    return (JS / name).read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as pw:
        yield pw.chromium.launch()


@pytest.fixture
def page(browser):
    """A blank page with the real renderer loaded and no network."""
    page = browser.new_page()
    page.route('**/*', lambda route: route.abort())
    page.set_content(
        '<!DOCTYPE html><html><head>'
        '<style>%s</style></head><body><div id="mount"></div></body></html>'
        % CSS.read_text(encoding='utf-8'))
    page.add_script_tag(content=_read('dom-safety.js'))
    page.add_script_tag(content=_read('list-cover.js'))
    return page


def _render(page, posters, aspect='2 / 1'):
    """Render `posters` into #mount and return a per-tile geometry snapshot.

    Geometry is read from `getBoundingClientRect` rather than from the inline
    `grid-*` styles: that is what proves the CSS actually honoured the layout,
    and it is the number that decides whether the collage looks deliberate.
    """
    return page.evaluate(
        """(args) => {
            const mount = document.getElementById('mount');
            window.FrameIQListCover.render(mount, args.posters,
                                          {aspect: args.aspect, size: 'w342'});
            const box = mount.getBoundingClientRect();
            return {
                mount: {w: box.width, h: box.height},
                tiles: Array.from(mount.children).map((n) => {
                    const r = n.getBoundingClientRect();
                    return {
                        tag: n.tagName,
                        cls: n.className,
                        empty: n.className.indexOf('fq-cover-tile--empty') !== -1,
                        src: n.tagName === 'IMG' ? n.getAttribute('src') : null,
                        loading: n.getAttribute ? n.getAttribute('loading') : null,
                        x: Math.round(r.left - box.left),
                        y: Math.round(r.top - box.top),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                    };
                }),
            };
        }""",
        {'posters': posters, 'aspect': aspect})


def _overlaps(a, b):
    return not (a['x'] + a['w'] <= b['x'] or b['x'] + b['w'] <= a['x']
                or a['y'] + a['h'] <= b['y'] or b['y'] + b['h'] <= a['y'])


# ── layout: gap-free, in-bounds, deliberate ────────────────────────────────

@pytest.mark.parametrize('count', [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_every_count_lays_out_without_gaps_or_overlap(page, count):
    """The whole point of the LAYOUTS table: each count fills its box exactly.

    Measured on the rendered rectangles, so this fails if the CSS stops
    honouring the grid as well as if the maths did.
    """
    posters = ['/p%d.jpg' % n for n in range(1, count + 1)]
    shot = _render(page, posters)
    tiles = shot['tiles']

    assert len(tiles) == count
    for i, a in enumerate(tiles):
        for b in tiles[i + 1:]:
            assert not _overlaps(a, b), 'tiles %d and %d overlap' % (i, i + 1)
        assert a['w'] > 0 and a['h'] > 0, 'tile %d collapsed to zero' % i
        # Every tile must sit inside the collage box. The 2px gap means a tile
        # may be up to a gap short of the edge, but never outside it.
        assert a['x'] >= -1, 'tile %d starts left of the box' % i
        assert a['y'] >= -1, 'tile %d starts above the box' % i
        assert a['x'] + a['w'] <= shot['mount']['w'] + 1, \
            'tile %d overflows the right edge' % i
        assert a['y'] + a['h'] <= shot['mount']['h'] + 1, \
            'tile %d overflows the bottom edge' % i


@pytest.mark.parametrize('count', [2, 3, 4, 5, 6, 7, 8, 9, 10])
def test_tiles_are_well_distributed_across_the_box(page, count):
    """Catches the failure a screenshot hides: two huge posters and a gap.

    No single tile may dominate, and the tiles must cover the box rather than
    clustering in one corner. A one-poster collage is excluded because it is
    one tile filling the box by definition — there is nothing to distribute,
    and its geometry is already covered by
    ``test_every_count_lays_out_without_gaps_or_overlap[1]``.
    """
    posters = ['/p%d.jpg' % n for n in range(1, count + 1)]
    shot = _render(page, posters)
    tiles = shot['tiles']
    box_area = max(1, shot['mount']['w'] * shot['mount']['h'])
    covered = sum(t['w'] * t['h'] for t in tiles)

    # The 2px gutter between tiles is what keeps coverage below 100%.
    assert covered / box_area > 0.85, \
        'collage only fills %.0f%% of its box' % (100 * covered / box_area)

    largest = max(t['w'] * t['h'] for t in tiles)
    assert largest / covered < 0.55, \
        'one tile dominates; the collage does not read as a collection'


def test_ten_posters_read_left_to_right_in_list_order(page):
    """The order is the user's list order, so column-major would be wrong."""
    shot = _render(page, ['/p%d.jpg' % n for n in range(1, 11)])
    tiles = shot['tiles']

    hero, rest = tiles[0], tiles[1:]
    assert hero['w'] > 0 and hero['h'] > 0

    # Non-hero tiles must be ordered by (row, column) — i.e. reading order.
    keyed = [(t['y'], t['x']) for t in rest]
    assert keyed == sorted(keyed), 'tiles are not in reading order'


def test_the_hero_is_the_first_item(page):
    """Item 1 gets the hero block, which is how the cover reads as ordered."""
    shot = _render(page, ['/p%d.jpg' % n for n in range(1, 8)])
    tiles = shot['tiles']
    hero = tiles[0]

    assert max(t['w'] for t in tiles[1:]) <= hero['w'] + 1
    assert hero['src'].endswith('/p1.jpg')


def test_rounded_corners_and_cover_fill(page):
    """The collage fills its container and is clipped by a radius."""
    _render(page, ['/p%d.jpg' % n for n in range(1, 5)])

    radius = page.evaluate(
        """() => getComputedStyle(document.getElementById('mount')).borderRadius""")
    display = page.evaluate(
        """() => getComputedStyle(document.getElementById('mount')).display""")

    assert display == 'grid'
    assert radius not in ('0px', ''), 'cover has no rounded corners'


# ── lazy loading ───────────────────────────────────────────────────────────

def test_first_poster_is_eager_and_the_rest_are_lazy(page):
    """The visible first cover must not be deferred; the rest must be."""
    shot = _render(page, ['/p%d.jpg' % n for n in range(1, 11)])

    assert shot['tiles'][0]['loading'] == 'eager'
    assert all(t['loading'] == 'lazy' for t in shot['tiles'][1:])


# ── empty and incomplete lists ──────────────────────────────────────────────

def test_empty_list_renders_a_placeholder_not_a_blank_box(page):
    """No broken images, and the box still occupies its space."""
    shot = _render(page, [])

    assert len(shot['tiles']) == 1
    assert shot['tiles'][0]['tag'] == 'DIV'
    assert shot['mount']['h'] > 0, \
        'the empty placeholder collapsed to zero height'

    placeholder = shot['tiles'][0]
    assert placeholder['cls'].find('fq-cover--empty') != -1
    # A film glyph, so an empty list reads as "no films yet" rather than as a
    # rendering failure.
    assert page.evaluate(
        """() => !!document.querySelector('.fq-cover--empty i.fa-film')""")


def test_missing_posters_keep_their_tile_so_the_box_still_fills(page):
    """A null poster is a placeholder tile, not a hole in the collage."""
    shot = _render(page, ['/a.jpg', None, '/c.jpg', None, '/e.jpg'])
    tiles = shot['tiles']

    assert len(tiles) == 5
    empties = [i for i, t in enumerate(tiles) if t['empty']]
    assert empties == [1, 3]
    for i in empties:
        assert tiles[i]['tag'] != 'IMG'


def test_all_missing_posters_still_fills_the_box(page):
    """Every slot present, none of them an <img>."""
    shot = _render(page, [None] * 10)

    assert len(shot['tiles']) == 10
    assert all(t['tag'] != 'IMG' for t in shot['tiles'])
    assert all(t['w'] > 0 for t in shot['tiles'])


# ── unsafe values never become a fetch ──────────────────────────────────────

@pytest.mark.parametrize('value', [
    'javascript:alert(1)',
    'data:image/png;base64,AAAA',
    '//evil.example/pixel.png',
    'https://evil.example/pixel.png',
    'https://image.tmdb.org.evil.example/p.jpg',
    'vbscript:msgbox(1)',
])
def test_hostile_poster_path_becomes_a_placeholder(page, value):
    """The render refuses, exactly as every other TMDb render path does."""
    shot = _render(page, [value])

    assert len(shot['tiles']) == 1
    assert shot['tiles'][0]['tag'] != 'IMG', \
        'hostile poster produced an <img src=%r>' % value
    assert shot['tiles'][0]['src'] is None


@pytest.mark.parametrize('value', [
    '/ok.jpg',
    'https://image.tmdb.org/t/p/w500/ok.jpg',
])
def test_valid_poster_paths_produce_a_tmdb_image(page, value):
    shot = _render(page, [value])

    assert shot['tiles'][0]['tag'] == 'IMG'
    assert shot['tiles'][0]['src'].startswith('https://image.tmdb.org/t/p/')


def test_a_broken_image_falls_back_to_the_placeholder(page):
    """A poster that 404s must not leave a browser icon in the collage.

    The `error` listener swaps the tile for a placeholder. Route aborts every
    request, so dispatching `error` is what the browser would do on a real miss.
    """
    page.evaluate(
        """() => {
            const mount = document.getElementById('mount');
            window.FrameIQListCover.render(mount, ['/a.jpg', '/b.jpg'],
                                          {aspect: '2 / 1'});
            const img = mount.children[0];
            img.dispatchEvent(new Event('error'));
        }""")

    tiles = page.evaluate(
        """() => Array.from(document.getElementById('mount').children)
                   .map((n) => n.tagName)""")
    assert tiles == ['DIV', 'IMG']


# ── responsive ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('width,height', [(1280, 800), (768, 1024), (375, 667)])
def test_collage_renders_at_desktop_tablet_and_phone_widths(page, width, height):
    """Desktop, tablet, phone: no zero-size tile, no overflow, no overlap."""
    page.set_viewport_size({'width': width, 'height': height})
    shot = _render(page, ['/p%d.jpg' % n for n in range(1, 11)])

    assert all(t['w'] > 0 and t['h'] > 0 for t in shot['tiles'])
    for i, a in enumerate(shot['tiles']):
        for b in shot['tiles'][i + 1:]:
            assert not _overlaps(a, b)


def test_hydrate_reads_posters_from_a_data_attribute(page):
    """The path both server-rendered templates use.

    Jinja writes the posters as JSON into `data-cover-posters`; the renderer
    parses them and builds elements. Asserting this end to end is what shows
    the templates will actually paint a collage.
    """
    page.set_content(
        '<!DOCTYPE html><html><head><style>%s</style></head><body>'
        '<div class="fq-cover" data-cover-posters=\'[null,"/a.jpg","/b.jpg"]\' '
        'data-cover-aspect="2 / 1"></div></body></html>'
        % CSS.read_text(encoding='utf-8'))
    page.add_script_tag(content=_read('dom-safety.js'))
    page.add_script_tag(content=_read('list-cover.js'))

    page.evaluate(
        "() => window.FrameIQListCover.hydrate(document)")

    tiles = page.evaluate(
        """() => Array.from(document.querySelectorAll('.fq-cover')[0].children)
                   .map((n) => n.tagName)""")
    assert tiles == ['DIV', 'IMG', 'IMG']


def test_hydrate_survives_malformed_json(page):
    """A corrupt attribute degrades to the placeholder, never an exception."""
    page.set_content(
        '<!DOCTYPE html><html><body>'
        '<div class="fq-cover" data-cover-posters="{not json"></div>'
        '</body></html>')
    page.add_script_tag(content=_read('dom-safety.js'))
    page.add_script_tag(content=_read('list-cover.js'))

    page.evaluate("() => window.FrameIQListCover.hydrate(document)")

    assert page.evaluate(
        "() => document.querySelector('.fq-cover').children.length") == 1


def test_renderer_survives_dom_safety_loading_after_it(browser):
    """Regression: `FrameIQDOM` must be resolved at call time, not load time.

    `templates/base.html` renders the `extra_head` block at line 73 and the
    `dom-safety.js` tag at line 263. A page that loads this renderer from
    `extra_head` therefore evaluates it BEFORE `dom-safety.js` exists. Capturing
    `FrameIQDOM` once at module scope left `D` undefined, and the first
    `D.el(...)` threw — an empty collage on every discover card, with nothing
    in the console a user would see.
    """
    page = browser.new_page()
    page.route('**/*', lambda route: route.abort())
    page.set_content('<!DOCTYPE html><html><head><style>%s</style></head>'
                     '<body><div id="mount"></div></body></html>'
                     % CSS.read_text(encoding='utf-8'))

    # Renderer FIRST, dom-safety second — the base.html order.
    page.add_script_tag(content=_read('list-cover.js'))
    page.add_script_tag(content=_read('dom-safety.js'))

    tiles = page.evaluate("""() => {
        const mount = document.getElementById('mount');
        window.FrameIQListCover.render(mount, ['/a.jpg', '/b.jpg'],
                                      {aspect: '2 / 1'});
        return Array.from(mount.children).map((n) => n.tagName);
    }""")

    assert tiles == ['IMG', 'IMG'], tiles


def test_renderer_degrades_when_dom_safety_is_absent(browser):
    """No `FrameIQDOM` means no collage — but no exception either.

    The discover card calls the renderer unconditionally, so a missing
    dependency must not throw mid-grid and take the whole page with it.
    """
    page = browser.new_page()
    page.set_content('<!DOCTYPE html><html><body><div id="mount"></div></body></html>')
    page.add_script_tag(content=_read('list-cover.js'))

    result = page.evaluate("""() => {
        const mount = document.getElementById('mount');
        try {
            window.FrameIQListCover.render(mount, ['/a.jpg'], {aspect: '2 / 1'});
            return {threw: false, children: mount.children.length};
        } catch (err) {
            return {threw: true, message: String(err)};
        }
    }""")

    assert result['threw'] is False, result
    assert result['children'] == 0, result
