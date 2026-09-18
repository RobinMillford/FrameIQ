/**
 * Lists V2 helpers: ranked-list presentation, likes, comments, watched-state
 * badges, clone, bulk selection, keyboard reordering. Vanilla JS, no innerHTML
 * for user-derived data (createElement/textContent only).
 */
(function () {
    'use strict';

    var listEl = document.querySelector('[data-list-id]');
    var listId = listEl ? Number(listEl.dataset.listId) : null;
    if (!listId) return;

    var csrfToken = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    function api(method, url, body) {
        var opts = {
            method: method,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken }
        };
        if (body !== undefined) opts.body = JSON.stringify(body);
        return fetch(url, opts).then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, status: res.status, data: data };
            });
        });
    }

    function toast(msg) {
        var t = document.getElementById('v2-toast');
        if (!t) {
            t = document.createElement('div');
            t.id = 'v2-toast';
            t.setAttribute('role', 'status');
            t.setAttribute('aria-live', 'polite');
            t.className = 'fixed bottom-6 left-1/2 -translate-x-1/2 z-[1300] ' +
                'px-4 py-2 rounded-lg text-sm text-white border ' +
                'border-[var(--line)] bg-[rgba(10,13,18,0.96)] shadow-xl';
            document.body.appendChild(t);
        }
        t.textContent = msg; // textContent: safe for any server text
        t.classList.remove('hidden');
        clearTimeout(t._timer);
        t._timer = setTimeout(function () { t.classList.add('hidden'); }, 2600);
    }

    // =========================================================================
    // RANKED LIST: server renders ranks in Jinja; nothing to compute here.
    // =========================================================================

    // =========================================================================
    // LIKES
    // =========================================================================
    var likeBtn = document.getElementById('like-list-btn');
    if (likeBtn) {
        var likeCountEl = document.getElementById('like-count');
        likeBtn.addEventListener('click', function () {
            var method = likeBtn.getAttribute('aria-pressed') === 'true' ? 'DELETE' : 'POST';
            api(method, '/api/lists/' + listId + '/like').then(function (res) {
                if (!res.ok) { toast(res.data.error || 'Could not update like'); return; }
                likeBtn.setAttribute('aria-pressed', res.data.liked ? 'true' : 'false');
                if (likeCountEl) likeCountEl.textContent = String(res.data.like_count);
            });
        });
    }

    // =========================================================================
    // CLONE
    // =========================================================================
    var cloneBtn = document.getElementById('clone-list-btn');
    if (cloneBtn) {
        cloneBtn.addEventListener('click', function () {
            cloneBtn.disabled = true;
            api('POST', '/api/lists/' + listId + '/clone').then(function (res) {
                cloneBtn.disabled = false;
                if (!res.ok) { toast(res.data.error || 'Could not clone list'); return; }
                toast('List cloned — opening your copy…');
                window.location.href = '/lists/' + res.data.list_id;
            });
        });
    }

    // =========================================================================
    // WATCHED-STATE BADGES (server-rendered; nothing dynamic to compute)
    // =========================================================================

    // =========================================================================
    // BULK SELECTION (owner/editor only; toolbar server-rendered)
    // =========================================================================
    var toolbar = document.getElementById('bulk-toolbar');
    if (toolbar) {
        var selectAll = document.getElementById('bulk-select-all');
        var removeBtn = document.getElementById('bulk-remove-btn');
        var moveSelect = document.getElementById('bulk-move-target');
        var moveBtn = document.getElementById('bulk-move-btn');
        var updateBar = function () {
            var ids = selectedIds();
            removeBtn.disabled = ids.length === 0;
            moveBtn.disabled = ids.length === 0 || !moveSelect.value;
            selectAll.setAttribute('aria-checked',
                selectAll.checked ? 'true' : 'false');
        };
        var selectedIds = function () {
            return Array.prototype.map.call(
                document.querySelectorAll('.bulk-check:checked'),
                function (cb) { return Number(cb.dataset.itemId); });
        };

        selectAll.addEventListener('change', function () {
            document.querySelectorAll('.bulk-check').forEach(function (cb) {
                cb.checked = selectAll.checked;
            });
            updateBar();
        });
        document.addEventListener('change', function (e) {
            if (e.target.classList && e.target.classList.contains('bulk-check')) {
                updateBar();
            }
        });

        removeBtn.addEventListener('click', function () {
            var ids = selectedIds();
            if (!ids.length || !window.confirm('Remove ' + ids.length + ' item(s)?')) return;
            api('POST', '/api/lists/' + listId + '/items/bulk',
                { action: 'remove', item_ids: ids }).then(function (res) {
                    if (!res.ok) { toast(res.data.error || 'Bulk remove failed'); return; }
                    window.location.reload();
                });
        });

        moveBtn.addEventListener('click', function () {
            var ids = selectedIds();
            if (!ids.length || !moveSelect.value) return;
            api('POST', '/api/lists/' + listId + '/items/bulk',
                { action: 'move', item_ids: ids, target_list_id: Number(moveSelect.value) })
                .then(function (res) {
                    if (!res.ok) { toast(res.data.error || 'Bulk move failed'); return; }
                    toast('Moved ' + ids.length + ' item(s)');
                    window.location.reload();
                });
        });
        updateBar();
    }

    // =========================================================================
    // KEYBOARD REORDERING (up/down/top/bottom) + keyboard DnD alternative.
    // Buttons are server-rendered inside each item (editor only).
    // =========================================================================
    function persistOrder() {
        var items = Array.prototype.map.call(
            document.querySelectorAll('.list-item'),
            function (el) { return Number(el.dataset.itemId); });
        if (items.length < 2) return;
        api('PUT', '/api/lists/' + listId + '/reorder',
            { item_order: items }).then(function (res) {
                if (!res.ok) {
                    toast(res.data.error || 'Reorder failed');
                    window.location.reload(); // resync with server state
                    return;
                }
                toast('Order saved');
            });
    }

    function moveItem(itemEl, delta) {
        // delta: -1 up, +1 down, -Infinity top, +Infinity bottom
        var items = Array.prototype.slice.call(
            document.querySelectorAll('.list-item'));
        var index = items.indexOf(itemEl);
        if (index < 0) return;
        var target;
        if (delta === -Infinity) target = 0;
        else if (delta === Infinity) target = items.length - 1;
        else target = Math.min(items.length - 1, Math.max(0, index + delta));
        if (target === index) return;
        items.splice(target, 0, items.splice(index, 1)[0]);
        var anchor = items[index > target ? target : target + 1] || null;
        itemEl.parentNode.insertBefore(itemEl, anchor);
        persistOrder();
        // Re-render rank badges from DOM order (presentation only; the
        // server remains the ordering source of truth).
        var ranked = document.body.dataset.listType === 'ranked';
        if (ranked) {
            items.forEach(function (el, i) {
                var badge = el.querySelector('.rank-badge');
                if (badge) badge.textContent = String(i + 1);
            });
        }
        itemEl.focus();
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-move]');
        if (!btn) return;
        var itemEl = btn.closest('.list-item');
        if (!itemEl) return;
        e.preventDefault();
        var d = btn.dataset.move;
        moveItem(itemEl, d === 'top' ? -Infinity : d === 'bottom' ? Infinity : Number(d));
    });

    // =========================================================================
    // COMMENTS (list page load-more handled server-side via ?page in API;
    // post/delete here)
    // =========================================================================
    var commentForm = document.getElementById('comment-form');
    if (commentForm) {
        commentForm.addEventListener('submit', function (e) {
            e.preventDefault();
            var input = document.getElementById('comment-input');
            var content = (input.value || '').trim();
            if (!content) return;
            api('POST', '/api/lists/' + listId + '/comments', { content: content })
                .then(function (res) {
                    if (!res.ok) { toast(res.data.error || 'Could not post comment'); return; }
                    window.location.reload(); // server is the sole source of truth
                });
        });
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest('[data-delete-comment]');
        if (!btn) return;
        e.preventDefault();
        if (!window.confirm('Delete this comment?')) return;
        api('DELETE', '/api/lists/' + listId + '/comments/' + btn.dataset.deleteComment)
            .then(function (res) {
                if (!res.ok) { toast(res.data.error || 'Could not delete comment'); return; }
                var card = btn.closest('[data-comment-id]');
                if (card) card.remove();
            });
    });

    // =========================================================================
    // MODE SWITCH (editor toolbar)
    // =========================================================================
    var modeForm = document.getElementById('list-mode-form');
    if (modeForm) {
        var modeSelect = document.getElementById('list-mode-select');
        modeSelect.addEventListener('change', function () {
            api('PUT', '/api/lists/' + listId + '/mode',
                { list_type: modeSelect.value }).then(function (res) {
                    if (!res.ok) {
                        toast(res.data.error || 'Could not change mode');
                        return;
                    }
                    toast(res.data.list_type === 'ranked'
                        ? 'Ranked mode on — order is your ranking'
                        : 'Unranked — order kept, numbers hidden');
                    setTimeout(function () { window.location.reload(); }, 700);
                });
        });
    }
})();
