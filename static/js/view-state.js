/**
 * FrameIQ View State — one shared client-side store for user-scoped
 * viewing state (mirrors api/user_view_state.py).
 *
 * Purpose (cross-surface consistency): when the user marks a movie watched
 * or finishes an episode, every view-state surface on the page — hero
 * chips, rail badges, the TV detail progress line — updates immediately
 * from this single store instead of waiting for a full reload.
 *
 * The store is per-page (no persistence): every fresh server render is the
 * source of truth. Loaded for authenticated users only (anonymous users
 * never receive personalized state).
 *
 * Server-rendered badges keep working unchanged: rails pre-render the
 * Viewed badge per user_viewed_keys, and quick-log flips it directly.
 * This store additionally covers surfaces quick-log cannot reach (hero
 * chips) and the incremental TV progress line on the TV detail page.
 */
(function () {
    'use strict';

    if (!window.__IS_AUTH__) return;

    var viewed = new Set(
        (window.__VIEWED_MOVIE_IDS__ || []).map(String)
    );

    function heroChipExists(tmdbId) {
        // Hero viewed chips only exist for movie slides; the server renders
        // one per viewed pick. When the id is already in the store the chip
        // was rendered server-side too.
        return viewed.has(String(tmdbId));
    }

    function addViewed(tmdbId) {
        viewed.add(String(tmdbId));
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
                chip.innerHTML =
                    '<svg class="w-3 h-3" fill="none" stroke="currentColor" ' +
                    'viewBox="0 0 24 24"><path stroke-linecap="round" ' +
                    'stroke-linejoin="round" stroke-width="2" ' +
                    'd="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z">' +
                    '</path></svg>Viewed';
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
        refreshHeroChips();
    }

    /**
     * Called after an episode finish/season mark succeeds. Fetches the
     * fresh overall progress for the show from the API and updates the
     * hero line. Nothing happens on pages without [data-tv-progress].
     */
    function onEpisodeChange(showId) {
        if (!document.querySelector('[data-tv-progress]')) return;
        if (!showId) return;
        fetch('/api/tv/' + encodeURIComponent(showId) +
              '/aired-progress?_=' + Date.now())
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data && data.tv_progress) refreshTvProgress(data.tv_progress);
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
        viewedCount: function () { return viewed.size; }
    };
})();
