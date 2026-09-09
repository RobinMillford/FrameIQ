/**
 * Where to Watch — availability loader (Feature 03).
 *
 * Fetches /api/media/<type>/<id>/availability and renders provider groups,
 * My Services match, and graceful empty/error states. Never blocks page
 * render; the section hides itself if availability cannot be determined.
 */
(function () {
    'use strict';

    var IMG_BASE = 'https://image.tmdb.org/t/p/w92';

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = String(s == null ? '' : s);
        return d.innerHTML;
    }

    function providerChip(p, extraClass) {
        var label = esc(p.name);
        var inner;
        if (p.logo) {
            // onerror falls back to the name chip — no permanent skeleton.
            inner = '<img src="' + esc(IMG_BASE + p.logo) + '" alt="' + label +
                    '" class="wtw-logo" style="height:1.5rem;width:auto;max-width:5rem;border-radius:.25rem" ' +
                    'onerror="this.outerHTML=\'<span class=&quot;wtw-fallback&quot;>' + label + '</span>\'">';
        } else {
            inner = '<span class="wtw-fallback">' + label + '</span>';
        }
        var cls = 'wtw-chip inline-flex items-center gap-1.5 bg-white/5 border border-white/10 rounded-md px-2 py-1 text-xs ' +
                  (extraClass || '');
        if (p.link) {
            return '<a href="' + esc(p.link) + '" target="_blank" rel="noopener" class="' + cls +
                   ' hover:border-white/25" title="View on ' + label + '">' + inner + '</a>';
        }
        return '<span class="' + cls + '">' + inner + '</span>';
    }

    function renderGroup(name, providers) {
        if (!providers || !providers.length) return '';
        var chips = providers.map(function (p) { return providerChip(p); }).join('');
        return '<div class="flex items-start gap-3">' +
               '<span class="text-xs font-semibold uppercase tracking-wide text-[var(--text-low)] w-14 shrink-0 pt-1">' +
               esc(name) + '</span>' +
               '<div class="flex flex-wrap gap-1.5">' + chips + '</div></div>';
    }

    function renderMatchBanner(data) {
        if (!data.my_services) return '';
        var ms = data.my_services;
        if (ms.available && ms.matches.length) {
            var names = ms.matches.map(function (p) { return esc(p.name); }).join(' · ');
            return '<div class="mt-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 px-3 py-2 text-sm text-emerald-300">' +
                   '✓ Available on your services: ' + names + '</div>';
        }
        if (data.available && data.providers.rent && data.providers.rent.length) {
            var rentNames = data.providers.rent.map(function (p) { return esc(p.name); }).join(' · ');
            return '<div class="mt-3 rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm text-[var(--text-mid)]">' +
                   'Rent from ' + rentNames + '</div>';
        }
        if (data.available) {
            return '<div class="mt-3 rounded-lg bg-white/5 border border-white/10 px-3 py-2 text-sm text-[var(--text-mid)]">' +
                   'Not available on your services</div>';
        }
        return '';
    }

    function hide() {
        var section = document.getElementById('where-to-watch');
        if (section) section.style.display = 'none';
    }

    function render(data) {
        var body = document.getElementById('wtw-body');
        var regionEl = document.getElementById('wtw-region');
        if (!body) return;
        if (regionEl) regionEl.textContent = '(' + data.region + ')';

        var groups =
            renderGroup('Stream', data.providers.stream) +
            renderGroup('Free', data.providers.free) +
            renderGroup('Rent', data.providers.rent) +
            renderGroup('Buy', data.providers.buy);

        if (!groups) {
            body.innerHTML = '<p class="text-sm text-[var(--text-low)]">' +
                             'Availability currently unavailable.</p>';
            return;
        }
        body.innerHTML = groups + renderMatchBanner(data);
    }

    function init() {
        var section = document.getElementById('where-to-watch');
        if (!section) return;
        var type = section.getAttribute('data-media-type');
        var id = section.getAttribute('data-media-id');
        if (!type || !id) return;

        fetch('/api/media/' + encodeURIComponent(type) + '/' + encodeURIComponent(id) + '/availability')
            .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
            .then(function (data) {
                if (!data || !data.providers) throw new Error('bad payload');
                render(data);
            })
            .catch(hide); // graceful: hide the section, never break the page
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
