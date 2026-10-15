/**
 * Notifications — bell badge + panel (Feature 04).
 *
 * Architecture: page-navigation-driven, NOT polling.
 *   - unread count is fetched ONCE per page load (server-rendered pages are
 *     the refresh cadence — no recurring timers, no persistent connections)
 *   - the list is fetched ONLY when the panel is opened
 *   - actions (open / mark read / mark all read) are plain POSTs via the
 *     CSRF-patched fetch in base.html
 */
(function () {
    'use strict';

    var bell = document.getElementById('notif-bell');
    var panel = document.getElementById('notif-panel');
    var badge = document.getElementById('notif-badge');
    var list = document.getElementById('notif-list');
    var markAllBtn = document.getElementById('notif-mark-all');
    if (!bell || !panel || !badge || !list) return;

    var unread = 0;
    var panelLoaded = false;

    function setBadge(count) {
        unread = count;
        if (count > 0) {
            badge.textContent = count > 99 ? '99+' : String(count);
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
    }

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = String(s == null ? '' : s);
        return d.innerHTML;
    }

    /**
     * Escape for a QUOTED ATTRIBUTE.
     *
     * F35: `esc()` above is the `textContent -> innerHTML` variant, which encodes
     * only `& < >`. Quotes pass through untouched, so using it inside
     * `href="…"` let a value terminate the attribute and the remainder become
     * live markup. That is the exact hazard documented in static/js/dom-safety.js.
     */
    function escAttr(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * A link target that is safe to put in `href`, or '' when unusable.
     *
     * Prefers the shared policy in dom-safety.js. The local fallback is
     * deliberately narrow — same-origin, root-relative paths only — and fails
     * closed: an unrecognised or scheme-bearing value yields '' so the caller
     * renders inert markup instead of a clickable URL.
     */
    function safeHref(value) {
        if (typeof value !== 'string' || !value) return '';
        var D = window.FrameIQDOM;
        if (D && typeof D.safeLinkUrl === 'function') {
            return D.safeLinkUrl(value) || '';
        }
        var raw = value.trim();
        if (raw.charAt(0) !== '/') return '';
        if (raw.slice(0, 2) === '//') return '';
        if (/[\u0000-\u0020\u007f]/.test(raw)) return '';
        if (raw.indexOf('\\') !== -1 || raw.indexOf('..') !== -1) return '';
        return raw;
    }

    function timeAgo(iso) {
        if (!iso) return '';
        var s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
        if (s < 60) return 'just now';
        if (s < 3600) return Math.floor(s / 60) + 'm ago';
        if (s < 86400) return Math.floor(s / 3600) + 'h ago';
        if (s < 86400 * 7) return Math.floor(s / 86400) + 'd ago';
        return new Date(iso).toLocaleDateString();
    }

    function itemHtml(n) {
        var dot = n.read ? '' :
            '<span class="mt-1.5 w-2 h-2 shrink-0 rounded-full bg-[var(--accent)]" aria-label="Unread"></span>';
        var epLine = (n.season != null && n.episode != null)
            ? 'S' + esc(n.season) + 'E' + esc(n.episode) : '';
        var inner =
            '<div class="flex-1 min-w-0">' +
            '<p class="text-sm font-medium text-white truncate">' + esc(n.title) + '</p>' +
            '<p class="text-sm text-[var(--text-mid)] truncate">' + esc(n.body) + '</p>' +
            '<p class="text-[11px] text-[var(--text-low)] mt-0.5">' + epLine +
            (epLine ? ' · ' : '') + timeAgo(n.created_at) + '</p>' +
            '</div>';
        var href = safeHref(n.target_url);
        if (href) {
            return '<a href="' + escAttr(href) + '" data-notif-id="' + escAttr(n.id) + '"' +
                   (n.read ? '' : ' data-unread="1"') +
                   ' class="flex gap-2.5 px-4 py-3 hover:bg-white/5 border-b border-[var(--line)] last:border-b-0">' +
                   dot + inner + '</a>';
        }
        // No target, or an unsafe one: render the row inert rather than
        // emitting a clickable URL we could not validate.
        return '<div class="flex gap-2.5 px-4 py-3 border-b border-[var(--line)] last:border-b-0"' +
               (n.id == null ? '' : ' data-notif-id="' + escAttr(n.id) + '"') +
               (n.read ? '' : ' data-unread="1"') + '>' +
               dot + inner + '</div>';
    }

    function renderList(data) {
        setBadge(data.unread_count || 0);
        if (!data.notifications || !data.notifications.length) {
            list.innerHTML = '<p class="px-4 py-8 text-sm text-[var(--text-low)] text-center">No notifications yet.</p>';
            return;
        }
        list.innerHTML = data.notifications.map(itemHtml).join('');
    }

    function renderListError() {
        list.innerHTML = '<p class="px-4 py-8 text-sm text-[var(--text-low)] text-center">Notifications unavailable right now.</p>';
    }

    function loadList() {
        list.innerHTML = '<p class="px-4 py-8 text-sm text-[var(--text-low)] text-center">Loading…</p>';
        fetch('/api/notifications', { credentials: 'same-origin' })
            .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(renderList)
            .catch(renderListError);
    }

    function refreshCount() {
        fetch('/api/notifications?limit=1', { credentials: 'same-origin' })
            .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(function (data) { setBadge(data.unread_count || 0); })
            .catch(function () { /* badge stays as-is; finite, no retry */ });
    }

    function togglePanel(open) {
        var willOpen = (open === undefined) ? panel.classList.contains('hidden') : open;
        panel.classList.toggle('hidden', !willOpen);
        bell.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
        if (willOpen && !panelLoaded) {
            panelLoaded = true;
            loadList();
        }
    }

    /* ── Events (event-delegated; no timers anywhere) ──────────────────── */

    bell.addEventListener('click', function (e) {
        e.stopPropagation();
        togglePanel();
    });

    document.addEventListener('click', function (e) {
        // panel.contains: the panel may be portal-hosted at <body> level
        // (chrome.js header-layering contract), so "inside the menu"
        // clicks no longer sit under #notif-root.
        if (!panel.classList.contains('hidden') &&
            !document.getElementById('notif-root').contains(e.target) &&
            !panel.contains(e.target)) {
            togglePanel(false);
        }
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') togglePanel(false);
    });

    // Clicking a notification navigates; also mark it read (fire-and-forget)
    // and update the badge from the response.
    list.addEventListener('click', function (e) {
        var a = e.target.closest('[data-notif-id]');
        if (!a || !a.dataset.unread) return;
        e.preventDefault();
        fetch('/api/notifications/' + a.dataset.notifId + '/read', {
            method: 'POST', credentials: 'same-origin'
        }).then(function (r) { return r.ok ? r.json() : null; })
          .then(function (data) {
              if (data) {
                  setBadge(data.unread_count || 0);
                  panelLoaded = false;
              }
              window.location.href = a.getAttribute('href');  // exact episode URL (server-generated)
          })
          .catch(function () { window.location.href = a.getAttribute('href'); });
    });

    if (markAllBtn) {
        markAllBtn.addEventListener('click', function () {
            fetch('/api/notifications/read-all', {
                method: 'POST', credentials: 'same-origin'
            }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
              .then(function () {
                  setBadge(0);
                  panelLoaded = false;
                  loadList();
                  if (window.frameToast) window.frameToast('All notifications marked as read', 'success');
              })
              .catch(function () {
                  if (window.frameToast) window.frameToast('Could not mark all as read', 'error');
              });
        });
    }

    // One count fetch per page load — the only network activity at startup.
    refreshCount();
})();
