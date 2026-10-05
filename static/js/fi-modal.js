/**
 * FrameIQ shared modal engine (static/js/fi-modal.js).
 *
 * ONE infrastructure for every overlay dialog: Add to List, Log to Diary,
 * and any future modal. Exposed as window.FiModal.
 *
 * Why it exists
 * -------------
 * The detail-page modals were included INSIDE
 * `<main class="pt-[72px] relative z-[2] page-enter">` (templates/base.html).
 * `.page-enter` is `animation: page-enter .35s ... both`, whose `to`
 * keyframe sets `transform: translateY(0)`. With fill-mode `both` that
 * computed transform stays applied after the animation finishes, and any
 * non-`none` transform on an ancestor makes that ancestor the containing
 * block for `position: fixed` descendants. So `position: fixed; inset: 0`
 * resolved against <main> (~9000px tall) instead of the viewport: the
 * backdrop stretched down the whole page and the dialog rendered inline
 * in page flow, after which the browser scrolled the focused input into
 * view. Measured: opening the modal moved window.scrollY from 0 to 3555.
 *
 * The fix is structural, not cosmetic: every modal is MOVED into a
 * body-level portal (#fi-modal-root) on first open. No transformed,
 * filtered, or stacking-context ancestor can ever contain it again, and
 * the modal can never contribute to document height.
 *
 * Behaviour contract
 * ------------------
 *  - State model: closed / opening / open / closing (data-state on both
 *    the backdrop and the dialog). `display` is never toggled, so
 *    transitions always run.
 *  - Animation: transform + opacity only, driven by CSS in modals.css.
 *  - Scroll lock: body is position:fixed with top = -scrollY; the exact
 *    original scroll offset is restored on close.
 *  - Focus: moved into the dialog on open, trapped while open, returned
 *    to the trigger on close. Hidden dialogs are inert + aria-hidden so
 *    their controls are never tabbable.
 *  - ESC closes; backdrop click closes; clicks inside the dialog do not.
 *  - Listeners are installed ONCE on the singleton root (delegation),
 *    so rapid open/close cannot accumulate handlers.
 *  - Only one modal is open at a time; opening another closes the first.
 */
(function () {
    'use strict';

    if (window.FiModal) return;

    var ROOT_ID = 'fi-modal-root';
    var ANIM_MS = 320;          // must match --dur-panel in modals.css
    var FOCUSABLE = [
        'a[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
        'select:not([disabled])', 'textarea:not([disabled])', '[tabindex]:not([tabindex="-1"])'
    ].join(',');

    var root = null;
    var current = null;        // {id, el, dialog, trigger, closing}
    var listenersBound = false;
    var lock = null;           // {y, prevPosition, prevTop, ...}

    function prefersReducedMotion() {
        return window.matchMedia
            && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    }

    function getRoot() {
        if (root && root.isConnected) return root;
        root = document.getElementById(ROOT_ID);
        if (!root) {
            root = document.createElement('div');
            root.id = ROOT_ID;
            root.className = 'fi-modal-root';
        }
        // The portal is always the last child of <body>: above <main>, the
        // header, heroes and cards, and nothing else can trap it.
        document.body.appendChild(root);
        bindListeners();
        return root;
    }

    /* ── Scroll lock ──────────────────────────────────────────────────
       position:fixed + top:-y is the only technique that also behaves on
       iOS Safari (overflow:hidden alone lets the page rubber-band and
       loses the offset). We snapshot every property we touch and restore
       exactly what was there, including "was not locked". */
    function lockScroll() {
        if (lock) return;
        var body = document.body;
        var y = window.scrollY || window.pageYOffset || 0;
        var cs = window.getComputedStyle(body);
        lock = {
            y: y,
            position: body.style.position,
            top: body.style.top,
            left: body.style.left,
            right: body.style.right,
            width: body.style.width,
            overflow: cs.overflow
        };
        body.style.position = 'fixed';
        body.style.top = (-y) + 'px';
        body.style.left = '0';
        body.style.right = '0';
        body.style.width = '100%';
        body.classList.add('fi-modal-scroll-locked');
    }

    function unlockScroll() {
        if (!lock) return;
        var body = document.body;
        var y = lock.y;
        body.style.position = lock.position;
        body.style.top = lock.top;
        body.style.left = lock.left;
        body.style.right = lock.right;
        body.style.width = lock.width;
        body.classList.remove('fi-modal-scroll-locked');
        lock = null;
        // Restore the exact offset BEFORE paint so the user never sees the
        // top of the page flash.
        window.scrollTo(0, y);
    }

    /* ── Focus helpers ────────────────────────────────────────────────
       A fixed-position backdrop sits at the viewport origin, so focusing
       a control inside it can trigger a browser scroll. We keep the lock
       in place and re-pin afterwards as a belt-and-braces measure. */
    function focusables(dialog) {
        return Array.prototype.filter.call(
            dialog.querySelectorAll(FOCUSABLE),
            function (el) {
                return el.offsetWidth > 0 || el.offsetHeight > 0 ||
                       el.getClientRects().length > 0;
            });
    }

    function focusFirst(dialog) {
        var list = focusables(dialog);
        if (!list.length) { dialog.focus(); return; }
        // Honour data-fi-autofocus, but only when that control is actually
        // rendered. The list form's <select> is hidden in the zero-list
        // state, and focusing a hidden element silently leaves focus on
        // <body> — outside the dialog, which breaks the focus trap.
        var preferred = dialog.querySelector('[data-fi-autofocus]');
        if (preferred) {
            var visible = focusables(preferred.parentElement || dialog)
                .indexOf(preferred) !== -1;
            if (visible) { preferred.focus(); return; }
        }
        list[0].focus();
    }

    function trapFocus(e) {
        if (e.key !== 'Tab' || !current) return;
        var list = focusables(current.dialog);
        if (!list.length) { e.preventDefault(); return; }
        var first = list[0];
        var last = list[list.length - 1];
        var active = document.activeElement;
        if (e.shiftKey && (active === first || !current.dialog.contains(active))) {
            e.preventDefault();
            last.focus();
        } else if (!e.shiftKey && (active === last || !current.dialog.contains(active))) {
            e.preventDefault();
            first.focus();
        }
    }

    function bindListeners() {
        if (listenersBound || !root) return;
        listenersBound = true;

        // Delegated: ONE keydown + ONE click handler for every modal.
        document.addEventListener('keydown', function (e) {
            if (!current) return;
            if (e.key === 'Escape') {
                e.preventDefault();
                close(current.id);
            } else {
                trapFocus(e);
            }
        });

        root.addEventListener('click', function (e) {
            if (!current) return;
            // Backdrop click closes; a click that started inside the
            // dialog (including on padding/whitespace) must NOT close.
            if (e.target === current.el && !current.dialog.contains(e.target)) {
                close(current.id);
            }
        });

        // Keep the lock pinned: some engines scroll the fixed body on
        // focus or on overscroll at the top of the modal.
        root.addEventListener('focusin', function () {
            if (!current || !lock) return;
            var y = lock.y;
            var body = document.body;
            if (body.style.position === 'fixed') body.style.top = (-y) + 'px';
        });
    }

    function setState(parts, state) {
        parts.forEach(function (el) {
            if (el) el.setAttribute('data-state', state);
        });
    }

    /** Move an in-page modal definition into the body-level portal. */
    function adopt(el) {
        var portal = getRoot();
        if (el.parentElement !== portal) portal.appendChild(el);
    }

    /**
     * Open a modal by element id.
     * @param {string} id
     * @param {Element} [trigger] element focus returns to on close
     */
    function open(id, trigger) {
        var el = document.getElementById(id);
        if (!el) return null;

        // One modal at a time — opening another closes the open one
        // cleanly instead of stacking two overlays.
        if (current && current.id !== id) close(current.id, true);

        el.classList.remove('fi-modal-hidden');
        var dialog = el.querySelector('[role="dialog"]') || el.firstElementChild;
        if (!dialog) return null;

        adopt(el);

        // The dialog itself must be reachable as a last-resort focus
        // target (e.g. the list form with no lists hides every control).
        if (!dialog.hasAttribute('tabindex')) dialog.setAttribute('tabindex', '-1');

        // Hide any other modal that shares the portal.
        Array.prototype.forEach.call(
            root.querySelectorAll('.fi-modal-backdrop'), function (other) {
                if (other !== el) {
                    other.setAttribute('data-state', 'closed');
                    other.setAttribute('aria-hidden', 'true');
                    other.inert = true;
                }
            });

        current = {
            id: id, el: el, dialog: dialog,
            trigger: trigger || document.activeElement
        };

        el.inert = false;
        el.removeAttribute('aria-hidden');
        el.classList.add('fi-modal-open');
        el.style.display = '';        // CSS owns display; never display:none

        lockScroll();
        setState([el, dialog], 'opening');
        // Force a reflow so the transition actually runs from the
        // initial keyframe instead of snapping.
        void el.offsetWidth;
        setState([el, dialog], 'open');

        focusFirst(dialog);
        return dialog;
    }

    function isOpen(id) {
        if (!current) return false;
        if (!id) return true;
        return current.id === id;
    }

    var closeHandlers = {};

    /**
     * Register a teardown callback for a modal id.
     *
     * The engine owns ESC and backdrop clicks, so a consumer can no longer
     * clean up from its own key handler — the trailer dialog must blank
     * its iframe src when the shared engine closes it, or the video keeps
     * playing behind the restored page.
     */
    function onClose(id, fn) {
        if (!closeHandlers[id]) closeHandlers[id] = [];
        closeHandlers[id].push(fn);
    }

    function runCloseHandlers(id) {
        var list = closeHandlers[id] || [];
        list.forEach(function (fn) {
            try { fn(); } catch (e) { /* teardown must not break closing */ }
        });
    }

    function finalize(el, dialog, trigger) {
        runCloseHandlers(el.id);
        el.setAttribute('data-state', 'closed');
        dialog.setAttribute('data-state', 'closed');
        el.setAttribute('aria-hidden', 'true');
        // inert removes the subtree from the tab order without
        // display:none, so the close transition is never blocked.
        el.inert = true;
        el.classList.remove('fi-modal-open');
        // Focus FIRST, while <body> is still position:fixed. Focusing an
        // element that sits above the current scroll offset makes the
        // engine scroll it into view; doing that after the unlock would
        // visibly drag the page up to the trigger (measured: 2400 -> 364).
        if (trigger && trigger.focus && trigger.isConnected) {
            trigger.focus();
        }
        unlockScroll();
    }

    /** Close a modal. Rapid calls are idempotent and never race. */
    function close(id, immediate) {
        var entry = current;
        if (!entry || (id && entry.id !== id)) return;
        if (entry.closing) return;

        var el = entry.el;
        var dialog = entry.dialog;
        var trigger = entry.trigger;

        entry.closing = true;
        current = null;              // drop immediately: no double-close
        setState([el, dialog], 'closing');

        var done = function () {
            finalize(el, dialog, trigger);
        };

        if (immediate || prefersReducedMotion()) {
            done();
        } else {
            // Guarded by the closing flag + transitionend listener so a
            // rapid open/close/open sequence can't leave a stale timer.
            var finished = false;
            var finish = function () {
                if (finished) return;
                finished = true;
                el.removeEventListener('transitionend', onEnd);
                clearTimeout(timer);
                done();
            };
            var onEnd = function (ev) {
                if (ev.target === dialog) finish();
            };
            el.addEventListener('transitionend', onEnd);
            var timer = setTimeout(finish, ANIM_MS + 90);
        }
    }

    window.FiModal = {
        open: open,
        close: close,
        isOpen: isOpen,
        onClose: onClose,
        rootId: ROOT_ID,
        /**
         * Re-assert focus after a form mutates its own DOM while open.
         *
         * Add to List focuses the <select> on open and only later learns
         * (async) that the user has zero lists, at which point it hides
         * that control. Hiding the focused element makes the engine blur
         * it, dropping focus onto <body> — outside the dialog, which
         * breaks the focus trap and the "focus stays inside" guarantee.
         */
        ensureFocus: function (id) {
            if (!current || (id && current.id !== id)) return;
            var active = document.activeElement;
            // getClientRects() forces a layout flush: hiding the focused
            // control does not blur it synchronously, so containment alone
            // is not enough — the element must still be rendered.
            if (active && current.dialog.contains(active)
                    && active.getClientRects().length > 0) return;
            focusFirst(current.dialog);
        },
        // exposed for tests / diagnostics
        _root: getRoot
    };
})();