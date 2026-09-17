/**
 * Year in Review share controls (Feature #8, Phase 9).
 *
 * Explicit opt-in ONLY (§44): no share request fires on page load, on
 * year change, or on anything else — the user must click "Share".
 *
 * Talks exclusively to FrameIQ's own endpoints (§42):
 *     POST   /api/year-in-review/share   {year} → {share_url}
 *     DELETE /api/year-in-review/share   {year} → {active:false}
 * Both are session-authenticated and CSRF-protected (X-CSRFToken header,
 * the established repo convention). No polling, no duplicate clicks
 * (buttons are disabled while a request is in flight), no private
 * statistics transmitted anywhere else, no /api/statistics calls.
 *
 * State handling (§43): idle → creating → active (Copy link + Revoke
 * shown) → revoked → back to idle. Errors are neutral; tokens/URLs are
 * never displayed in error text.
 *
 * XSS safety: only createElement/textContent — no innerHTML.
 */
(function () {
    'use strict';

    var YEAR = window.__YIR_YEAR__; // injected period label (not private data)
    var shareUrl = null;
    var inFlight = false;

    function el(id) {
        return document.getElementById(id);
    }

    function show(node) {
        node.classList.remove('hidden');
    }

    function hide(node) {
        node.classList.add('hidden');
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return (meta && meta.content) || '';
    }

    function setStatus(text) {
        var status = el('yir-share-status');
        if (status) {
            status.textContent = text; // accessible (role="status", aria-live)
        }
    }

    function setButtons(state) {
        // state: 'idle' | 'busy' | 'active'
        var share = el('yir-share-btn');
        var copy = el('yir-copy-btn');
        var revoke = el('yir-revoke-btn');
        var busy = state === 'busy';
        share.disabled = busy;
        if (state === 'active') {
            show(copy);
            show(revoke);
        } else {
            hide(copy);
            hide(revoke);
        }
    }

    function request(method, payload, done) {
        // One request per explicit action; guarded by inFlight so
        // duplicate clicks are no-ops (§50.66).
        var headers = {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken()
        };
        fetch('/api/year-in-review/share', {
            method: method,
            credentials: 'same-origin',
            headers: headers,
            body: JSON.stringify(payload)
        })
            .then(function (res) {
                return res.json().then(function (data) {
                    return { ok: res.ok, status: res.status, data: data };
                });
            })
            .then(function (result) { done(result); })
            .catch(function () {
                done({ ok: false, status: 0, data: null });
            });
    }

    function copyLink() {
        if (!shareUrl) {
            return;
        }
        var finish = function (ok) {
            setStatus(ok ? 'Link copied.' : 'Copy failed — copy the link from the Share result.');
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(shareUrl)
                .then(function () { finish(true); })
                .catch(function () { finish(legacyCopy()); });
        } else {
            finish(legacyCopy());
        }
    }

    function legacyCopy() {
        // Safe fallback (§42): a temporary textarea the user's own
        // clipboard command fills. Never touches innerHTML.
        try {
            var helper = document.createElement('textarea');
            helper.value = shareUrl;
            helper.setAttribute('readonly', 'readonly');
            helper.className = 'fixed left-[-9999px]';
            document.body.appendChild(helper);
            helper.select();
            var ok = document.execCommand('copy');
            document.body.removeChild(helper);
            return ok;
        } catch (err) {
            return false;
        }
    }

    function createShare() {
        if (inFlight) {
            return;
        }
        inFlight = true;
        setButtons('busy');
        setStatus('Creating share…');
        request('POST', { year: YEAR }, function (result) {
            inFlight = false;
            if (!result.ok) {
                setButtons('idle');
                setStatus('Sharing is unavailable right now.');
                return;
            }
            shareUrl = result.data.share_url;
            setButtons('active');
            setStatus('Share link is active.');
        });
    }

    function revokeShare() {
        if (inFlight) {
            return;
        }
        inFlight = true;
        setButtons('busy');
        setStatus('Revoking share…');
        request('DELETE', { year: YEAR }, function (result) {
            inFlight = false;
            shareUrl = null;
            setButtons('idle');
            if (!result.ok) {
                setStatus('Revocation is unavailable right now.');
                return;
            }
            setStatus('Share revoked. Old links no longer work.');
        });
    }

    function init() {
        var controls = el('yir-share-controls');
        var share = el('yir-share-btn');
        var copy = el('yir-copy-btn');
        var revoke = el('yir-revoke-btn');
        if (!controls || !share || !copy || !revoke ||
            typeof YEAR !== 'number') {
            return; // not the recap page — nothing to do
        }
        show(controls);
        share.addEventListener('click', createShare);
        copy.addEventListener('click', copyLink);
        revoke.addEventListener('click', revokeShare);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
