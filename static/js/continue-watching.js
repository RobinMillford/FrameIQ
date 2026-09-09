/**
 * Continue Watching — card actions (Feature: intent-based Continue Watching).
 *
 * The home rail is rendered server-side with all display metadata (canonical
 * title, poster, season/episode). This script wires the two explicit actions:
 *
 *   ✓ Finished → records canonical watched state and removes the item
 *   × Remove   → hides the item WITHOUT marking it watched
 *
 * Playback position is owned by the third-party provider; "▶ Continue" is a
 * plain link that reopens the exact same watch URL. No telemetry, no timers.
 *
 * CSRF: uses the csrf-token meta tag (same convention as quick-log.js), with
 * the fetch patch in base.html as a second layer.
 */
(function () {
    'use strict';

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') : '';
    }

    function post(url) {
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin'
        }).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        });
    }

    function cardOf(el) {
        return el.closest('[data-cw-card]');
    }

    function episodeEndpoint(card, action) {
        return '/api/continue-watching/tv/' + card.dataset.tmdbId +
            '/' + card.dataset.season + '/' + card.dataset.episode + '/' + action;
    }

    function removeCard(card) {
        card.style.opacity = '0.35';
        card.style.pointerEvents = 'none';
        // Collapse the wrapper so the rail reflows instead of leaving a gap.
        setTimeout(function () { card.remove(); }, 250);
    }

    function handleFinish(btn) {
        var card = cardOf(btn);
        if (!card || btn.disabled) return;
        btn.disabled = true;
        var type = card.dataset.mediaType;
        var url = type === 'movie'
            ? '/api/continue-watching/movie/' + card.dataset.tmdbId + '/finish'
            : episodeEndpoint(card, 'finish');

        post(url).then(function (res) {
            if (type === 'movie') {
                window.frameToast && window.frameToast('✓ Marked as watched', 'success');
            } else if (res && res.next) {
                window.frameToast && window.frameToast(
                    '✓ Finished — next: S' + res.next.season + 'E' + res.next.episode,
                    'success');
            } else {
                window.frameToast && window.frameToast('✓ Finished', 'success');
            }
            removeCard(card);
        }).catch(function () {
            btn.disabled = false;
            window.frameToast && window.frameToast('Could not update — try again', 'error');
        });
    }

    function handleRemove(btn) {
        var card = cardOf(btn);
        if (!card || btn.disabled) return;
        btn.disabled = true;
        var type = card.dataset.mediaType;
        var url = type === 'movie'
            ? '/api/continue-watching/movie/' + card.dataset.tmdbId + '/remove'
            : episodeEndpoint(card, 'remove');

        post(url).then(function () {
            window.frameToast && window.frameToast('Removed from Continue Watching', 'info');
            removeCard(card);
        }).catch(function () {
            btn.disabled = false;
            window.frameToast && window.frameToast('Could not remove — try again', 'error');
        });
    }

    document.addEventListener('click', function (e) {
        var finish = e.target.closest('[data-cw-finish]');
        if (finish) { handleFinish(finish); return; }
        var remove = e.target.closest('[data-cw-remove]');
        if (remove) { handleRemove(remove); }
    });
})();
