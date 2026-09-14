/**
 * For You rail (Feature #6/#7 Phases 5 + 7).
 *
 * Responsibilities:
 *  1. Fill the server-rendered `[data-for-you-placeholder]` from the canonical
 *     GET /api/for-you and render cards with the site's existing rail-card
 *     markup (Phase 5). All recommendation logic lives in api/for_you.py —
 *     this module does no scoring, filtering, or TMDb calls.
 *  2. Collect passive impression + click feedback for the rendered cards and
 *     deliver them to the existing POST /api/rec/feedback endpoint (Phase 7).
 *     Pure telemetry: invisible to the user, never blocks navigation, never
 *     retried aggressively.
 *
 * Behavior contract (tested by tests/test_for_you_homepage.py and
 * tests/test_for_you_feedback.py):
 *  - runs only for authenticated users (window.__IS_AUTH__); the placeholder
 *    is not rendered for anonymous users at all
 *  - exactly ONE /api/for-you request per page load; no polling, no scroll
 *    refetch
 *  - personalized=false (cold start), empty items, any HTTP/parse error →
 *    the whole For You <section> is removed; existing rails stay intact and
 *    NO feedback is ever emitted for a cold-start user
 *  - reason text is rendered verbatim from item.reason.text — never
 *    constructed client-side; no scoring internals are displayed
 *  - cards reuse the exact rail_card.html markup (movie → /movie/{id},
 *    tv → /tv/{id}); posterless items use the no-poster placeholder
 *
 * Feedback specifics:
 *  - impression = card actually became ≥50% visible (one IntersectionObserver,
 *    unobserve after firing); at most ONE impression per card per page view
 *    (client Set keyed media_type:tmdb_id; the server's per-day partial
 *    unique index is the final protection)
 *  - position is the card's rendered (1-based) position in the deterministic
 *    For You result — assigned at render time, never re-derived from the DOM
 *  - click = delegated listener on the rail; enqueues event=click and flushes
 *    with fetch keepalive so the request survives navigation. Navigation is
 *    NEVER delayed or prevented.
 *  - CSRF: fetch POSTs go through the site's base.html fetch patch (which
 *    injects X-CSRFToken on mutating requests); we also set the header
 *    explicitly from the csrf-token meta tag, matching quick-log.js.
 *    navigator.sendBeacon is deliberately NOT used: it bypasses that patch
 *    and cannot carry the CSRF header, and inventing a CSRF bypass is worse
 *    than losing beacon support (spec: fetch keepalive instead).
 *  - batching: in-memory queue, flushed immediately at FLUSH_SIZE events,
 *    otherwise one short debounced flush (setTimeout) — no perpetual timer.
 *    A page with 14 cards produces ≤14 impressions in 1–2 small requests,
 *    far below the endpoint's 120/min limit and 100-event batch cap.
 *  - failures (4xx/5xx/timeout/malformed) are silently dropped; telemetry
 *    never shows UI, never retries, never interferes with navigation.
 */
(function () {
    'use strict';

    // The placeholder IS the rail container; the section shell is the
    // homepage rails-loop wrapper around it.
    var container = document.querySelector('[data-for-you-placeholder]');
    if (!container || window.__IS_AUTH__ !== true) return;

    var section = container.closest('section');
    if (!section) return;

    // ── Feedback constants (Phase 7) ─────────────────────────────────────
    var SURFACE = 'home_for_you';
    var IMPRESSION_THRESHOLD = 0.5;   // ≥50% of the card visible counts
    var FLUSH_SIZE = 10;              // flush immediately at this queue depth
    var FLUSH_DELAY_MS = 2000;        // otherwise one short debounced flush

    var pendingFeedback = [];         // in-memory queue (no persistence)
    var seenImpressions = Object.create(null);  // media_type:tmdb_id → true
    var flushTimer = null;
    var observer = null;

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    // Fire-and-forget telemetry POST. keepalive lets page-exit flushes
    // survive navigation. Any failure is silently discarded — no retry.
    function flushFeedback(useKeepalive) {
        if (flushTimer) {
            clearTimeout(flushTimer);
            flushTimer = null;
        }
        if (!pendingFeedback.length) return;
        var batch = pendingFeedback.splice(0, pendingFeedback.length);
        fetch('/api/rec/feedback', {
            method: 'POST',
            credentials: 'same-origin',
            keepalive: !!useKeepalive,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify({ events: batch })
        }).catch(function () { /* telemetry is best-effort; drop */ });
    }

    function queueFeedback(event) {
        pendingFeedback.push(event);
        if (pendingFeedback.length >= FLUSH_SIZE) {
            flushFeedback(false);
        } else if (!flushTimer) {
            flushTimer = setTimeout(function () {
                flushTimer = null;
                flushFeedback(false);
            }, FLUSH_DELAY_MS);
        }
    }

    // Page-exit flush: keepalive fetch (CSRF-compatible), never blocks.
    function flushOnPageExit() {
        flushFeedback(true);
    }

    function buildEvent(item, eventName, position) {
        var ev = {
            media_id: item.tmdb_id,
            media_type: item.media_type,
            surface: SURFACE,
            event: eventName,
            position: position
        };
        if (item.source) ev.source = item.source;
        if (item.reason && item.reason.kind) {
            ev.reason_kind = item.reason.kind;
        }
        return ev;
    }

    function initImpressions() {
        var cards = container.querySelectorAll('[data-rec-media-id]');
        if (!cards.length) return;
        if (!('IntersectionObserver' in window)) {
            // Very old browsers: count rendered cards once, best-effort.
            for (var k = 0; k < cards.length; k++) recordImpression(cards[k]);
            return;
        }
        observer = new IntersectionObserver(function (entries) {
            for (var i = 0; i < entries.length; i++) {
                var entry = entries[i];
                if (entry.isIntersecting
                        && entry.intersectionRatio >= IMPRESSION_THRESHOLD) {
                    recordImpression(entry.target);
                    observer.unobserve(entry.target);
                }
            }
        }, { threshold: IMPRESSION_THRESHOLD });
        for (var j = 0; j < cards.length; j++) observer.observe(cards[j]);
    }

    function recordImpression(card) {
        var key = card.getAttribute('data-rec-media-type') + ':'
            + card.getAttribute('data-rec-media-id');
        if (seenImpressions[key]) return;   // one impression per page view
        seenImpressions[key] = true;
        queueFeedback(buildEvent({
            tmdb_id: parseInt(card.getAttribute('data-rec-media-id'), 10),
            media_type: card.getAttribute('data-rec-media-type'),
            source: card.getAttribute('data-rec-source') || null,
            reason: { kind: card.getAttribute('data-rec-reason-kind')
                || null }
        }, 'impression', parseInt(card.getAttribute('data-rec-position'), 10)));
    }

    function initClicks() {
        // Delegated listener: no per-card handlers; navigation always wins.
        container.addEventListener('click', function (e) {
            var card = e.target.closest('[data-rec-media-id]');
            if (!card) return;
            queueFeedback(buildEvent({
                tmdb_id: parseInt(card.getAttribute('data-rec-media-id'), 10),
                media_type: card.getAttribute('data-rec-media-type'),
                source: card.getAttribute('data-rec-source') || null,
                reason: { kind: card.getAttribute('data-rec-reason-kind')
                    || null }
            }, 'click', parseInt(card.getAttribute('data-rec-position'), 10)));
            // Clicks must reach the server even if the page unloads.
            flushOnPageExit();
        });
    }

    function disconnectTelemetry() {
        if (observer) {
            observer.disconnect();
            observer = null;
        }
    }

    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'hidden') flushOnPageExit();
    });
    window.addEventListener('pagehide', flushOnPageExit);

    // ── Rendering (Phase 5, unchanged responsibilities) ──────────────────

    function hide() {
        disconnectTelemetry();
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

    function card(item, position) {
        var isMovie = item.media_type === 'movie';
        var wrap = document.createElement('div');
        wrap.className = 'shrink-0 snap-start w-[148px] sm:w-[160px]';

        // Minimal telemetry metadata only (no scores, no reason objects).
        wrap.setAttribute('data-rec-media-id', item.tmdb_id);
        wrap.setAttribute('data-rec-media-type', item.media_type);
        wrap.setAttribute('data-rec-position', position);
        if (item.source) {
            wrap.setAttribute('data-rec-source', item.source);
        }
        if (item.reason && item.reason.kind) {
            wrap.setAttribute('data-rec-reason-kind', item.reason.kind);
        }

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
        var position = 0;   // rendered position, 1-based
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
            position += 1;
            frag.appendChild(card(it, position));
        }

        container.textContent = '';
        container.appendChild(frag);
        if (window.lucide) lucide.createIcons();

        initImpressions();
        initClicks();
    }

    fetch('/api/for-you', {
        credentials: 'same-origin',
        headers: { 'Accept': 'application/json' }
    }).then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
    }).then(render).catch(hide);   // any failure → silently hide the rail
})();
