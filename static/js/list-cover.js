/**
 * Automatic list-cover collage (Feature F48).
 *
 * Replaces the broken "Cover" upload on lists. That control put a `data:` URL
 * built by `FileReader.readAsDataURL` into a 500-character column, and since
 * F39 the write path refuses `data:` anyway, so it could never store an image.
 * Instead of building uploads, a cover is derived from the list's own first ten
 * items — see `utils/list_covers.py` for the selection and ordering rule, and
 * why the `id` tiebreak exists.
 *
 * URLs go through `FrameIQDOM.tmdbImageUrl`, the SAME helper every other TMDb
 * render path uses (chrome.js, more-like-this.js, review-manager.js). It is
 * not reimplemented here: it is what makes a hostile `poster_path` — a
 * `javascript:` or `data:` value in the database — resolve to the local
 * placeholder instead of a fetch. Elements are built with `FrameIQDOM.el`,
 * which has no `html` option, so a poster path or title cannot become markup.
 *
 * Loaded as a classic script; exposes `window.FrameIQListCover`.
 * Requires `dom-safety.js` to have loaded first.
 */
(function (global) {
    'use strict';

    /**
     * Resolve `FrameIQDOM` at CALL time, never at module-evaluation time.
     *
     * An earlier version captured it once at load:
     *
     *     var D = global.FrameIQDOM;
     *
     * which silently broke on every page whose <head> block loads this file
     * before `dom-safety.js`. `templates/base.html` renders `extra_head` at
     * line 73 and the `dom-safety.js` tag at line 263, so `D` was `undefined`
     * and the first `D.el(...)` threw — leaving every collage an empty box
     * with no error anyone would see.
     *
     * Every consumer of this module runs after the document has parsed past
     * `dom-safety.js` (DOMContentLoaded, or a fetch callback), so resolving
     * lazily makes the load order irrelevant instead of merely satisfied.
     */
    function dom() {
        return global.FrameIQDOM || null;
    }

    /** Matches utils/list_covers.COVER_POSTER_LIMIT. */
    var LIMIT = 10;

    var PLACEHOLDER = '/static/images/no-poster.svg';

    /**
     * Gap-free grid layouts, one per poster count.
     *
     * `cols` x `rows` cells, minus the hero's `heroCols` x `heroRows` block,
     * must equal the number of posters. Every entry below is arithmetically
     * exact, which is the point: a partially-filled final row reads as an
     * accident, and a collage that is one tile short of filling its box is
     * what makes generated artwork look generated.
     *
     *   3 -> 2x2, hero 1x2 .... 1 tall + 2 stacked right
     *   5 -> 4x2, hero 2x2 .... 1 wide + 2x2 right
     *   7 -> 4x3, hero 2x3 .... 1 wide + 2x3 right
     *   9 -> 6x2, hero 2x2 .... 1 wide + 4x2 right
     *  10 -> 5x3, hero 2x3 .... 1 wide + 3x3 right
     *
     * Counts that tile evenly (1,2,4,6,8) use no hero at all: a plain grid of
     * equal posters is the most honest presentation when there is nothing to
     * single out.
     */
    var LAYOUTS = {
        1:  { cols: 1, rows: 1 },
        2:  { cols: 2, rows: 1 },
        3:  { cols: 2, rows: 2, heroCols: 1, heroRows: 2 },
        4:  { cols: 2, rows: 2 },
        5:  { cols: 4, rows: 2, heroCols: 2, heroRows: 2 },
        6:  { cols: 3, rows: 2 },
        7:  { cols: 4, rows: 3, heroCols: 2, heroRows: 3 },
        8:  { cols: 4, rows: 2 },
        9:  { cols: 6, rows: 2, heroCols: 2, heroRows: 2 },
        10: { cols: 5, rows: 3, heroCols: 2, heroRows: 3 },
    };

    /** Fallback for an unexpected count: an evenly-tiled plain grid. */
    function fallbackLayout(count) {
        var cols = count <= 4 ? count : 4;
        return { cols: cols, rows: Math.ceil(count / cols) };
    }

    function layoutFor(count) {
        return LAYOUTS[count] || fallbackLayout(count);
    }

    /**
     * Apply a `grid-column`/`grid-row` declaration.
     *
     * Via the `style` PROPERTY, not `el({style: ...})`: `dom-safety.js` puts
     * `style` in DENY_ATTRIBUTES and `el` throws on it, because a `style`
     * attribute assembled from data is how CSS injection happens. These values
     * are integers this module computed, but the property assignment keeps the
     * guarantee unconditional rather than "safe because I am careful".
     */
    function place(tile, layout, index) {
        tile.style.gridColumn = gridColumn(layout, index);
        tile.style.gridRow = gridRow(layout, index);
        return tile;
    }

    /** Placeholder tile: a poster that is missing, unusable, or failed to load. */
    function placeholderTile(layout, index) {
        return place(dom().el('div', {
            class: 'fq-cover-tile fq-cover-tile--empty',
            'aria-hidden': 'true',
        }, [dom().el('i', { class: 'fas fa-film' })]), layout, index);
    }

    /**
     * Zero-based cell index for a non-hero tile.
     *
     * With a hero, index 0 IS the hero, so the first remaining tile is cell 0.
     * Without one, tile 0 is itself cell 0. Getting this wrong by one shifts
     * every tile after the first, which still fills the grid and so looks fine
     * in a screenshot while reading in the wrong order.
     */
    function cellIndex(layout, index) {
        return index - (layout.heroCols ? 1 : 0);
    }

    /** Columns the hero leaves free for ordinary tiles. Always >= 1. */
    function freeCols(layout) {
        return Math.max(1, layout.cols - (layout.heroCols || 0));
    }

    /**
     * Grid coordinates for the tile at `index`.
     *
     * The hero (index 0) claims a block in the top-left. Every other tile takes
     * a single cell, filled row-major *after* the hero within the columns the
     * hero left free. The `heroCols` offset is what keeps them out of the
     * hero's own columns: cells are numbered across the full grid, but the
     * free region starts at column `heroCols + 1`.
     */
    function gridColumn(layout, index) {
        if (layout.heroCols && index === 0) return 'span ' + layout.heroCols;
        var heroCols = layout.heroCols || 0;
        var cell = cellIndex(layout, index);
        return (heroCols + (cell % freeCols(layout)) + 1) + '';
    }

    function gridRow(layout, index) {
        if (layout.heroCols && index === 0) return 'span ' + layout.heroRows;
        return (Math.floor(cellIndex(layout, index) / freeCols(layout)) + 1) + '';
    }

    function posterTile(path, layout, index, size, lazy) {
        var url = dom().tmdbImageUrl(String(path == null ? '' : path), size, '');
        if (!url) return placeholderTile(layout, index);

        var img = dom().el('img', {
            src: url,
            alt: '',
            class: 'fq-cover-tile fq-cover-tile__img',
            loading: lazy ? 'lazy' : 'eager',
            decoding: 'async',
            draggable: 'false',
        });
        place(img, layout, index);

        // The first poster is the one people actually see, so it is eager and
        // high priority; the rest lazy-load. Attached as a listener rather than
        // an `onerror` attribute, per the dom-safety policy.
        if (!lazy && 'fetchPriority' in img) img.fetchPriority = 'high';
        img.addEventListener('error', function () {
            if (img.parentNode) img.parentNode.replaceChild(placeholderTile(layout, index), img);
        });

        return img;
    }

    /**
     * The whole-cover placeholder: no items at all, or every poster unusable.
     * Deliberately a film glyph on the surface gradient, matching the empty
     * state the discover grid already used before F48.
     */
    function emptyState() {
        return dom().el('div', { class: 'fq-cover fq-cover--empty', 'aria-hidden': 'true' }, [
            dom().el('i', { class: 'fas fa-film fq-cover__glyph' }),
        ]);
    }

    /**
     * Render a collage into `container`.
     *
     * @param container  element to fill (emptied first)
     * @param posters    array of poster paths / absolute URLs, in list order;
     *                   `null` and `''` entries are legal and become tiles
     * @param opts.size  TMDb size segment (default 'w342')
     * @param opts.aspect CSS aspect-ratio string; omit to fill the container
     * @returns the container
     */
    function render(container, posters, opts) {
        if (!container) return container;
        // Without dom-safety.js there is no audited way to build an element or
        // validate a URL, so draw nothing. Returning quietly matters: the
        // discover grid calls this once per card, and a throw here would abort
        // the whole page rather than degrade one cover.
        if (!dom()) return container;
        opts = opts || {};

        dom().clear(container);

        var raw = (posters && typeof posters.length === 'number') ? posters : [];
        var count = Math.min(raw.length, LIMIT);
        var aspect = opts.aspect || '';

        if (count === 0) {
            // The aspect ratio must be applied HERE, before the early return.
            // An earlier version set it after this branch, so an empty list
            // got a placeholder with no height constraint and collapsed to a
            // zero-pixel strip — an empty list rendered as nothing at all
            // rather than as a deliberate "no films yet" panel.
            var empty = emptyState();
            if (aspect) empty.style.setProperty('--fq-cover-aspect', aspect);
            container.appendChild(empty);
            return container;
        }

        var layout = layoutFor(count);
        container.classList.add('fq-cover');
        container.style.setProperty('--fq-cover-cols', layout.cols);
        container.style.setProperty('--fq-cover-rows', layout.rows);
        if (aspect) container.style.setProperty('--fq-cover-aspect', aspect);

        var size = opts.size || 'w342';
        for (var i = 0; i < count; i++) {
            var value = raw[i];
            var tile = (value === null || value === undefined || value === '')
                ? placeholderTile(layout, i)
                : posterTile(value, layout, i, size, i > 0);
            container.appendChild(tile);
        }
        return container;
    }

    /**
     * Render every `[data-cover-posters]` under `root`.
     *
     * The posters arrive as a JSON string in a data attribute, which is how a
     * server-rendered template hands untrusted values to script without an
     * inline-handler or template-string injection surface. The attribute value
     * is escaped by Jinja's `tojson`; it is parsed here and never interpolated
     * into markup.
     */
    function hydrate(root) {
        var scope = root || global.document;
        if (!scope || !scope.querySelectorAll || !dom()) return 0;

        var hosts = scope.querySelectorAll('[data-cover-posters]');
        for (var i = 0; i < hosts.length; i++) {
            var host = hosts[i];
            var posters = [];
            try {
                var parsed = JSON.parse(host.getAttribute('data-cover-posters') || '[]');
                if (Object.prototype.toString.call(parsed) === '[object Array]') posters = parsed;
            } catch (err) {
                posters = [];
            }
            render(host, posters, {
                aspect: host.getAttribute('data-cover-aspect') || '',
                size: host.getAttribute('data-cover-size') || 'w342',
            });
        }
        return hosts.length;
    }

    global.FrameIQListCover = {
        render: render,
        hydrate: hydrate,
        layoutFor: layoutFor,
        emptyState: emptyState,
        LAYOUTS: LAYOUTS,
        LIMIT: LIMIT,
        PLACEHOLDER: PLACEHOLDER,
    };
})(typeof window !== 'undefined' ? window : globalThis);