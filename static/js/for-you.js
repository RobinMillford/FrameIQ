/**
 * For You rail (Feature #6/#7 Phase 5).
 *
 * Sole responsibility: fill the server-rendered `#for-you-rail` placeholder
 * (templates/partials/for_you_rail.html) from the canonical
 * GET /api/for-you endpoint and render cards with the site's existing
 * rail-card markup. All recommendation logic lives in api/for_you.py —
 * this module does no scoring, filtering, or TMDb calls.
 *
 * Behavior contract (tested by tests/test_for_you_homepage.py):
 *  - runs only for authenticated users (window.__IS_AUTH__); the placeholder
 *    is not rendered for anonymous users at all
 *  - exactly ONE request per page load; no polling, no scroll refetch
 *  - personalized=false (cold start), empty items, any HTTP/parse error →
 *    the whole For You <section> is removed; existing rails stay intact
 *  - reason text is rendered verbatim from item.reason.text — never
 *    constructed client-side; no scoring internals are displayed
 *  - cards reuse the exact rail_card.html markup (movie → /movie/{id},
 *    tv → /tv/{id}); posterless items use the no-poster placeholder
 */
(function () {
    'use strict';

    // The placeholder IS the rail container; the section shell is the
    // homepage rails-loop wrapper around it.
    var container = document.querySelector('[data-for-you-placeholder]');
    if (!container || window.__IS_AUTH__ !== true) return;

    var section = container.closest('section');
    if (!section) return;

    function hide() {
        if (section && section.parentNode) {
            section.parentNode.removeChild(section);
        }
    }

    function posterTag(src, title) {
        var img = document.createElement('img');
        img.alt = title;
        img.loading = 'lazy';
        if (src) {
            img.src = src.charAt(0) === '/'
                ? 'https://image.tmdb.org/t/p/w342' + src : src;
            img.onerror = function () {
                img.onerror = null;
                img.src = '/static/images/no-poster.svg';
            };
        } else {
            img.src = '/static/images/no-poster.svg';
        }
        return img;
    }

    function card(item) {
        var isMovie = item.media_type === 'movie';
        var wrap = document.createElement('div');
        wrap.className = 'shrink-0 snap-start w-[148px] sm:w-[160px]';

        var a = document.createElement('a');
        a.href = isMovie ? '/movie/' + item.tmdb_id : '/tv/' + item.tmdb_id;
        a.className =
            'rail-card poster-glow group block shrink-0 snap-start w-full';

        var box = document.createElement('div');
        box.className =
            'poster-box relative overflow-hidden rounded-xl ' +
            'bg-[var(--bg-surface)] ring-1 ring-[var(--line)] aspect-[2/3]';
        box.appendChild(posterTag(item.poster_path, item.title));

        var title = document.createElement('p');
        title.className = 'mt-2 text-[13px] font-medium ' +
            'text-[var(--text-hi)] truncate group-hover:text-[var(--accent)]' +
            ' transition-colors';
        title.textContent = item.title;

        var reason = document.createElement('p');
        reason.className = 'font-slate text-[10px] ' +
            'text-[var(--text-low)]';
        reason.textContent = item.reason && item.reason.text
            ? item.reason.text : '';

        a.appendChild(box);
        a.appendChild(title);
        a.appendChild(reason);
        wrap.appendChild(a);
        return wrap;
    }

    function render(data) {
        if (!data || data.personalized !== true || !Array.isArray(data.items)
                || data.items.length === 0) {
            hide();
            return;
        }

        // In-rail dedupe: a repeated (media_type, tmdb_id) must never
        // produce two cards (server already dedupes; this is the guard).
        var seen = Object.create(null);
        var frag = document.createDocumentFragment();
        for (var i = 0; i < data.items.length; i++) {
            var it = data.items[i];
            if (!it || typeof it.tmdb_id !== 'number'
                    || (it.media_type !== 'movie'
                        && it.media_type !== 'tv')) {
                continue;
            }
            var key = it.media_type + ':' + it.tmdb_id;
            if (seen[key]) continue;
            seen[key] = true;
            frag.appendChild(card(it));
        }

        container.textContent = '';
        container.appendChild(frag);
        if (window.lucide) lucide.createIcons();
    }

    fetch('/api/for-you', {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
    }).then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    }).then(render).catch(hide);   // any failure → silently hide the rail
})();
