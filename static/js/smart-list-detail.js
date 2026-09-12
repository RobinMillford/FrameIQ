/**
 * Smart List detail page (Feature 05).
 *
 * Fetches /api/smart-lists/<id>/results once per load (plus explicit
 * pagination via "Load more" and a refresh after edits) — no polling, no
 * timers, no background workers. Cards link to the existing detail pages;
 * all text is injected via textContent.
 */
(function () {
    'use strict';

    var page = window.SMART_LIST_PAGE || {};
    var listId = page.id;
    if (!listId) return;

    var grid = document.getElementById('sl-grid');
    var empty = document.getElementById('sl-empty');
    var loading = document.getElementById('sl-loading');
    var errorBox = document.getElementById('sl-error');
    var countEl = document.getElementById('sl-count');
    var moreWrap = document.getElementById('sl-more-wrap');
    var moreBtn = document.getElementById('sl-more');

    var state = { page: 1, pages: 1, total: 0 };

    function pluralize(n, word) {
        return n === 1 ? ('1 ' + word) : (n + ' ' + word + 's');
    }

    function show(el, on) {
        el.classList.toggle('hidden', !on);
    }

    function renderCard(item) {
        var card = document.createElement('a');
        card.href = item.detail_url;
        card.className = 'group rounded-xl overflow-hidden border border-[var(--line)] bg-[var(--surface)] hover:border-[var(--accent)] transition-colors focus-visible:outline focus-visible:ring-2 focus-visible:ring-[var(--accent)]';

        var img = document.createElement('img');
        img.src = item.poster_url || '/static/images/no-poster.svg';
        img.alt = '';
        img.loading = 'lazy';
        img.className = 'w-full aspect-[2/3] object-cover';
        img.onerror = function () { this.src = '/static/images/no-poster.svg'; };
        card.appendChild(img);

        var body = document.createElement('div');
        body.className = 'p-3';

        var title = document.createElement('h3');
        title.className = 'text-sm font-semibold text-[var(--text-bright)] line-clamp-2 group-hover:text-[var(--accent)]';
        title.textContent = item.title;
        body.appendChild(title);

        var meta = document.createElement('p');
        meta.className = 'text-xs text-[var(--text-mid)] mt-1';
        var bits = [];
        if (item.year) bits.push(item.year);
        if (item.media_type === 'tv') bits.push('TV');
        if (typeof item.rating === 'number' && item.rating > 0) {
            bits.push('★ ' + item.rating.toFixed(1));
        }
        meta.textContent = bits.join(' · ');
        body.appendChild(meta);

        if (item.priority && item.priority !== 'medium') {
            var p = document.createElement('p');
            p.className = 'text-xs mt-1 ' +
                (item.priority === 'high' ? 'text-orange-400' : 'text-[var(--text-mid)]');
            p.textContent = item.priority === 'high' ? '🔥 High priority' : '💤 Low priority';
            body.appendChild(p);
        }

        card.appendChild(body);
        return card;
    }

    function renderPayload(payload, append) {
        state.page = payload.page;
        state.pages = payload.pages;
        state.total = payload.total;

        countEl.textContent = pluralize(payload.total, 'result');

        var items = payload.items || [];
        if (!append) grid.innerHTML = '';
        items.forEach(function (item) { grid.appendChild(renderCard(item)); });

        show(grid, items.length > 0);
        show(empty, payload.total === 0);
        show(moreWrap, payload.page < payload.pages);
        show(errorBox, false);
    }

    function load(p, append) {
        p = p || 1;
        show(loading, !append);
        show(errorBox, false);
        if (!append) { show(grid, false); show(empty, false); }

        fetch('/api/smart-lists/' + listId + '/results?page=' + p + '&per_page=24')
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (payload) {
                renderPayload(payload, append);
            })
            .catch(function () {
                show(loading, false);
                show(errorBox, true);
            })
            .finally(function () { show(loading, false); });
    }

    // ── Edit modal ────────────────────────────────────────────────────────
    var modal = document.getElementById('sl-edit-modal');
    var cfg = page.config || {};
    var filters = cfg.filters || {};

    function setVal(id, v) {
        var el = document.getElementById(id);
        if (el) el.value = (v === undefined || v === null) ? '' : v;
    }
    function getVal(id) {
        var el = document.getElementById(id);
        return el ? el.value.trim() : '';
    }

    function openEdit() {
        setVal('sl-name', cfg.name);
        setVal('sl-desc', cfg.description);
        setVal('sl-scope', cfg.scope);
        setVal('sl-sort', cfg.sort);
        setVal('sl-watch-state', filters.watch_state || '');
        setVal('sl-runtime', filters.runtime || '');
        setVal('sl-services', filters.services || '');
        setVal('sl-min-rating', filters.min_rating || '');
        modal.classList.remove('hidden');
        modal.classList.add('flex');
    }

    function closeEdit() {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }

    document.getElementById('sl-edit-btn').addEventListener('click', openEdit);
    document.getElementById('sl-edit-cancel').addEventListener('click', closeEdit);
    modal.addEventListener('click', function (e) {
        if (e.target === modal) closeEdit();
    });

    document.getElementById('sl-edit-save').addEventListener('click', function () {
        var name = getVal('sl-name');
        if (!name) { window.frameToast && window.frameToast('Name is required', 'error'); return; }

        var nextFilters = {};
        var ws = getVal('sl-watch-state');
        if (ws) nextFilters.watch_state = ws;
        var rt = getVal('sl-runtime');
        if (rt) nextFilters.runtime = rt;
        var sv = getVal('sl-services');
        if (sv) nextFilters.services = sv;
        var mr = getVal('sl-min-rating');
        if (mr) nextFilters.min_rating = parseFloat(mr);

        var payload = {
            name: name,
            description: getVal('sl-desc'),
            scope: getVal('sl-scope') || 'watchlist',
            sort: getVal('sl-sort') || 'date_added',
            filters: nextFilters
        };

        fetch('/api/smart-lists/' + listId, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }).then(function (r) {
            return r.json().then(function (data) { return { ok: r.ok, data: data }; });
        }).then(function (res) {
            if (!res.ok) {
                window.frameToast && window.frameToast(res.data.error || 'Could not save', 'error');
                return;
            }
            cfg = res.data;
            filters = cfg.filters || {};
            closeEdit();
            renderRules();
            load(1, false);
            window.frameToast && window.frameToast('Smart List updated', 'success');
        }).catch(function () {
            window.frameToast && window.frameToast('Could not save changes', 'error');
        });
    });

    // ── Delete ────────────────────────────────────────────────────────────
    document.getElementById('sl-delete-btn').addEventListener('click', function () {
        if (!window.confirm('Delete this Smart List? Your data is not affected.')) return;
        fetch('/api/smart-lists/' + listId, { method: 'DELETE' })
            .then(function (r) {
                if (r.ok) { window.location.href = '/lists'; }
                else { window.frameToast && window.frameToast('Could not delete', 'error'); }
            })
            .catch(function () {
                window.frameToast && window.frameToast('Could not delete', 'error');
            });
    });

    // ── Rules chips (updated after edit) ─────────────────────────────────
    function renderRules() {
        var wrap = document.getElementById('sl-rules');
        wrap.innerHTML = '';
        (cfg.rules || page.rules || []).forEach(function (rule) {
            var chip = document.createElement('span');
            chip.className = 'text-xs px-2.5 py-1 rounded-full bg-[var(--surface)] border border-[var(--line)] text-[var(--text-mid)]';
            chip.textContent = rule;
            wrap.appendChild(chip);
        });
    }

    // ── Pagination + retry ────────────────────────────────────────────────
    moreBtn.addEventListener('click', function () {
        if (state.page < state.pages) load(state.page + 1, true);
    });
    document.getElementById('sl-retry').addEventListener('click', function () {
        load(1, false);
    });

    load(1, false);
})();
