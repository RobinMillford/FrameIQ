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

    /**
     * F21 — escape for a QUOTED ATTRIBUTE context.
     *
     * The `esc()` above is the textContent->innerHTML variant, which encodes
     * only `& < >`; a quote survives it and can terminate a double-quoted
     * attribute. Values placed inside quotes use this instead.
     */
    function escAttr(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // F21 — shared DOM-safety helpers (static/js/dom-safety.js). Resolved LAZILY
    // rather than captured at IIFE time: where-to-watch.js is loaded by the
    // detail templates and must not depend on whether the helper happened to
    // execute first.
    function dom() {
        return window.FrameIQDOM || null;
    }

    /**
     * F21 — URL policy for provider links.
     *
     * `p.link` is `data.results.<REGION>.link` taken verbatim from the TMDb
     * watch-providers response. `esc()` does NOT help here: HTML-encoding does
     * not prevent `href="javascript:..."` from being followed. The scheme must
     * be validated independently, so the link is parsed and only http/https
     * survive. An unsafe or missing link degrades to a non-navigating chip,
     * which is exactly how a provider without a link already renders.
     */
    function safeProviderLink(link) {
        var D = dom();
        return (D && typeof D.safeLinkUrl === 'function') ? D.safeLinkUrl(link) : '';
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
            // F21 — the fallback is applied by a real 'error' listener
            // installed in attachImageFallbacks() after insertion. The previous
            // `onerror="this.outerHTML='...'"` spliced a provider name into a
            // JS string nested inside an HTML attribute, which a name containing
            // a quote could terminate early.
            inner = '<img src="' + escAttr(IMG_BASE + p.logo) + '" alt="' + escAttr(p.name) +
                    '" class="wtw-logo" style="height:1.5rem;width:auto;max-width:5rem;border-radius:.25rem"' +
                    ' data-wtw-logo="' + escAttr(p.name) + '">';
        } else {
            inner = '<span class="wtw-fallback">' + label + '</span>';
        }
        var cls = 'wtw-chip inline-flex items-center gap-1.5 bg-white/5 border border-white/10 rounded-md px-2 py-1 text-xs ' +
                  (extraClass || '');
        var href = safeProviderLink(p.link);
        if (href) {
            return '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer" class="' + cls +
                   ' hover:border-white/25" title="View on ' + label + '">' + inner + '</a>';
        }
        return '<span class="' + cls + '">' + inner + '</span>';
    }

    /**
     * F21 — install logo-fallback behaviour with event listeners rather than
     * inline attributes. Delegated on the section so it survives every
     * subsequent re-render.
     */
    function attachImageFallbacks(root) {
        var images = (root || document).querySelectorAll('img[data-wtw-logo]:not([data-wtw-bound])');
        Array.prototype.forEach.call(images, function (img) {
            img.setAttribute('data-wtw-bound', '1');
            img.addEventListener('error', function () {
                var span = document.createElement('span');
                span.className = 'wtw-fallback';
                span.textContent = img.getAttribute('data-wtw-logo') || '';
                if (img.parentNode) img.parentNode.replaceChild(span, img);
            });
        });
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
        attachImageFallbacks(body);
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
