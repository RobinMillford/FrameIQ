/* F13 — share control for public detail pages.
 *
 * Mirrors the existing convention in watch_movie.html / watch_tv.html: the
 * Web Share API where supported, clipboard copy everywhere else, with visible
 * success AND failure feedback. Uses the CANONICAL page URL (supplied by the
 * server, not location.href) so a shared link always matches the URL declared
 * in <link rel="canonical"> — a watch page with tracking query parameters must
 * never be what gets pasted into a chat.
 *
 * No third-party SDK.
 */
(function () {
  'use strict';

  var root = document.getElementById('detail-share');
  if (!root) return;

  var button = root.querySelector('[data-share-button]');
  var status = root.querySelector('[data-share-status]');
  var cfg = {
    url: root.getAttribute('data-share-url') || '',
    title: root.getAttribute('data-share-title') || document.title
  };

  function say(message, tone) {
    if (!status) return;
    status.textContent = message;
    status.style.color = tone === 'error'
      ? 'var(--danger, #f87171)'
      : 'var(--text-mid)';
    window.clearTimeout(say._t);
    say._t = window.setTimeout(function () { status.textContent = ''; }, 2500);
  }

  function copyFallback() {
    if (!navigator.clipboard || !navigator.clipboard.writeText) {
      say('Copying is not supported here', 'error');
      return;
    }
    navigator.clipboard.writeText(cfg.url)
      .then(function () { say('Link copied'); })
      .catch(function () { say('Could not copy link', 'error'); });
  }

  function share() {
    if (navigator.share) {
      navigator.share({ title: cfg.title, url: cfg.url })
        .then(function () { say('Shared'); })
        .catch(function (err) {
          // A user-cancelled share is not a failure worth reporting.
          if (err && err.name === 'AbortError') return;
          copyFallback();
        });
      return;
    }
    copyFallback();
  }

  if (button) button.addEventListener('click', share);

  // Ctrl/Cmd+C copies the canonical link rather than the current URL, so what
  // lands in the clipboard is what a preview would resolve to.
  root.addEventListener('copy', function (e) {
    if (!cfg.url) return;
    e.clipboardData.setData('text/plain', cfg.url);
    e.preventDefault();
  });
})();