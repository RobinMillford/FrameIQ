/**
 * Where to Watch — availability loader (Feature 03 + Part A fix).
 *
 * Explicit FINITE state machine. The spinner is always removed once the
 * request completes, fails, or errors — never left spinning:
 *
 *   LOADING        "Checking availability…"
 *   AVAILABLE      stream/free/rent/buy provider groups (+ My Services match)
 *   NO_AVAILABILITY  API succeeded, but the user's region has no providers
 *   UNKNOWN        request failed / payload unusable (never "not available")
 *
 * One request → one finite result. No polling, no retries, no timers.
 * The URL is built from the section anchor (/movie/<id> or /tv/<id>) so the
 * loader works even if media_type/media_id context vars are unset.
 */
(function () {
    'use strict';

    var IMG_BASE = 'https://image.tmdb.org/t/p/w92';

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = String(s == null ? '' : s);
        return d.innerHTML;
    }

    /* ── State renderers ───────────────────────────────────────────────── */

    function stateLoading() {
        var body = document.getElementById('wtw-body');
        if (body) {
            body.innerHTML =
                '<p id="wtw-loading" class="text-sm text-[var(--text-low)] flex items-center gap-2">' +
                '<span class="inline-block w-3.5 h-3.5 border-2 border-[var(--text-low)] border-t-transparent rounded-full animate-spin"></span>' +
                'Checking availability…</p>';
        }
    }

    function stateNoAvailability(region) {
        var body = document.getElementById('wtw-body');
        if (!body) return;
        body.innerHTML =
            '<p class="text-sm text-[var(--text-mid)]">No streaming availability found in ' +
            esc(region) + '.</p>';
    }

    function stateUnknown() {
        var body = document.getElementById('wtw-body');
        if (!body) return;
        body.innerHTML =
            '<p class="text-sm text-[var(--text-mid)]">Streaming availability is currently unavailable.</p>';
    }

    function hide() {
        var section = document.getElementById('where-to-watch');
        if (section) section.style.display = 'none';
    }

    /* ── Available rendering ───────────────────────────────────────────── */

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

    function renderAvailable(data) {
        var body = document.getElementById('wtw-body');
        var regionEl = document.getElementById('wtw-region');
        if (regionEl && data.region) regionEl.textContent = '(' + data.region + ')';

        var groups =
            renderGroup('Stream', data.providers.stream) +
            renderGroup('Free', data.providers.free) +
            renderGroup('Rent', data.providers.rent) +
            renderGroup('Buy', data.providers.buy);

        body.innerHTML = groups + renderMatchBanner(data);
    }

    /* ── Dispatch: map a response/error to exactly one terminal state ──── */

    function terminalState(data) {
        var body = document.getElementById('wtw-body');
        if (body) body.innerHTML = '';

        if (!data || !data.providers || !data.region) {
            stateUnknown();                       // failed / unusable payload
        } else if (data.status === 'unknown') {
            stateUnknown();                       // upstream explicitly unknown
        } else if (!data.available) {
            stateNoAvailability(data.region);     // succeeded, region has none
        } else {
            renderAvailable(data);                // genuine providers
        }
    }

    /* ── Init: one bounded fetch, one finite result ────────────────────── */

    function init() {
        var section = document.getElementById('where-to-watch');
        if (!section) return;

        var type = section.getAttribute('data-media-type') || '';
        var id = section.getAttribute('data-media-id') || '';
        var url = null;

        if (type && id) {
            url = '/api/media/' + encodeURIComponent(type) + '/' +
                  encodeURIComponent(id) + '/availability';
        } else {
            // Robust fallback: parse the section's location anchor instead of
            // depending on context variables.
            var m = window.location.pathname.match(/^\/(movie|tv)\/(\d+)/);
            if (m) {
                url = '/api/media/' + m[1] + '/' + m[2] + '/availability';
            }
        }
        if (!url) return;  // not a title page; section stays hidden (no spinner)

        fetch(url)
            .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
            .then(terminalState)
            .catch(terminalState.bind(null, null));  // network/HTTP failure → UNKNOWN
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
