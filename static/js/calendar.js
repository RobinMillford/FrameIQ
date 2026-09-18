/**
 * Unified personal entertainment calendar (Feature 10).
 *
 * One bounded request per view state to FrameIQ's own /api/calendar
 * (authenticated, user-scoped — no user_id parameter exists). All
 * rendering is DOM-safe (createElement/textContent — event titles come
 * from TMDb/user data and are never trusted HTML).
 *
 * Views: month grid (desktop-first) + agenda (mobile-first).
 * Quick filters (Today / This Week / This Month) recompute the range
 * client-side — no extra polling or live counters.
 */
(function () {
    'use strict';

    var state = {
        view: 'agenda',       // 'month' | 'agenda'
        type: 'all',          // all | tv | movie
        scope: 'all',         // all | watchlist | tracking
        rangeStart: null,     // Date (range the UI displays)
        rangeEnd: null,
        events: [],
        meta: null,
    };

    /* ── date helpers (local, deterministic) ─────────────────────────── */

    function iso(d) {
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return d.getFullYear() + '-' + m + '-' + day;
    }

    function addDays(d, n) {
        var c = new Date(d);
        c.setDate(c.getDate() + n);
        return c;
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

    /* ── data ────────────────────────────────────────────────────────── */

    function computeRange() {
        var today = new Date();
        if (state.view === 'month') {
            var first = new Date(today.getFullYear(), today.getMonth(), 1);
            var last = new Date(today.getFullYear(), today.getMonth() + 1, 0);
            // include leading/trailing week context for the grid
            state.rangeStart = startOfWeek(first);
            state.rangeEnd = addDays(startOfWeek(last), 6);
        } else {
            // agenda: today + next 30 days
            state.rangeStart = today;
            state.rangeEnd = addDays(today, 30);
        }
    }

    function load() {
        computeRange();
        var url = '/api/calendar?start=' + iso(state.rangeStart) +
            '&end=' + iso(state.rangeEnd) +
            '&type=' + state.type + '&scope=' + state.scope;
        var req = fetch(url, { credentials: 'same-origin' });
        req.then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        }).then(function (data) {
            state.events = data.events || [];
            state.meta = data.meta || {};
            render();
        }).catch(function () {
            renderError();
        });
    }

    /* ── rendering ───────────────────────────────────────────────────── */

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function eventsByDate() {
        var map = {};
        state.events.forEach(function (ev) {
            (map[ev.date] = map[ev.date] || []).push(ev);
        });
        return map;
    }

    function todayIso() {
        return iso(new Date());
    }

    function eventChip(ev, compact) {
        var isTv = ev.event_type === 'episode';
        var chip = el('button', 'cal-chip' + (isTv ? ' cal-chip-tv' : ' cal-chip-movie'));
        chip.type = 'button';
        chip.setAttribute('data-url', ev.detail_url);
        var aria = ev.title + (isTv
            ? (', season ' + ev.season_number + ' episode ' + ev.episode_number)
            : (', movie release'));
        chip.setAttribute('aria-label', aria);
        if (ev.watched) chip.classList.add('cal-chip-watched');

        var label = el('span', 'cal-chip-label',
            (isTv ? 'S' + ev.season_number + 'E' + ev.episode_number + ' · '
                  : '') + ev.title);
        if (compact) label.classList.add('cal-truncate');

        var tag = el('span', 'cal-chip-type', isTv ? 'TV' : 'Film');
        chip.appendChild(el('span', 'cal-dot ' + (isTv ? 'cal-dot-tv' : 'cal-dot-movie')));
        chip.appendChild(label);
        chip.appendChild(tag);
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
        var dows = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
        dows.forEach(function (dw) {
            grid.appendChild(el('div', 'cal-dow', dw));
        });

        var cursor = new Date(state.rangeStart);
        while (cursor <= state.rangeEnd) {
            var key = iso(cursor);
            var cell = el('div', 'cal-cell' + (key === today ? ' cal-cell-today' : ''));
            cell.appendChild(el('div', 'cal-daynum', String(cursor.getDate())));
            var list = el('div', 'cal-cell-events');
            (byDate[key] || []).slice(0, 3).forEach(function (ev) {
                list.appendChild(eventChip(ev, true));
            });
            var extra = (byDate[key] || []).length - 3;
            if (extra > 0) {
                list.appendChild(el('div', 'cal-more', '+' + extra + ' more'));
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
            var when = new Date(key + 'T00:00:00');
            var head = el('h3', 'cal-agenda-date',
                key === today ? 'Today' : fmtDay(when));
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

    function renderError() {
        var host = state.view === 'month'
            ? document.getElementById('cal-month')
            : document.getElementById('cal-agenda');
        host.replaceChildren();
        var wrap = el('div', 'cal-empty');
        wrap.appendChild(el('p', 'cal-empty-title',
            'Calendar is unavailable right now.'));
        host.appendChild(wrap);
    }

    function renderSummary() {
        var c = (state.meta && state.meta.counts) || { tv: 0, movie: 0 };
        var el1 = document.getElementById('cal-count-tv');
        var el2 = document.getElementById('cal-count-movie');
        if (el1) el1.textContent = c.tv + (c.tv === 1 ? ' episode' : ' episodes');
        if (el2) el2.textContent = c.movie +
            (c.movie === 1 ? ' movie release' : ' movie releases');

        // Coming-up strip: next 3 events from today onward
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
            fmtMonth(new Date());
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

    document.addEventListener('DOMContentLoaded', function () {
        bindGroup('cal-view-group', 'view', function (v) {
            state.view = v;
            render();
        });
        bindGroup('cal-type-group', 'type', function (v) {
            state.type = v;
            load();
        });
        bindGroup('cal-scope-group', 'scope', function (v) {
            state.scope = v;
            load();
        });

        var todayBtn = document.getElementById('cal-quick-today');
        if (todayBtn) {
            todayBtn.addEventListener('click', function () {
                var b = document.querySelector(
                    '#cal-view-group [data-view="agenda"]');
                if (b) b.click();
                load();
            });
        }
        var weekBtn = document.getElementById('cal-quick-week');
        if (weekBtn) {
            weekBtn.addEventListener('click', function () {
                var b = document.querySelector(
                    '#cal-view-group [data-view="agenda"]');
                if (b) b.click();
                load();
            });
        }
        var monthBtn = document.getElementById('cal-quick-month');
        if (monthBtn) {
            monthBtn.addEventListener('click', function () {
                var b = document.querySelector(
                    '#cal-view-group [data-view="month"]');
                if (b) b.click();
                load();
            });
        }

        load();
    });
})();
