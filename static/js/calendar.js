/**
 * Unified personal entertainment calendar (Features 10A–10C).
 *
 * One bounded request per view state to FrameIQ's own /api/calendar
 * (authenticated, user-scoped — no user_id parameter exists). All
 * rendering is DOM-safe (createElement/textContent — event titles come
 * from TMDb/user data are never trusted; no HTML-string assignment).
 *
 * Feature 10C adds: real month navigation (prev/next/today anchored to
 * the displayed month, not "now"), request sequencing via
 * AbortController (stale responses can never overwrite newer state,
 * duplicate concurrent fetches are impossible), lightweight loading
 * skeleton, actionable error states with retry (401 session-expired,
 * 429 rate-limited, network/5xx), an accessible plus-N day dialog,
 * view/type/scope URL synchronization (?view=&type=&scope=), a bounded
 * client-side response cache keyed by start|end|type|scope, and
 * server-canonical "today" from meta.today (never a UTC-converted
 * local timestamp).
 */
(function () {
    'use strict';

    var VIEWS = ['month', 'agenda'];
    var TYPES = ['all', 'tv', 'movie'];
    var SCOPES = ['all', 'watchlist', 'tracking'];
    var MONTH_CHUNK = 3;      // visible chips per month cell before "+N more"
    var AGENDA_WINDOW = 30;   // agenda days per window (within the 62-day cap)
    var CACHE_MAX = 24;       // bounded client cache of fetched view states

    var state = {
        view: 'agenda',       // 'month' | 'agenda'
        type: 'all',          // all | tv | movie
        scope: 'all',         // all | watchlist | tracking
        anchor: null,         // Date — month (month view) / window start (agenda)
        events: [],
        meta: null,
    };

    var controller = null;    // AbortController for the in-flight request
    var reqSeq = 0;           // monotonic guard: stale responses are ignored
    var cache = new Map();    // "start|end|type|scope" -> {events, meta}
    var dialogTrigger = null; // element to restore focus to on dialog close

    /* ── date helpers (local components only — no UTC round-trips) ───── */

    function iso(d) {
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return d.getFullYear() + '-' + m + '-' + day;
    }

    function parseIsoKey(key) {
        // 'YYYY-MM-DD' → local Date via explicit components (never
        // Date.parse/UTC, which shift calendar days across timezones).
        var p = key.split('-');
        return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    }

    function addDays(d, n) {
        var c = new Date(d);
        c.setDate(c.getDate() + n);
        return c;
    }

    function addMonths(d, n) {
        return new Date(d.getFullYear(), d.getMonth() + n, 1);
    }

    function startOfWeek(d) {
        var c = new Date(d);
        var dow = (c.getDay() + 6) % 7; // Monday = 0
        return addDays(c, -dow);
    }

    function fmtDay(d) {
        return d.toLocaleDateString(undefined,
            { weekday: 'short', month: 'short', day: 'numeric' });
    }

    function fmtMonth(d) {
        return d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    }

    function todayIso() {
        // Server-canonical when available (meta.today is part of every
        // calendar response); local date only as a pre-first-load fallback.
        if (state.meta && state.meta.today) return state.meta.today;
        return iso(new Date());
    }

    /* ── URL state (Feature 10C §S) ──────────────────────────────────── */

    function initFromUrl() {
        var q = new URLSearchParams(window.location.search);
        var view = q.get('view');
        var type = q.get('type');
        var scope = q.get('scope');
        if (VIEWS.indexOf(view) !== -1) state.view = view;
        if (TYPES.indexOf(type) !== -1) state.type = type;
        if (SCOPES.indexOf(scope) !== -1) state.scope = scope;
    }

    function syncUrl() {
        var q = new URLSearchParams();
        q.set('view', state.view);
        q.set('type', state.type);
        q.set('scope', state.scope);
        // replaceState (no history entries): back/forward stays
        // predictable — Back leaves the calendar, it never replays
        // intermediate filter states.
        window.history.replaceState(null, '',
            window.location.pathname + '?' + q.toString());
    }

    /* ── data ────────────────────────────────────────────────────────── */

    function computeRange() {
        var anchor = state.anchor;
        if (state.view === 'month') {
            var first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
            var last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
            // include leading/trailing week context for the grid
            state.rangeStart = startOfWeek(first);
            state.rangeEnd = addDays(startOfWeek(last), 6);
        } else {
            state.rangeStart = new Date(anchor);
            state.rangeEnd = addDays(anchor, AGENDA_WINDOW);
        }
    }

    function cacheKey() {
        return iso(state.rangeStart) + '|' + iso(state.rangeEnd) + '|' +
            state.type + '|' + state.scope;
    }

    function load() {
        computeRange();
        var key = cacheKey();
        var cached = cache.get(key);
        if (cached) {
            // Refresh insertion order so the bound stays LRU-ish.
            cache.delete(key);
            cache.set(key, cached);
            state.events = cached.events;
            state.meta = cached.meta;
            render();
            return;
        }

        // Abort any in-flight request: navigating quickly can never
        // produce two live fetches, so duplicate responses are
        // structurally impossible (§N).
        if (controller) controller.abort();
        controller = new AbortController();
        var seq = ++reqSeq;

        var url = '/api/calendar?start=' + iso(state.rangeStart) +
            '&end=' + iso(state.rangeEnd) +
            '&type=' + state.type + '&scope=' + state.scope;

        setBusy(true);
        fetch(url, { credentials: 'same-origin', signal: controller.signal })
            .then(function (r) {
                // login_required answers a fetch with a redirect to the
                // login page (HTML) — an expired session must not be
                // misread as a data response.
                var ct = r.headers.get('content-type') || '';
                if (r.status === 401 || r.status === 403 ||
                    (r.ok && ct.indexOf('application/json') === -1)) {
                    return { authError: true };
                }
                if (r.status === 429) return { rateLimited: true };
                if (!r.ok) return { serverError: true };
                return r.json();
            })
            .then(function (data) {
                if (seq !== reqSeq) return;   // stale — a newer navigation won
                if (data.authError) return renderError('auth');
                if (data.rateLimited) return renderError('rate');
                if (data.serverError) return renderError('server');
                if (!data || !Array.isArray(data.events) || !data.meta) {
                    return renderError('malformed');
                }
                cache.set(key, data);
                if (cache.size > CACHE_MAX) {
                    cache.delete(cache.keys().next().value);
                }
                state.events = data.events;
                state.meta = data.meta;
                render();
            })
            .catch(function (err) {
                if (err && err.name === 'AbortError') return; // superseded
                if (seq !== reqSeq) return;
                renderError('network');
            })
            .then(function () {
                if (seq === reqSeq) setBusy(false);
            });
    }

    function navigate(anchor) {
        state.anchor = anchor;
        syncUrl();
        load();
    }

    /* ── rendering ───────────────────────────────────────────────────── */

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function setBusy(busy) {
        var surface = document.getElementById('cal-surface');
        if (!surface) return;
        surface.setAttribute('aria-busy', busy ? 'true' : 'false');
        surface.classList.toggle('cal-loading', busy);
    }

    function eventsByDate() {
        var map = {};
        state.events.forEach(function (ev) {
            (map[ev.date] = map[ev.date] || []).push(ev);
        });
        return map;
    }

    function eventChip(ev, compact) {
        var isTv = ev.event_type === 'episode';
        var chip = el('button', 'cal-chip' + (isTv ? ' cal-chip-tv' : ' cal-chip-movie'));
        chip.type = 'button';
        chip.setAttribute('data-url', ev.detail_url);
        var aria = fmtDay(parseIsoKey(ev.date)) + ', ' + ev.title + (isTv
            ? (', season ' + ev.season_number + ' episode ' + ev.episode_number +
               ', television episode')
            : (', movie release' +
               (ev.release_type ? ', ' + ev.release_type : '') +
               (ev.watched ? ', watched' : '')));
        chip.setAttribute('aria-label', aria);

        var label = el('span', 'cal-chip-label',
            (isTv ? 'S' + ev.season_number + 'E' + ev.episode_number + ' · '
                  : '') + ev.title);
        if (compact) label.classList.add('cal-truncate');

        chip.appendChild(el('span', 'cal-dot ' + (isTv ? 'cal-dot-tv' : 'cal-dot-movie')));
        chip.appendChild(label);
        // Watched state is text, never colour alone (§M).
        if (ev.watched) chip.appendChild(el('span', 'cal-chip-seen', 'Seen'));
        chip.appendChild(el('span', 'cal-chip-type', isTv ? 'TV' : 'Film'));
        chip.addEventListener('click', function () {
            var url = chip.getAttribute('data-url');
            if (url) window.location.href = url;
        });
        return chip;
    }

    function renderMonth() {
        var host = document.getElementById('cal-month');
        host.replaceChildren();
        host.classList.remove('hidden');
        document.getElementById('cal-agenda').classList.add('hidden');

        var byDate = eventsByDate();
        var today = todayIso();
        var grid = el('div', 'cal-grid');
        grid.setAttribute('role', 'grid');
        grid.setAttribute('aria-label', fmtMonth(state.anchor));
        ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach(function (dw) {
            grid.appendChild(el('div', 'cal-dow', dw));
        });

        var cursor = new Date(state.rangeStart);
        while (cursor <= state.rangeEnd) {
            var key = iso(cursor);
            var inMonth = cursor.getMonth() === state.anchor.getMonth();
            var cell = el('div', 'cal-cell' +
                (key === today ? ' cal-cell-today' : '') +
                (inMonth ? '' : ' cal-cell-adjacent'));
            cell.setAttribute('aria-label', fmtDay(cursor));
            cell.appendChild(el('div', 'cal-daynum', String(cursor.getDate())));
            var list = el('div', 'cal-cell-events');
            (byDate[key] || []).slice(0, MONTH_CHUNK).forEach(function (ev) {
                list.appendChild(eventChip(ev, true));
            });
            var total = (byDate[key] || []).length;
            if (total > MONTH_CHUNK) {
                var more = el('button', 'cal-more',
                    '+' + (total - MONTH_CHUNK) + ' more');
                more.type = 'button';
                more.setAttribute('aria-label',
                    'Show all ' + total + ' events on ' + fmtDay(cursor));
                more.addEventListener('click', function (d, evs) {
                    return function () { openDayDialog(d, evs, more); };
                }(key, byDate[key]));
                list.appendChild(more);
            }
            cell.appendChild(list);
            grid.appendChild(cell);
            cursor = addDays(cursor, 1);
        }
        host.appendChild(grid);
    }

    function renderAgenda() {
        var host = document.getElementById('cal-agenda');
        host.replaceChildren();
        host.classList.remove('hidden');
        document.getElementById('cal-month').classList.add('hidden');

        var byDate = eventsByDate();
        var today = todayIso();
        var dates = Object.keys(byDate).sort();

        if (!dates.length) {
            renderEmpty(host);
            return;
        }

        dates.forEach(function (key) {
            var section = el('section', 'cal-agenda-day');
            var head = el('h3', 'cal-agenda-date',
                key === today ? 'Today' : fmtDay(parseIsoKey(key)));
            section.appendChild(head);

            byDate[key].forEach(function (ev) {
                var row = eventChip(ev, false);
                row.classList.add('cal-agenda-item');
                if (ev.time) {
                    row.appendChild(el('span', 'cal-agenda-time', ev.time));
                }
                section.appendChild(row);
            });
            host.appendChild(section);
        });
    }

    /* ── day dialog (Feature 10C §B/§K/§M) ───────────────────────────── */

    function ensureDialog() {
        var overlay = document.getElementById('cal-day-dialog');
        if (overlay) return overlay;
        overlay = el('div', 'cal-dialog-overlay hidden');
        overlay.id = 'cal-day-dialog';
        var box = el('div', 'cal-dialog');
        box.setAttribute('role', 'dialog');
        box.setAttribute('aria-modal', 'true');
        box.setAttribute('aria-labelledby', 'cal-day-dialog-title');
        var close = el('button', 'cal-dialog-close', '\u00d7');
        close.type = 'button';
        close.setAttribute('aria-label', 'Close');
        var title = el('h3', 'cal-dialog-title');
        title.id = 'cal-day-dialog-title';
        var list = el('div', 'cal-dialog-list');
        box.appendChild(close);
        box.appendChild(title);
        box.appendChild(list);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
        close.addEventListener('click', closeDayDialog);
        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeDayDialog();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' &&
                !overlay.classList.contains('hidden')) closeDayDialog();
        });
        return overlay;
    }

    function openDayDialog(dateKey, events, trigger) {
        dialogTrigger = trigger || null;
        var overlay = ensureDialog();
        overlay.querySelector('.cal-dialog-title').textContent =
            fmtDay(parseIsoKey(dateKey));
        var list = overlay.querySelector('.cal-dialog-list');
        list.replaceChildren();
        events.forEach(function (ev) {
            var row = eventChip(ev, false);
            row.classList.add('cal-dialog-item');
            if (ev.time) row.appendChild(el('span', 'cal-agenda-time', ev.time));
            list.appendChild(row);
        });
        overlay.classList.remove('hidden');
        overlay.querySelector('.cal-dialog-close').focus();
    }

    function closeDayDialog() {
        var overlay = document.getElementById('cal-day-dialog');
        if (overlay) overlay.classList.add('hidden');
        if (dialogTrigger && dialogTrigger.focus) dialogTrigger.focus();
        dialogTrigger = null;
    }

    /* ── empty / error / summary ─────────────────────────────────────── */

    function activeHost() {
        return document.getElementById(
            state.view === 'month' ? 'cal-month' : 'cal-agenda');
    }

    function renderEmpty(host) {
        var wrap = el('div', 'cal-empty');
        var icon = el('i', 'far fa-calendar');
        var title = el('p', 'cal-empty-title', 'Nothing coming up');
        var sub = el('p', 'cal-empty-sub', emptyCopy());
        wrap.appendChild(icon);
        wrap.appendChild(title);
        wrap.appendChild(sub);
        host.appendChild(wrap);
    }

    function emptyCopy() {
        if (state.scope === 'watchlist') {
            return state.type === 'tv'
                ? 'No watchlist releases in this period.'
                : 'Add movies to your watchlist to see their release dates here.';
        }
        if (state.scope === 'tracking' || state.type === 'tv') {
            return 'Your tracked shows have no upcoming episodes.';
        }
        return 'Track shows or add movies to your watchlist to see what\u2019s coming up.';
    }

    function renderError(kind) {
        var host = activeHost();
        host.replaceChildren();
        var wrap = el('div', 'cal-empty cal-error');
        var copy = {
            auth: ['Session expired', 'Please sign in again to view your calendar.'],
            rate: ['Too many requests', 'You\u2019re moving fast \u2014 wait a moment, then try again.'],
            network: ['Calendar is unavailable right now', 'Check your connection and try again.'],
            server: ['Calendar is unavailable right now', 'Something went wrong on our side. Try again shortly.'],
            malformed: ['Calendar is unavailable right now', 'The response was invalid. Try again.'],
        }[kind] || ['Calendar is unavailable right now', 'Try again.'];
        wrap.appendChild(el('p', 'cal-empty-title', copy[0]));
        wrap.appendChild(el('p', 'cal-empty-sub', copy[1]));
        if (kind === 'auth') {
            var signIn = el('a', 'cal-retry', 'Sign in');
            signIn.href = '/login';
            wrap.appendChild(signIn);
        } else {
            var retry = el('button', 'cal-retry', 'Retry');
            retry.type = 'button';
            retry.addEventListener('click', function () { load(); });
            wrap.appendChild(retry);
        }
        host.appendChild(wrap);
    }

    function renderSummary() {
        var c = (state.meta && state.meta.counts) || { tv: 0, movie: 0 };
        var el1 = document.getElementById('cal-count-tv');
        var el2 = document.getElementById('cal-count-movie');
        if (el1) el1.textContent = c.tv + (c.tv === 1 ? ' episode' : ' episodes');
        if (el2) el2.textContent = c.movie +
            (c.movie === 1 ? ' movie release' : ' movie releases');

        // Unknown release dates (Feature 10B): surface unobtrusively —
        // never invent a fallback date for these titles (§J).
        var unknown = (state.meta && state.meta.release_date_unknown) || null;
        var unkCount = unknown && typeof unknown === 'object'
            ? (unknown.count || 0) : (unknown || 0);
        var note = document.getElementById('cal-unknown');
        if (note) {
            if (unkCount > 0) {
                note.textContent = unkCount +
                    (unkCount === 1
                        ? ' watchlist title has no release date yet'
                        : ' watchlist titles have no release date yet');
                var titles = (unknown && unknown.titles) || [];
                if (titles.length) note.title = titles.join(', ');
                note.classList.remove('hidden');
            } else {
                note.classList.add('hidden');
            }
        }

        // Coming-up strip: next 3 events from the server's today onward.
        var strip = document.getElementById('cal-coming-up');
        strip.replaceChildren();
        var today = todayIso();
        var upcoming = state.events.filter(function (ev) {
            return ev.date >= today;
        }).slice(0, 3);
        if (upcoming.length) {
            strip.appendChild(el('span', 'cal-strip-title', 'Coming up'));
            upcoming.forEach(function (ev) {
                strip.appendChild(eventChip(ev, true));
            });
        }
    }

    function render() {
        document.getElementById('cal-month-label').textContent =
            fmtMonth(state.anchor);
        if (state.view === 'month') renderMonth(); else renderAgenda();
        renderSummary();
    }

    /* ── controls ────────────────────────────────────────────────────── */

    function bindGroup(id, attr, onChange) {
        var root = document.getElementById(id);
        if (!root) return;
        root.querySelectorAll('button[data-' + attr + ']').forEach(function (b) {
            b.addEventListener('click', function () {
                root.querySelectorAll('button[data-' + attr + ']').forEach(
                    function (o) {
                        o.classList.remove('active');
                        o.setAttribute('aria-pressed', 'false');
                    });
                b.classList.add('active');
                b.setAttribute('aria-pressed', 'true');
                onChange(b.getAttribute('data-' + attr));
            });
        });
    }

    function on(id, fn) {
        var node = document.getElementById(id);
        if (node) node.addEventListener('click', fn);
    }

    function init() {
        if (document.documentElement.dataset.calInit === '1') return;
        document.documentElement.dataset.calInit = '1';

        state.anchor = new Date();
        state.anchor = new Date(state.anchor.getFullYear(),
            state.anchor.getMonth(), 1);
        initFromUrl();
        syncUrl();

        bindGroup('cal-view-group', 'view', function (v) {
            state.view = v;
            syncUrl();
            // Re-anchor without refetching when the cached range already
            // covers the toggle; otherwise fetch the new window.
            load();
        });
        bindGroup('cal-type-group', 'type', function (v) {
            state.type = v;
            syncUrl();
            load();
        });
        bindGroup('cal-scope-group', 'scope', function (v) {
            state.scope = v;
            syncUrl();
            load();
        });

        on('cal-prev', function () {
            navigate(state.view === 'month'
                ? addMonths(state.anchor, -1)
                : addDays(state.anchor, -7));
        });
        on('cal-next', function () {
            navigate(state.view === 'month'
                ? addMonths(state.anchor, 1)
                : addDays(state.anchor, 7));
        });
        on('cal-today', function () {
            var now = new Date();
            navigate(new Date(now.getFullYear(), now.getMonth(), 1));
        });

        // Quick filters set view + anchor, then ONE load — never a
        // synthetic button-press chain (the old double-fetch bug).
        on('cal-quick-today', function () {
            var now = new Date();
            state.view = 'agenda';
            markActive('cal-view-group', 'view', 'agenda');
            navigate(new Date(now.getFullYear(), now.getMonth(), now.getDate()));
        });
        on('cal-quick-week', function () {
            var now = new Date();
            state.view = 'agenda';
            markActive('cal-view-group', 'view', 'agenda');
            navigate(startOfWeek(now));
        });
        on('cal-quick-month', function () {
            var now = new Date();
            state.view = 'month';
            markActive('cal-view-group', 'view', 'month');
            navigate(new Date(now.getFullYear(), now.getMonth(), 1));
        });

        load();
    }

    function markActive(groupId, attr, value) {
        var root = document.getElementById(groupId);
        if (!root) return;
        root.querySelectorAll('button[data-' + attr + ']').forEach(
            function (o) {
                var on = o.getAttribute('data-' + attr) === value;
                o.classList.toggle('active', on);
                o.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
