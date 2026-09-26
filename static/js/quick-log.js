/**
 * FrameIQ Quick Log — one-tap movie logging from any poster/card surface.
 *
 * The backend endpoint is POST /api/media/<id>/log (routes/diary.py).
 * It creates today's watch event (a rewatch when the movie was already
 * watched) and syncs the derived viewed state.
 *
 * Wiring: any element with [data-quick-log] triggers the action.
 * Required attributes:
 *   data-quick-log      = TMDb movie id
 *   data-title          = movie title (avoids a TMDB lookup)
 *   data-poster-path    = poster_path (optional)
 *   data-release-date   = YYYY-MM-DD (optional)
 *   data-rewatch        = "true" to force a rewatch event (optional)
 * Optional callback: window.__quickLogAfter?.(data)
 *
 * CSRF: uses the csrf-token meta tag (same convention as chat-page.js).
 * Duplicate-submission safety: button disabled + in-flight guard +
 * server-side cooldown (the endpoint collapses rapid duplicates while
 * still allowing a genuine second watch as a rewatch).
 */
(function () {
    'use strict';

    if (!window.__IS_AUTH__) return;

    var inFlight = {};

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    /**
     * Task D: the quick-log button is an ACTION ("create today's watch
     * event"), never a state claim — the ✓ Viewed badge is the state.
     * Wording per current canonical state (api/user_view_state.py):
     *   not viewed           → "Log Watched"
     *   viewed, not today    → "Log Rewatch"
     *   viewed + logged today→ "Watched Today"
     * Rewatches stay fully functional in all states.
     */
    function actionLabel(btn) {
        var id = btn.getAttribute('data-quick-log');
        if (window.FrameIQViewState && window.FrameIQViewState.isLoggedToday(id)) {
            return 'Watched Today';
        }
        if (window.FrameIQViewState && window.FrameIQViewState.isViewedMovie(id)) {
            return 'Log Rewatch';
        }
        return 'Log Watched';
    }

    /**
     * Stamp every quick-log action on the page with its current-state
     * label + tooltip. Runs once per page and again after every flush of
     * the shared view-state store, so server-rendered buttons always
     * agree with the canonical store.
     */
    function applyActionState(root) {
        var scope = root && root.querySelectorAll ? root : document;
        var buttons = scope.querySelectorAll('[data-quick-log]');
        for (var i = 0; i < buttons.length; i++) {
            var btn = buttons[i];
            var label = actionLabel(btn);
            var labelEl = btn.querySelector('.ql-log-label');
            if (labelEl) {
                labelEl.textContent = label;
            } else if (btn.childElementCount === 0 ||
                       (btn.childNodes.length === 1 &&
                        btn.firstChild.nodeType === 3)) {
                btn.textContent = label;
            }
            btn.title = label === 'Watched Today'
                ? label : label + ' (today)';
        }
    }

    // Refresh action wording whenever the shared store flushes (initial
    // bootstrap included) so viewed/logged-today state is never stale.
    document.addEventListener('FrameIQViewStateUpdated', function (e) {
        applyActionState(e.detail && e.detail.root);
    });

    function flashError(message) {
        if (typeof window.frameToast === 'function') {
            window.frameToast(message, 'error');
        } else {
            alert(message);
        }
    }

    function applyWatchedState(btn) {
        var card = btn.closest('.rail-card, .media-card, .movie-card, .show-card');
        if (card) {
            var badge = card.querySelector('.ql-watched-badge');
            if (badge) badge.classList.remove('hidden');
        }
        // The action stays visible and always offers the next legitimate
        // action (rewatch): a watch history is append-only, so hiding the
        // button would hide functionality rather than state.
        if (window.FrameIQViewState) {
            window.FrameIQViewState.markLoggedToday(btn.getAttribute('data-quick-log'));
        }
        var labelEl = btn.querySelector('.ql-log-label');
        if (labelEl) {
            labelEl.textContent = 'Watched Today';
        }
        btn.title = 'Watched Today';
    }

    function submit(btn) {
        var mediaId = btn.getAttribute('data-quick-log');
        if (!mediaId || inFlight[mediaId]) return;
        inFlight[mediaId] = true;

        var payload = {
            media_type: 'movie',
            title: btn.getAttribute('data-title') || '',
            poster_path: btn.getAttribute('data-poster-path') || null,
            release_date: btn.getAttribute('data-release-date') || '',
            force_rewatch: btn.getAttribute('data-rewatch') === 'true',
        };

        btn.disabled = true;
        btn.classList.add('opacity-60', 'cursor-wait');

        fetch('/api/media/' + encodeURIComponent(mediaId) + '/log', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken(),
            },
            body: JSON.stringify(payload),
        })
            .then(function (res) {
                return res.json().catch(function () {
                    return {};
                }).then(function (data) {
                    return { status: res.status, data: data };
                });
            })
            .then(function (out) {
                if (out.status >= 400) {
                    throw new Error(out.data.error || 'Could not log this movie');
                }
                applyWatchedState(btn);
                if (typeof window.frameToast === 'function') {
                    window.frameToast(
                        out.data.is_rewatch
                            ? 'Rewatch logged ✓'
                            : 'Logged as watched ✓',
                        'success'
                    );
                }
                // Cross-surface consistency (Phase 16): record the viewed
                // id in the shared client state so every view-state helper
                // (hero chips, rail badges, action wording) sees it
                // immediately — no reload needed.
                if (window.FrameIQViewState && mediaId) {
                    window.FrameIQViewState.onMovieLogged(String(mediaId));
                }
                if (typeof window.__quickLogAfter === 'function') {
                    window.__quickLogAfter(out.data);
                }
            })
            .catch(function (err) {
                btn.disabled = false;
                btn.classList.remove('opacity-60', 'cursor-wait');
                flashError(err.message || 'Network error. Please try again.');
            })
            .finally(function () {
                delete inFlight[mediaId];
            });
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-quick-log]');
        if (btn) {
            e.preventDefault();
            submit(btn);
        }
    });

    /**
     * Server-rendered rails (no client render hooks of their own) would
     * otherwise never learn the logged-today/viewed state. One batched
     * ensureState() per page (id-signature deduped — surfaces that already
     * synced these ids never re-fetch) seeds the store; the resulting
     * FrameIQViewStateUpdated event stamps the wording.
     */
    function kickStoreBootstrap() {
        if (!window.FrameIQViewState) {
            setTimeout(kickStoreBootstrap, 50);   // view-state.js loads after us
            return;
        }
        var buttons = document.querySelectorAll('[data-quick-log]');
        if (!buttons.length) return;
        var movieIds = [];
        for (var i = 0; i < buttons.length; i++) {
            movieIds.push(buttons[i].getAttribute('data-quick-log'));
        }
        window.FrameIQViewState.ensureState({ movieIds: movieIds });
    }
    kickStoreBootstrap();

    if (typeof window.__quickLogAfter === 'undefined') {
        window.__quickLogAfter = null;
    }
})();