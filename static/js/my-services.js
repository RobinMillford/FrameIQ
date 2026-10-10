/**
 * My Services settings (Feature 03).
 *
 * Loads the provider picker for the selected region, toggles selections,
 * and saves via POST /api/me/streaming-services (idempotent replace).
 *
 * Design notes
 * ------------
 * The provider list for a region runs to 40-70 entries. Rendering each as a
 * full-width tile in a 3-column grid produced an enormous, repetitive block
 * that dominated the settings page. This is a searchable compact multi-select:
 *
 *   - a text filter narrows the VISIBLE list only. `selected` is a separate
 *     map keyed by provider_id, so filtering never drops a selection — a
 *     selected service that is filtered out of view is still submitted, and
 *     is still shown as a chip above the list.
 *   - selections are summarised as removable chips with a live count.
 *   - "Select shown" / "Clear all" act on what is currently visible.
 *
 * Selection changes update INCREMENTALLY
 * --------------------------------------
 * Toggling a provider must not rebuild the list: re-rendering removes the
 * focused checkbox from the document and drops keyboard focus to <body>,
 * making the list unusable with Tab/Space. So a toggle mutates `selected`,
 * restyles only the affected row, and refreshes chips + count. The checkbox
 * element the user is interacting with is never replaced.
 *
 * Selection cap
 * -------------
 * MAX_SERVICES mirrors `_MAX_SERVICES` in routes/availability.py. The backend
 * rejects >20 with HTTP 400, so the UI enforces the same ceiling up front
 * rather than letting the user build a set that cannot be saved. The backend
 * stays authoritative; this is a usability guard, not a replacement for it.
 *
 * Behaviour deliberately preserved from the original implementation:
 *   - GET  /api/me/streaming-services            -> {region, services, available_providers}
 *   - GET  /api/me/streaming-services?region=CODE
 *   - POST /api/me/streaming-services            -> {services, region}, idempotent replace
 *   - changing the REGION reloads the list and RESETS the selection to the
 *     services already saved for that region.
 *   - `data-provider` checkbox attributes and delegated `change` handling, so
 *     `#services-list` remains the single source of truth for toggling.
 */
(function () {
    'use strict';

    var listEl = document.getElementById('services-list');
    if (!listEl) return;

    var regionEl = document.getElementById('streaming-region');
    var saveBtn = document.getElementById('save-services');
    var statusEl = document.getElementById('services-status');
    var errorEl = document.getElementById('services-error');
    var searchEl = document.getElementById('services-search');
    var chipsEl = document.getElementById('services-chips');
    var countEl = document.getElementById('services-selected-count');
    var searchStatusEl = document.getElementById('services-search-status');
    var selectShownBtn = document.getElementById('services-select-visible');
    var clearBtn = document.getElementById('services-clear');
    var limitEl = document.getElementById('services-limit');

    // MUST stay in sync with routes/availability.py::_MAX_SERVICES.
    // tests/test_edit_profile_ui.py asserts these two agree.
    var MAX_SERVICES = 20;

    var selected = {};        // provider_id -> true   (the SUBMITTED set)
    var providers = [];       // every provider for the current region
    var byId = {};            // provider_id -> provider
    var query = '';           // current search text
    var saving = false;

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = String(s == null ? '' : s);
        return d.innerHTML;
    }

    function showError(msg) {
        if (!errorEl) return;
        errorEl.textContent = msg;
        errorEl.classList.remove('hidden');
    }

    function clearError() {
        if (errorEl) errorEl.classList.add('hidden');
    }

    function showLimit(msg) {
        if (!limitEl) return;
        limitEl.textContent = msg;
        limitEl.classList.remove('hidden');
    }

    function clearLimit() {
        if (!limitEl) return;
        limitEl.textContent = '';
        limitEl.classList.add('hidden');
    }

    function logoHtml(p) {
        if (!p || !p.logo) return '<span class="text-xs">' + esc(p && p.name) + '</span>';
        return '<img src="https://image.themoviedb.org/t/p/w92' + esc(p.logo) +
               '" alt="" aria-hidden="true" loading="lazy" ' +
               'style="height:1rem;width:auto;border-radius:.2rem;flex:none" ' +
               'onerror="this.outerHTML=\'<span class=\\\'text-xs\\\'>' +
               esc(p.name) + '</span>\'">';
    }

    function selectedIds() {
        return Object.keys(selected).map(Number).sort(function (a, b) { return a - b; });
    }

    function selectedCount() {
        return Object.keys(selected).length;
    }

    function nameFor(id) {
        var p = byId[id];
        return p ? p.name : ('Service #' + id);
    }

    function matches(p, q) {
        if (!q) return true;
        return String(p.name || '').toLowerCase().indexOf(q) !== -1;
    }

    function atCap() {
        return selectedCount() >= MAX_SERVICES;
    }

    function limitMessage() {
        return 'You can select up to ' + MAX_SERVICES +
               ' services. Clear some selections first.';
    }

    function overCapMessage() {
        return 'You have ' + selectedCount() + ' services selected, but the limit is ' +
               MAX_SERVICES + '. Remove ' + (selectedCount() - MAX_SERVICES) +
               (selectedCount() - MAX_SERVICES === 1 ? ' service' : ' services') +
               ' before saving.';
    }

    /* ── row styling: update ONE row, never rebuild the list ───────────── */

    function applyRowState(input) {
        if (!input) return;
        var row = input.closest('.svc-row');
        if (!row) return;
        row.setAttribute('data-selected', input.checked ? 'true' : 'false');
    }

    function syncAllRowStates() {
        var boxes = listEl.querySelectorAll('input[data-provider]');
        for (var i = 0; i < boxes.length; i++) applyRowState(boxes[i]);
    }

    /* ── chips + count ──────────────────────────────────────────────────── */

    function renderChips() {
        var ids = selectedIds();
        var n = ids.length;
        if (countEl) {
            countEl.textContent = n === 0
                ? 'No services selected'
                : (n + (n === 1 ? ' service selected' : ' services selected') +
                   (n >= MAX_SERVICES ? ' (limit ' + MAX_SERVICES + ')' : ''));
        }
        if (chipsEl) {
            if (!n) {
                chipsEl.innerHTML =
                    '<li class="text-xs text-gray-400">Nothing selected yet — pick from the list below.</li>';
            } else {
                chipsEl.innerHTML = ids.map(function (id) {
                    var p = byId[id];
                    return '<li class="inline-flex items-center gap-1.5 rounded border ' +
                           'border-[var(--accent)]/50 bg-[var(--accent)]/10 px-2 py-1 text-xs">' +
                           logoHtml(p) +
                           '<span class="max-w-[10rem] truncate">' + esc(nameFor(id)) + '</span>' +
                           '<button type="button" data-remove-provider="' + id + '" ' +
                           'aria-label="Remove ' + esc(nameFor(id)) + '" ' +
                           'class="ml-0.5 rounded px-1 leading-none text-gray-400 hover:text-white focus:outline-none focus:ring-2 focus:ring-[var(--accent)]">' +
                           '&times;</button></li>';
                }).join('');
            }
        }
        // Bulk button reflects remaining capacity against what is visible.
        if (selectShownBtn) {
            var q = query.trim().toLowerCase();
            var addable = providers.filter(function (p) {
                return matches(p, q) && !selected[p.id];
            }).length;
            selectShownBtn.disabled = addable === 0 || (selectedCount() + addable) > MAX_SERVICES;
            selectShownBtn.title = selectShownBtn.disabled && addable > 0
                ? limitMessage()
                : 'Select the services currently shown by the filter';
        }
    }

    /* ── option list (filtered, never mutates `selected`) ───────────────── */

    function renderList() {
        var q = query.trim().toLowerCase();
        var visible = providers.filter(function (p) { return matches(p, q); });

        if (!providers.length) {
            listEl.innerHTML =
                '<p class="py-3 text-xs text-gray-400">No provider data for this region yet.</p>';
            if (searchStatusEl) searchStatusEl.textContent = '';
            return;
        }

        listEl.innerHTML = visible.length ? visible.map(function (p) {
            var on = !!selected[p.id];
            return '<label class="svc-row" data-selected="' + (on ? 'true' : 'false') + '">' +
                   '<input type="checkbox" data-provider="' + p.id + '" ' +
                   'class="flex-none accent-[var(--accent)]" ' + (on ? 'checked' : '') + '>' +
                   '<span class="flex min-w-0 items-center gap-2">' + logoHtml(p) +
                   '<span class="truncate text-xs">' + esc(p.name) + '</span></span>' +
                   '</label>';
        }).join('') :
            '<p class="py-3 text-xs text-gray-400">No services match “' +
            esc(query.trim()) + '”. Your existing selection is unaffected.</p>';

        if (searchStatusEl) {
            searchStatusEl.textContent = q
                ? (visible.length + ' of ' + providers.length + ' services shown')
                : (providers.length + ' services available');
        }
    }

    function renderAll() {
        renderChips();
        renderList();
    }

    /* ── data ───────────────────────────────────────────────────────────── */

    function load(region) {
        listEl.innerHTML = '<p class="py-3 text-xs text-gray-400">Loading services…</p>';
        if (chipsEl) chipsEl.innerHTML = '';
        clearError();
        clearLimit();
        fetch('/api/me/streaming-services?region=' + encodeURIComponent(region))
            .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
            .then(function (data) {
                // Region switch RESETS the selection to what is saved for the
                // new region — unchanged from the original implementation.
                selected = {};
                (data.services || []).forEach(function (id) { selected[id] = true; });
                applyProviders(data.available_providers || []);
                if (selectedCount() > MAX_SERVICES) showLimit(overCapMessage());
            })
            .catch(function () {
                listEl.innerHTML = '<p class="py-3 text-xs text-gray-400">Could not load services.</p>';
                showError('Could not load streaming services. Please try again later.');
            });
    }

    function applyProviders(list) {
        providers = list;
        byId = {};
        providers.forEach(function (p) { byId[p.id] = p; });
        renderAll();
    }

    /* ── events ─────────────────────────────────────────────────────────── */

    // Single delegated toggle source of truth (unchanged contract).
    // NOTE: no re-render here — that would destroy the focused checkbox.
    listEl.addEventListener('change', function (e) {
        var input = e.target.closest('input[data-provider]');
        if (!input) return;
        var id = parseInt(input.getAttribute('data-provider'), 10);

        if (input.checked) {
            if (!selected[id] && atCap()) {
                // Reject the 21st selection: revert the control and leave every
                // existing selection untouched.
                input.checked = false;
                applyRowState(input);
                showLimit(limitMessage());
                return;
            }
            selected[id] = true;
        } else {
            delete selected[id];
        }

        clearLimit();
        applyRowState(input);   // restyle THIS row only
        renderChips();          // chips + count + bulk-button capacity
    });

    // Chip removal.
    if (chipsEl) {
        chipsEl.addEventListener('click', function (e) {
            var btn = e.target.closest('button[data-remove-provider]');
            if (!btn) return;
            var id = parseInt(btn.getAttribute('data-remove-provider'), 10);
            delete selected[id];
            clearLimit();
            // Only the matching row is restyled; the list itself is untouched.
            var box = listEl.querySelector('input[data-provider="' + id + '"]');
            if (box) {
                box.checked = false;
                applyRowState(box);
            }
            renderChips();
            // Only move focus when the focused element was actually destroyed
            // (the chip button itself). Otherwise leave focus alone.
            (chipsEl.querySelector('button[data-remove-provider]') || box ||
             searchEl || saveBtn).focus();
        });
    }

    // Search: filters the visible list ONLY. `selected` is untouched.
    if (searchEl) {
        searchEl.addEventListener('input', function () {
            query = searchEl.value;
            renderList();        // list rebuild is correct here: the user asked for it
            renderChips();       // recompute bulk capacity for the new filter
        });
    }

    if (selectShownBtn) {
        selectShownBtn.addEventListener('click', function () {
            var q = query.trim().toLowerCase();
            var addable = providers.filter(function (p) {
                return matches(p, q) && !selected[p.id];
            });
            // Atomic: either the whole visible set fits, or nothing is applied.
            if (selectedCount() + addable.length > MAX_SERVICES) {
                showLimit(limitMessage());
                return;
            }
            addable.forEach(function (p) { selected[p.id] = true; });
            clearLimit();
            renderAll();
        });
    }

    if (clearBtn) {
        clearBtn.addEventListener('click', function () {
            selected = {};
            clearLimit();
            renderAll();
            (searchEl || saveBtn).focus();
        });
    }

    regionEl.addEventListener('change', function () {
        load(regionEl.value);
    });

    saveBtn.addEventListener('click', function () {
        if (saving) return;
        var ids = selectedIds();
        // Front-end guard; the backend remains authoritative.
        if (ids.length > MAX_SERVICES) {
            showLimit(overCapMessage());
            return;
        }
        saving = true;
        saveBtn.disabled = true;
        statusEl.textContent = 'Saving…';
        clearError();
        clearLimit();

        fetch('/api/me/streaming-services', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': (document.querySelector('meta[name="csrf-token"]') || {}).content || ''
            },
            body: JSON.stringify({ services: ids, region: regionEl.value })
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (err) { throw new Error(err.error || ('http ' + r.status)); });
                return r.json();
            })
            .then(function (data) {
                selected = {};
                (data.services || []).forEach(function (id) { selected[id] = true; });
                renderAll();
                statusEl.textContent = 'Saved ✓';
                setTimeout(function () { statusEl.textContent = ''; }, 2500);
            })
            .catch(function (err) {
                statusEl.textContent = '';
                showError('Save failed: ' + err.message);
            })
            .then(function () { saving = false; saveBtn.disabled = false; });
    });

    // Init: preselect region from saved preference, then load picker.
    fetch('/api/me/streaming-services')
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (data) {
            regionEl.value = data.region;
            selected = {};
            (data.services || []).forEach(function (id) { selected[id] = true; });
            applyProviders(data.available_providers || []);
            // Legacy/over-cap saved data is never silently truncated: the user
            // is told and can remove chips until it is savable.
            if (selectedCount() > MAX_SERVICES) showLimit(overCapMessage());
        })
        .catch(function () { load(regionEl.value); });
})();
