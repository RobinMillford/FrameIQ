/**
 * Shared detail-page actions: Log to Diary + Add to List (movie & TV).
 *
 * Replaces the previously duplicated inline modal scripts on
 * movie_detail.html / tv_detail.html. One module, installed exactly once;
 * media_type flows explicitly from the page via #detail-context (never
 * hardcoded here).
 *
 * Hardening over the old inline scripts:
 *  - CSRF: sets X-CSRFToken explicitly (the base.html fetch patch also
 *    injects it; belt and braces, not a contradictory mechanism).
 *  - Response classification: 201 / 400 / 401 / 403 / 429 / 5xx /
 *    non-JSON (login HTML) / network failure — no more generic
 *    "Failed to log to diary" when the session expired.
 *  - Session-expired detection: fetch auto-follows auth redirects (302 →
 *    /login), surfacing as a 200 text/html response; that is reported as
 *    "Your session has expired. Please log in again." rather than a false
 *    generic error.
 *  - Double-submit guard per action (busy flag + button disabled).
 *  - Zero-list state: actionable "Create a list" link (existing /lists page).
 *  - Duplicate list add (backend 400 'already in the list') → clear
 *    "Already in this list." message; the backend stays authoritative.
 *  - DOM-safe rendering only (textContent/replaceChildren; no innerHTML
 *    for server-controlled strings). List names and titles are content data.
 *  - Modal visibility is set via inline style in addition to the Tailwind
 *    `hidden` class, so dialogs still open/close if the Tailwind Play CDN
 *    fails to load.
 */
(function () {
    'use strict';

    if (window.DetailModals) return; // single initialization guard

    function csrfToken() {
        var el = document.querySelector('meta[name="csrf-token"]');
        return el ? el.content : '';
    }

    function notify(message, kind) {
        var toast = document.createElement('div');
        toast.setAttribute('role', 'status');
        toast.setAttribute('aria-live', 'polite');
        toast.style.cssText = 'position:fixed;left:50%;bottom:1.5rem;transform:translateX(-50%);'
            + 'z-index:60;max-width:90vw;padding:0.65rem 1.1rem;border-radius:0.5rem;'
            + 'color:#fff;font-weight:600;box-shadow:0 10px 25px rgba(0,0,0,.4);'
            + (kind === 'error' ? 'background:#b91c1c;' : 'background:#0e7490;');
        toast.textContent = message; // safe: server/user strings never parsed as HTML
        document.body.appendChild(toast);
        window.setTimeout(function () { toast.remove(); }, 3500);
    }

    var SESSION_MSG = 'Your session has expired. Please log in again.';
    var NETWORK_MSG = 'Network error — check your connection and try again.';
    var RATE_MSG = 'Too many requests — please wait a moment and try again.';
    var SERVER_MSG = 'Server error — please try again later.';

    /**
     * Hardened JSON POST. Returns {ok, status, data?, message} and never
     * throws for HTTP-level problems (network failures return ok:false).
     * `button` is disabled while the request is in flight (double-submit
     * guard) and restored afterwards.
     */
    function apiPost(url, payload, button) {
        if (button) {
            if (button.dataset.busy === '1') {
                return Promise.resolve({ ok: false, status: 0, message: '' });
            }
            button.dataset.busy = '1';
            button.disabled = true;
        }
        var finish = function (result) {
            if (button) {
                button.dataset.busy = '';
                button.disabled = false;
            }
            return result;
        };
        return fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken()
            },
            body: JSON.stringify(payload)
        }).then(function (r) {
            var ctype = r.headers.get('content-type') || '';
            if (ctype.indexOf('application/json') === -1) {
                // HTML (login redirect followed to 200) or an error page.
                var msg = (r.status === 200 || r.status === 302)
                    ? SESSION_MSG
                    : 'Unexpected server response (HTTP ' + r.status + ').';
                return { ok: false, status: r.status, message: msg };
            }
            return r.json().catch(function () { return {}; }).then(function (data) {
                if (r.ok) return { ok: true, status: r.status, data: data };
                var err = (data && data.error) || '';
                var message;
                if (r.status === 401 || r.status === 403) message = SESSION_MSG;
                else if (r.status === 429) message = RATE_MSG;
                else if (r.status >= 500) message = SERVER_MSG;
                else message = err || 'Request failed (HTTP ' + r.status + ').';
                return { ok: false, status: r.status, data: data, message: message,
                         duplicate: err.indexOf('already in the list') !== -1 };
            });
        }).catch(function () {
            return finish({ ok: false, status: 0, message: NETWORK_MSG });
        }).then(finish);
    }

    // ── Modal show/hide (works without the Tailwind CDN) ────────────────────
    var lastFocus = null;
    function showModal(id) {
        var el = document.getElementById(id);
        if (!el) return;
        lastFocus = document.activeElement;
        el.classList.remove('hidden');
        el.style.display = 'flex'; // inline style wins even if `.hidden` is undefined
        var focusable = el.querySelector('input, select, button');
        if (focusable) focusable.focus();
    }
    function hideModal(id) {
        var el = document.getElementById(id);
        if (!el) return;
        el.classList.add('hidden');
        el.style.display = 'none';
        if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        ['diary-modal', 'list-modal'].forEach(function (id) {
            var el = document.getElementById(id);
            if (el && !el.classList.contains('hidden')) hideModal(id);
        });
    });
    // Backdrop click closes (clicks on the dialog content do not bubble to root).
    ['diary-modal', 'list-modal'].forEach(function (id) {
        var el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('click', function (e) {
            if (e.target === el) hideModal(id);
        });
    });

    function requireAuth(userId) {
        // Auth pre-check: send the user to the real login flow (existing
        // convention: /login?next=<current path>) instead of a doomed POST.
        if (userId) return true;
        window.location.href = '/login?next=' + encodeURIComponent(
            window.location.pathname + window.location.search);
        return false;
    }

    // ── Log to Diary ────────────────────────────────────────────────────────
    function initDiary(ctx) {
        var openBtn = document.getElementById('open-diary-modal');
        var modal = document.getElementById('diary-modal');
        if (!openBtn || !modal) return;

        openBtn.addEventListener('click', function () {
            if (!requireAuth(ctx.user_id)) return;
            var dateInput = document.getElementById('diary-date');
            if (dateInput && !dateInput.value) {
                try { dateInput.valueAsDate = new Date(); } catch (e) { /* noop */ }
            }
            showModal('diary-modal');
        });

        var submitBtn = document.getElementById('diary-submit');
        var status = document.getElementById('diary-status');
        if (!submitBtn) return;
        submitBtn.addEventListener('click', function () {
            var dateInput = document.getElementById('diary-date');
            var ratingSel = document.getElementById('diary-rating');
            if (!dateInput || !dateInput.value) {
                if (status) {
                    status.textContent = 'Please select a date.';
                    status.classList.remove('hidden');
                }
                return;
            }
            if (status) status.classList.add('hidden');
            apiPost('/api/diary/log', {
                media_id: ctx.media.id,          // TMDb id (API contract)
                media_type: ctx.media.media_type,
                watched_date: dateInput.value,
                rating: (ratingSel && ratingSel.value) || null
            }, submitBtn).then(function (res) {
                if (res.ok) {
                    hideModal('diary-modal');
                    notify(res.data && res.data.message ? res.data.message
                           : 'Logged to your diary.', 'ok');
                    // Immediate visible state without a page reload.
                    openBtn.textContent = 'In Diary ✓';
                    openBtn.disabled = true;
                    document.dispatchEvent(new CustomEvent('detail:diary-logged',
                        { detail: { media_type: ctx.media.media_type } }));
                } else if (res.message) {
                    if (status) {
                        status.textContent = res.message;
                        status.classList.remove('hidden');
                    }
                    notify(res.message, 'error');
                }
            });
        });

        var cancelBtn = document.getElementById('diary-cancel');
        if (cancelBtn) cancelBtn.addEventListener('click', function () {
            hideModal('diary-modal');
        });
    }

    // ── Add to List ─────────────────────────────────────────────────────────
    function setStatus(el, text) {
        if (!el) return;
        el.textContent = text; // safe for server-controlled strings
        el.classList.toggle('hidden', !text);
    }

    function loadListsInto(ctx, select, zeroBox, status) {
        setStatus(status, 'Loading lists…');
        fetch('/api/users/' + ctx.user_id + '/lists')
            .then(function (r) {
                if (r.status === 401 || r.status === 403) throw { auth: true };
                if (!r.ok) throw { status: r.status };
                return r.json();
            })
            .then(function (data) {
                var lists = (data && data.lists) || [];
                select.replaceChildren();
                if (!lists.length) {
                    select.classList.add('hidden');   // empty dropdown must not look selectable
                    if (zeroBox) zeroBox.classList.remove('hidden');
                    setStatus(status, '');
                    return;
                }
                if (zeroBox) zeroBox.classList.add('hidden');
                select.classList.remove('hidden');
                var placeholder = document.createElement('option');
                placeholder.value = '';
                placeholder.textContent = 'Select a list…';
                select.appendChild(placeholder);
                lists.forEach(function (list) {
                    var opt = document.createElement('option');
                    opt.value = list.id;
                    opt.textContent = list.title; // safe
                    select.appendChild(opt);
                });
                setStatus(status, '');
            })
            .catch(function (err) {
                var msg = (err && err.auth) ? SESSION_MSG
                    : (err && err.status === 429) ? RATE_MSG : SERVER_MSG;
                setStatus(status, msg + ' ');
                var retry = document.createElement('button');
                retry.type = 'button';
                retry.textContent = 'Retry';
                retry.className = 'underline';
                retry.addEventListener('click', function () {
                    loadListsInto(ctx, select, zeroBox, status);
                });
                if (status) status.appendChild(retry);
            });
    }

    function initList(ctx) {
        var openBtn = document.getElementById('open-list-modal');
        var modal = document.getElementById('list-modal');
        if (!openBtn || !modal) return;

        openBtn.addEventListener('click', function () {
            if (!requireAuth(ctx.user_id)) return;
            showModal('list-modal');
            var select = document.getElementById('list-select');
            var zeroBox = document.getElementById('list-zero');
            var status = document.getElementById('list-status');
            loadListsInto(ctx, select, zeroBox, status);
        });

        var addBtn = document.getElementById('list-add');
        var status = document.getElementById('list-status');
        if (!addBtn) return;
        addBtn.addEventListener('click', function () {
            var select = document.getElementById('list-select');
            var listId = select ? select.value : '';
            if (!listId) {
                setStatus(status, 'Please select a list.');
                return;
            }
            setStatus(status, '');
            apiPost('/api/lists/' + listId + '/add', {
                media_id: ctx.media.id,          // TMDb id (API contract)
                media_type: ctx.media.media_type
            }, addBtn).then(function (res) {
                if (res.ok) {
                    hideModal('list-modal');
                    notify('Added to list.', 'ok');
                    openBtn.textContent = 'Manage Lists';
                    document.dispatchEvent(new CustomEvent('detail:list-added',
                        { detail: { media_type: ctx.media.media_type, list_id: Number(listId) } }));
                } else if (res.duplicate) {
                    setStatus(status, 'Already in this list.');
                    notify('Already in this list.', 'ok');
                } else if (res.message) {
                    setStatus(status, res.message);
                    notify(res.message, 'error');
                }
            });
        });

        var cancelBtn = document.getElementById('list-cancel');
        if (cancelBtn) cancelBtn.addEventListener('click', function () {
            hideModal('list-modal');
        });
    }

    function boot() {
        var ctxEl = document.getElementById('detail-context');
        if (!ctxEl) return;
        var ctx;
        try { ctx = JSON.parse(ctxEl.textContent); } catch (e) { return; }
        if (!ctx || !ctx.media) return;
        initDiary(ctx);
        initList(ctx);
        window.DetailModals = { apiPost: apiPost, notify: notify,
                                showModal: showModal, hideModal: hideModal };
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
