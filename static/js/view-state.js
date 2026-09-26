/**
 * FrameIQ View State — one shared client-side store for user-scoped
 * viewing state (mirrors api/user_view_state.py).
 *
 * Purpose (cross-surface consistency): when the user marks a movie watched
 * or finishes an episode, every view-state surface on the page — hero
 * chips, rail badges, the TV detail progress line, client-rendered card
 * grids — updates immediately from this single store instead of waiting
 * for a full reload.
 *
 * The store is per-page (no persistence): every fresh server render is the
 * source of truth. Loaded for authenticated users only (anonymous users
 * never receive personalized state).
 *
 * Server-rendered badges keep working unchanged: rails pre-render the
 * Viewed badge per user_viewed_keys, and quick-log flips it directly.
 * This store additionally covers surfaces quick-log cannot reach (hero
 * chips), the incremental TV progress line on the TV detail page, and
 * (Task C) client-rendered surfaces — browse, trending, CineBot cards,
 * For You — via applyToCards() + one batched /api/view-state fetch per
 * page render (never one request per card).
 */
(function () {
    'use strict';

    var viewed = new Set(
        (window.__VIEWED_MOVIE_IDS__ || []).map(String)
    );
    var loggedToday = new Set();             // movie ids with a watch event dated today
    var tvProgress = Object.create(null);   // String(showId) → {watched,aired,percent}
    var stateReady = false;
    var lastSignature = '';
    var observer = null;

    function heroChipExists(tmdbId) {
        // Hero viewed chips only exist for movie slides; the server renders
        // one per viewed pick. When the id is already in the store the chip
        // was rendered server-side too.
        return viewed.has(String(tmdbId));
    }

    function addViewed(tmdbId) {
        viewed.add(String(tmdbId));
    }

    function isViewedMovie(tmdbId) {
        return viewed.has(String(tmdbId));
    }

    /**
     * Task D: distinguishes "viewed" from "a watch event was logged today"
     * so the quick-log action can read "Watched today" / "Log rewatch"
     * instead of a state-blind "Log watched".
     */
    function isLoggedToday(tmdbId) {
        return loggedToday.has(String(tmdbId));
    }

    function markLoggedToday(tmdbId) {
        loggedToday.add(String(tmdbId));
    }

    function tvProgressFor(showId) {
        return tvProgress[String(showId)] || null;
    }

    /* ── Badge builders — EXACT Task B rail_card.html markup ───────────── */

    var VIEWED_CHECK_SVG =
        '<svg class="w-3 h-3" fill="none" stroke="currentColor" ' +
        'viewBox="0 0 24 24"><path stroke-linecap="round" ' +
        'stroke-linejoin="round" stroke-width="2" ' +
        'd="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>';

    function buildViewedBadge() {
        var span = document.createElement('span');
        span.className = 'ql-watched-badge absolute z-10 font-slate ' +
            'text-[10px] bg-green-600 text-white px-1.5 py-0.5 ' +
            'rounded-md flex items-center gap-1 top-2 right-2 hidden';
        span.innerHTML = VIEWED_CHECK_SVG + 'Viewed';
        return span;
    }

    function buildTvProgressBadge(prog) {
        var span = document.createElement('span');
        span.className = 'tv-progress-badge absolute bottom-2 right-2 ' +
            'z-10 font-slate text-[10px] bg-black/70 backdrop-blur ' +
            'text-emerald-300 px-1.5 py-0.5 rounded-md';
        span.setAttribute('title', prog.watched + ' of ' + prog.aired +
            ' aired episodes watched');
        span.textContent = Math.round(prog.percent) + '%';
        return span;
    }

    /**
     * Mount a badge on a client-rendered card. Prefers the existing
     * .poster-box (same geometry as the server-rendered rails); otherwise
     * attaches to the card itself (made position:relative). A card whose
     * top-right corner is taken (trending rank badge, slideshow rating,
     * For You ⋯ menu) gets the badge top-LEFT so nothing collides — same
     * visual language, no card redesign.
     */
    function mountBadge(card, badge, corner) {
        if (corner === 'left') {
            badge.classList.remove('top-2', 'right-2');
            badge.classList.add('top-2', 'left-2');
        }
        var box = card.querySelector('.poster-box');
        if (box) {
            box.appendChild(badge);
            return badge;
        }
        if (getComputedStyle(card).position === 'static') {
            card.style.position = 'relative';
        }
        card.appendChild(badge);
        return badge;
    }

    function cornerFor(card) {
        // top-right is occupied by a rank/rating/menu chip on some surfaces.
        return (card.querySelector(
            '.trending-badge, .show-rating, [data-rec-actions]'))
            ? 'left' : 'right';
    }

    /**
     * Apply current store state to media cards beneath `root` (default:
     * whole document). Cards are located by existing Task B / surface
     * class names, keyed by href (/movie/{id} or /tv/{id}) so the store
     * never needs surface-specific wiring. Safe to run repeatedly: badges
     * are created once and then only shown/hidden/updated.
     *
     * This is the single entry point for client-rendered surfaces (browse,
     * trending, CineBot, For You) and for dynamically inserted cards
     * (load-more, filters, modals).
     */
    function applyToCards(root) {
        if (window.__IS_AUTH__ !== true) return;
        var scope = root && root.querySelectorAll ? root : document;

        var anchors = scope.querySelectorAll(
            '.movie-card a[href^="/movie/"], .media-card a[href^="/movie/"], ' +
            '.trailer-card a[href^="/movie/"], ' +
            '.chat-media-items a[href^="/movie/"]');
        for (var i = 0; i < anchors.length; i++) {
            applyMovieAnchor(anchors[i]);
        }

        var tvAnchors = scope.querySelectorAll(
            '.tv-card a[href^="/tv/"], .show-card a[href^="/tv/"], ' +
            '.media-card a[href^="/tv/"], ' +
            '.chat-media-items a[href^="/tv/"]');
        for (var j = 0; j < tvAnchors.length; j++) {
            applyTvAnchor(tvAnchors[j]);
        }

        // For You / rail-style cards carry the data attributes directly.
        var recs = scope.querySelectorAll('[data-rec-media-id]');
        for (var k = 0; k < recs.length; k++) {
            applyRecCard(recs[k]);
        }

        // Cards with no media-link anchor (trending onclick cards)
        // self-identify with data-card-id/type; cards whose anchor already
        // matches the selectors above are skipped here so no card is ever
        // painted twice (CineBot items are anchor-based via tmdb_link).
        var tagged = scope.querySelectorAll('[data-card-id]');
        for (var n = 0; n < tagged.length; n++) {
            var tc = tagged[n];
            if (tc.querySelector('a[href^="/movie/"], a[href^="/tv/"]')) {
                continue;
            }
            if (tc.getAttribute('data-card-type') === 'movie') {
                applyTaggedMovie(tc);
            } else {
                applyTaggedTv(tc);
            }
        }
    }

    function applyTaggedMovie(card) {
        var id = card.getAttribute('data-card-id');
        if (!id) return;
        var badge = ensureBadge(card, 'viewed');
        badge.classList.toggle('hidden', !viewed.has(id));
    }

    function applyTaggedTv(card) {
        var id = card.getAttribute('data-card-id');
        if (!id) return;
        var prog = tvProgress[id];
        var badge = ensureBadge(card, 'tv');
        if (prog) {
            badge.setAttribute('title', prog.watched + ' of ' + prog.aired +
                ' aired episodes watched');
            badge.textContent = Math.round(prog.percent) + '%';
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    function ensureBadge(card, kind) {
        var cls = kind === 'viewed' ? '.ql-watched-badge'
            : '.tv-progress-badge';
        var badge = card.querySelector(cls);
        if (!badge) {
            badge = kind === 'viewed'
                ? buildViewedBadge() : buildTvProgressBadge(
                    { watched: 0, aired: 0, percent: 0 });
            mountBadge(card, badge, cornerFor(card));
        }
        return badge;
    }

    function applyMovieAnchor(anchor) {
        var match = (anchor.getAttribute('href') || '').match(/\/movie\/(\d+)/);
        if (!match) return;
        // CineBot DOM wraps the card: a[href] > div.chat-media-item, so
        // closest() finds nothing there — fall back to the wrapped card.
        var card = anchor.closest('.movie-card, .media-card, .trailer-card') ||
            anchor.querySelector('.chat-media-item');
        if (!card) return;
        card.setAttribute('data-card-id', match[1]);
        card.setAttribute('data-card-type', 'movie');
        var badge = ensureBadge(card, 'viewed');
        badge.classList.toggle('hidden', !viewed.has(match[1]));
    }

    function applyTvAnchor(anchor) {
        var match = (anchor.getAttribute('href') || '').match(/\/tv\/(\d+)/);
        if (!match) return;
        var card = anchor.closest('.tv-card, .show-card, .media-card') ||
            anchor.querySelector('.chat-media-item');
        if (!card) return;
        card.setAttribute('data-card-id', match[1]);
        card.setAttribute('data-card-type', 'tv');
        var prog = tvProgress[match[1]];
        var badge = ensureBadge(card, 'tv');
        if (prog) {
            badge.setAttribute('title', prog.watched + ' of ' + prog.aired +
                ' aired episodes watched');
            badge.textContent = Math.round(prog.percent) + '%';
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    function applyRecCard(card) {
        var id = card.getAttribute('data-rec-media-id');
        var type = card.getAttribute('data-rec-media-type');
        if (!id) return;
        var badge;
        if (type === 'movie') {
            badge = ensureBadge(card, 'viewed');
            badge.classList.toggle('hidden', !viewed.has(id));
        } else if (type === 'tv') {
            var prog = tvProgress[id];
            badge = ensureBadge(card, 'tv');
            if (prog) {
                badge.setAttribute('title', prog.watched + ' of ' +
                    prog.aired + ' aired episodes watched');
                badge.textContent = Math.round(prog.percent) + '%';
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        }
    }

    /* ── Batched state bootstrap (one request per page render) ─────────── */

    // A short debounce lets every async render hook on a page (trending
    // slider, six genre rows, …) coalesce into ONE /api/view-state call.
    var pendingTimer = null;
    var pending = { movieIds: [], tvIds: [], force: false };
    var lastFlush = Promise.resolve(false);

    /**
     * Ensure the store holds state for the given id sets. Debounced and
     * idempotent: repeated calls with the same id signature never re-fetch
     * (Phase 12 — load more / filter changes only fetch for genuinely
     * new ids).
     */
    function ensureState(opts) {
        if (window.__IS_AUTH__ !== true) return Promise.resolve(false);
        opts = opts || {};
        pending.movieIds = pending.movieIds.concat(opts.movieIds || []);
        pending.tvIds = pending.tvIds.concat(opts.tvIds || []);
        if (opts.force) pending.force = true;
        if (!pendingTimer) {
            pendingTimer = setTimeout(flushEnsure, 120);
        }
        return lastFlush;
    }

    function flushEnsure() {
        pendingTimer = null;
        var movieIds = dedupe(pending.movieIds).sort();
        var tvIds = dedupe(pending.tvIds).sort();
        var force = pending.force;
        pending = { movieIds: [], tvIds: [], force: false };
        var sig = 'm:' + movieIds.join(',') + '|t:' + tvIds.join(',') +
            (force ? '|f' : '');
        if (!force && stateReady && sig === lastSignature) {
            lastFlush = Promise.resolve(true);
            return lastFlush;
        }
        lastSignature = sig;
        var params = new URLSearchParams();
        if (movieIds.length) params.set('movies', movieIds.join(','));
        if (tvIds.length) params.set('tv', tvIds.join(','));
        lastFlush = fetch('/api/view-state?' + params.toString(), {
            credentials: 'same-origin'
        }).then(function (res) {
            if (!res.ok) throw new Error('HTTP ' + res.status);
            return res.json();
        }).then(function (data) {
            (data.viewed_movie_ids || []).forEach(function (id) {
                viewed.add(String(id));
            });
            (data.logged_today_movie_ids || []).forEach(function (id) {
                loggedToday.add(String(id));
            });
            var tp = data.tv_progress || {};
            for (var sid in tp) {
                if (Object.prototype.hasOwnProperty.call(tp, sid)) {
                    tvProgress[sid] = tp[sid];
                }
            }
            stateReady = true;
            // Paint HERE, not only in the caller's .then: callers attach
            // to whichever promise was current at call time, so a debounced
            // flush that completes after the last call would otherwise
            // never repaint (the /tv_shows bug).
            applyToCards(document);
            // Task D: quick-log action wording follows the store.
            document.dispatchEvent(new CustomEvent('FrameIQViewStateUpdated', {
                detail: { root: document },
            }));
            return true;
        }).catch(function () {
            return false;
        });
        return lastFlush;
    }

    function dedupe(arr) {
        var seen = Object.create(null);
        var out = [];
        for (var i = 0; i < arr.length; i++) {
            var v = String(arr[i]);
            if (!seen[v]) { seen[v] = true; out.push(v); }
        }
        return out;
    }

    /**
     * Full refresh cycle for client-rendered surfaces: ensure state for
     * the ids currently in `root`, then paint badges. Call after any
     * dynamic render (initial load, load-more, filter, modal).
     */
    function syncCards(root) {
        if (window.__IS_AUTH__ !== true) return;
        var scope = root && root.querySelectorAll ? root : document;
        var movieIds = [], tvIds = [];
        scope.querySelectorAll(
            '.movie-card a[href^="/movie/"], .media-card a[href^="/movie/"], ' +
            '.chat-media-items a[href^="/movie/"]')
            .forEach(function (a) {
                var m = (a.getAttribute('href') || '').match(/\/movie\/(\d+)/);
                if (m) movieIds.push(m[1]);
            });
        scope.querySelectorAll(
            '.tv-card a[href^="/tv/"], .show-card a[href^="/tv/"], ' +
            '.media-card a[href^="/tv/"], ' +
            '.chat-media-items a[href^="/tv/"]')
            .forEach(function (a) {
                var m = (a.getAttribute('href') || '').match(/\/tv\/(\d+)/);
                if (m) tvIds.push(m[1]);
            });
        scope.querySelectorAll('[data-rec-media-id]').forEach(function (c) {
            if (c.getAttribute('data-rec-media-type') === 'movie') {
                movieIds.push(c.getAttribute('data-rec-media-id'));
            } else {
                tvIds.push(c.getAttribute('data-rec-media-id'));
            }
        });
        // Surfaces whose cards have no href (trending onclick cards)
        // self-identify with data-card-id/type.
        scope.querySelectorAll('[data-card-id]').forEach(function (c) {
            if (c.getAttribute('data-card-type') === 'movie') {
                movieIds.push(c.getAttribute('data-card-id'));
            } else {
                tvIds.push(c.getAttribute('data-card-id'));
            }
        });
        ensureState({ movieIds: movieIds, tvIds: tvIds })
            .then(function (ok) { if (ok) applyToCards(root); });
    }

    /*
     * Phase 12 (dynamic content): newly inserted cards must also receive
     * state. A lightweight MutationObserver debounces a full sync on DOM
     * updates — ensureState's signature dedupe means repeat syncs with the
     * same ids never re-fetch. Pages with SERVER-rendered rail cards are
     * skipped: those surfaces own their state end-to-end and never mutate
     * their card grids client-side. Painting adds badge nodes, which
     * triggers the observer once more; the follow-up sync is a no-op, so
     * the loop terminates (classList toggles are not childList mutations).
     */
    var syncTimer = null;
    function startObserver() {
        if (document.querySelector('.rail-card')) return;
        if (!('MutationObserver' in window)) return;
        observer = new MutationObserver(function () {
            if (syncTimer) return;
            syncTimer = setTimeout(function () {
                syncTimer = null;
                syncCards(document);
            }, 150);
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    /**
     * Re-render the hero carousel viewed chips from the store.
     * Slides are server-rendered with data-hero-viewed markers; a chip that
     * has not been rendered yet (movie logged from a rail after page load)
     * is appended next to the rating chip.
     */
    function refreshHeroChips() {
        var slides = document.querySelectorAll('.hero-slide .hero-copy');
        for (var i = 0; i < slides.length; i++) {
            var copy = slides[i];
            var id = copy.getAttribute('data-hero-id');
            if (!id) continue;
            var chipsRow = copy.querySelector('.flex.flex-wrap.items-center');
            if (!chipsRow) continue;
            var existing = chipsRow.querySelector('.hero-viewed-chip');
            if (viewed.has(String(id)) && !existing) {
                var chip = document.createElement('span');
                chip.className = 'slate-chip';
                chip.setAttribute('style',
                    'background:#16a34a;border-color:#16a34a;color:#fff');
                chip.innerHTML = VIEWED_CHECK_SVG + 'Viewed';
                chipsRow.appendChild(chip);
            }
        }
    }

    /**
     * Re-render the TV detail overall progress line from a freshly fetched
     * progress object {watched, aired, percent}.
     */
    function refreshTvProgress(prog) {
        if (!prog || typeof prog.watched !== 'number') return;
        var line = document.querySelector('[data-tv-progress]');
        if (!line) return;
        line.setAttribute('data-watched', String(prog.watched));
        line.setAttribute('data-aired', String(prog.aired));
        line.setAttribute('data-percent', String(prog.percent));
        var badge = line.querySelector('.bg-green-600');
        if (badge) {
            badge.innerHTML = badge.innerHTML.replace(
                /(\d+(?:\.\d+)?)% watched/, prog.percent + '% watched');
            if (badge.textContent.indexOf('% watched') === -1) {
                badge.textContent = prog.percent + '% watched';
            }
        }
        var detail = line.querySelector('.text-\\[var\\(--text-mid\\)\\]') ||
            line.querySelectorAll('span')[line.querySelectorAll('span').length - 1];
        if (detail) {
            detail.textContent = prog.watched + ' of ' + prog.aired +
                ' aired episodes';
        }
    }

    /**
     * Called by quick-log.js after a successful movie log.
     */
    function onMovieLogged(tmdbId) {
        addViewed(tmdbId);
        markLoggedToday(tmdbId);
        refreshHeroChips();
        applyToCards(document);
    }

    /**
     * Called after an episode finish/season mark succeeds. Fetches the
     * fresh overall progress for the show from the API and updates the
     * hero line. Nothing happens on pages without [data-tv-progress].
     */
    function onEpisodeChange(showId) {
        if (!showId) return;
        fetch('/api/tv/' + encodeURIComponent(showId) +
              '/aired-progress?_=' + Date.now())
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data && data.tv_progress) {
                    tvProgress[String(showId)] = data.tv_progress;
                    refreshTvProgress(data.tv_progress);
                    applyToCards(document);
                }
            })
            .catch(function () { /* keep the previous value */ });
    }

    window.FrameIQViewState = {
        addViewed: addViewed,
        refreshHeroChips: refreshHeroChips,
        refreshTvProgress: refreshTvProgress,
        onMovieLogged: onMovieLogged,
        onEpisodeChange: onEpisodeChange,
        heroChipExists: heroChipExists,
        isViewedMovie: isViewedMovie,
        isLoggedToday: isLoggedToday,
        markLoggedToday: markLoggedToday,
        tvProgressFor: tvProgressFor,
        ensureState: ensureState,
        applyToCards: applyToCards,
        syncCards: syncCards,
        viewedCount: function () { return viewed.size; }
    };

    if (window.__IS_AUTH__) {
        startObserver();
    }
})();
