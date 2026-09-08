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
            var label = card.querySelector('.ql-log-label');
            if (label) label.textContent = 'Watched ✓';
            btn.classList.add('hidden');
        }
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

    if (typeof window.__quickLogAfter === 'undefined') {
        window.__quickLogAfter = null;
    }
})();