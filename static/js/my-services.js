/**
 * My Services settings (Feature 03).
 *
 * Loads the provider picker for the selected region, toggles selections,
 * and saves via POST /api/me/streaming-services (idempotent replace).
 */
(function () {
    'use strict';

    var listEl = document.getElementById('services-list');
    if (!listEl) return;

    var regionEl = document.getElementById('streaming-region');
    var saveBtn = document.getElementById('save-services');
    var statusEl = document.getElementById('services-status');
    var errorEl = document.getElementById('services-error');
    var selected = {};   // provider_id -> true
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

    function logoHtml(p) {
        if (!p.logo) return esc(p.name);
        return '<img src="https://image.tmdb.org/t/p/w92' + esc(p.logo) +
               '" alt="' + esc(p.name) + '" style="height:1.25rem;width:auto;border-radius:.25rem" ' +
               'onerror="this.outerHTML=\'<span>' + esc(p.name) + '</span>\'">';
    }

    function renderPicker(providers) {
        if (!providers.length) {
            listEl.innerHTML = '<p class="col-span-full text-gray-400">No provider data for this region yet.</p>';
            return;
        }
        listEl.innerHTML = providers.map(function (p) {
            var on = !!selected[p.id];
            return '<label class="flex items-center gap-2 bg-gray-800/60 border border-gray-700 rounded px-3 py-2 cursor-pointer hover:border-gray-500">' +
                   '<input type="checkbox" data-provider="' + p.id + '" ' + (on ? 'checked' : '') + '>' +
                   '<span class="flex items-center gap-2">' + logoHtml(p) + '</span>' +
                   '</label>';
        }).join('');
    }

    function load(region) {
        listEl.innerHTML = '<p class="col-span-full text-gray-400">Loading services…</p>';
        errorEl && errorEl.classList.add('hidden');
        fetch('/api/me/streaming-services?region=' + encodeURIComponent(region))
            .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
            .then(function (data) {
                selected = {};
                (data.services || []).forEach(function (id) { selected[id] = true; });
                renderPicker(data.available_providers || []);
            })
            .catch(function () {
                listEl.innerHTML = '<p class="col-span-full text-gray-400">Could not load services.</p>';
                showError('Could not load streaming services. Please try again later.');
            });
    }

    listEl.addEventListener('change', function (e) {
        var input = e.target.closest('input[data-provider]');
        if (!input) return;
        var id = parseInt(input.getAttribute('data-provider'), 10);
        if (input.checked) selected[id] = true;
        else delete selected[id];
    });

    regionEl.addEventListener('change', function () {
        load(regionEl.value);
    });

    saveBtn.addEventListener('click', function () {
        if (saving) return;
        saving = true;
        saveBtn.disabled = true;
        statusEl.textContent = 'Saving…';
        errorEl && errorEl.classList.add('hidden');

        fetch('/api/me/streaming-services', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': (document.querySelector('meta[name="csrf-token"]') || {}).content || ''
            },
            body: JSON.stringify({ services: Object.keys(selected).map(Number), region: regionEl.value })
        })
            .then(function (r) {
                if (!r.ok) return r.json().then(function (err) { throw new Error(err.error || ('http ' + r.status)); });
                return r.json();
            })
            .then(function (data) {
                selected = {};
                (data.services || []).forEach(function (id) { selected[id] = true; });
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
            renderPicker(data.available_providers || []);
        })
        .catch(function () { load(regionEl.value); });
})();
