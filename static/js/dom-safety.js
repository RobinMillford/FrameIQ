/**
 * FrameIQ DOM-safety helpers (Feature F21).
 *
 * WHY THIS EXISTS
 * ---------------
 * A security audit found stored XSS in several render paths where untrusted
 * values were concatenated into an `innerHTML` string. The important lesson is
 * that "escape better" is NOT the fix:
 *
 *   * The common `textContent -> innerHTML` escaper only encodes `& < >`.
 *     Quotes pass through untouched, so the value still breaks out of
 *     `onclick="removeCollaborator(1, '${username}')"`.
 *   * No encoding at all prevents `href="javascript:alert(1)"` from being
 *     followed. Encoding and URL scheme validation are different problems.
 *   * One generic escaper cannot be correct for HTML text, attribute, JS
 *     string, CSS and URL contexts at the same time.
 *
 * So the strategy is to stop building executable strings:
 *
 *   1. Untrusted text goes in through `textContent` (or `el({text})`), which
 *      cannot produce markup. There is deliberately no `html` option.
 *   2. Untrusted URLs go through `safeLinkUrl` / `safeImageUrl`, which parse
 *      the value and enforce an http/https allowlist INDEPENDENT of any HTML
 *      encoding. `javascript:`, `data:`, `vbscript:`, protocol-relative
 *      `//host` and unparseable values are rejected.
 *   3. Behaviour is attached with `addEventListener` over a closure or a
 *      validated numeric id — never by interpolating data into an inline
 *      `onclick` string.
 *
 * Loaded as a classic script; exposes `window.FrameIQDOM`.
 * Dependency-free and framework-free by design.
 */
(function (global) {
    'use strict';

    /** Schemes we are willing to fetch or navigate to. Anything else is refused. */
    var SAFE_SCHEMES = ['http:', 'https:'];

    /**
     * Characters that must never appear in a URL: C0 controls, space, DEL.
     * These are used to smuggle `java\tscript:` or `java\nscript:` past naive
     * prefix checks, and browsers strip them before resolving the scheme.
     */
    var URL_SMUGGLING_RE = /[\u0000-\u0020\u007f]/;

    /** A value that starts with a scheme, e.g. `javascript:`, `https:`. */
    var HAS_SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.\-]*:/;

    /**
     * Attribute names `el()` refuses outright.
     *
     * Behaviour belongs in `addEventListener`, never in an attribute — that is
     * the whole point of the rewrite, and an `on*` handler set here would
     * revive exactly the injection sink F21 removed. `style` is excluded too:
     * a raw style string is an unvalidated CSS context, which is what
     * `safeCssColor` exists to prevent. `srcdoc` parses its content as markup.
     */
    var DENY_ATTRIBUTES = {
        'onclick': true, 'onerror': true, 'onload': true, 'onmouseover': true,
        'onmouseout': true, 'onfocus': true, 'onblur': true, 'onchange': true,
        'oninput': true, 'onsubmit': true, 'onkeydown': true, 'onkeyup': true,
        'onanimationend': true, 'ontransitionend': true, 'onanimationstart': true,
        'style': true, 'srcdoc': true,
    };

    /**
     * Coerce any value to a string safe to assign to `textContent`.
     * `null`/`undefined` become '' so callers never print "undefined"; numbers
     * and booleans render, because the feed legitimately shows counts.
     */
    function text(value) {
        if (value === null || value === undefined) return '';
        return String(value);
    }

    /**
     * Validate a URL for use in `src` or `href`.
     *
     * @param {*} value          candidate URL, from any source
     * @param {*} relativePrefix when the application legitimately stores bare
     *        filenames (e.g. "uploads/a.png"), the caller states the prefix it
     *        would have applied. Only a basename survives.
     * @returns {string|null}    a validated URL, or null if unsafe
     */
    function safeUrl(value, relativePrefix) {
        if (typeof value !== 'string') return null;
        var raw = value.trim();
        if (!raw) return null;
        if (URL_SMUGGLING_RE.test(raw)) return null;

        // Protocol-relative "//evil.example" resolves to another origin.
        if (raw.slice(0, 2) === '//') return null;

        // Root-relative: same origin by construction.
        if (raw.charAt(0) === '/') {
            if (raw.indexOf('\\') !== -1) return null;
            if (raw.indexOf('..') !== -1) return null;
            // "/javascript:alert(1)" is a path, but reject anything that looks
            // like a scheme anyway so the intent is never ambiguous.
            if (HAS_SCHEME_RE.test(raw.slice(1))) return null;
            return raw;
        }

        // A bare relative filename is only accepted when the caller declared
        // the prefix it would have applied. Reduce it to a basename so no
        // directory traversal can ride along.
        if (!HAS_SCHEME_RE.test(raw)) {
            if (!relativePrefix) return null;
            var base = raw.split(/[\\/]/).pop();
            if (!base || base === '.' || base === '..' || base.indexOf('..') !== -1) {
                return null;
            }
            return relativePrefix + base;
        }

        // Absolute URL: require an explicit, parsed, allowlisted scheme.
        var parsed;
        try {
            parsed = new URL(raw);
        } catch (err) {
            return null;
        }
        if (SAFE_SCHEMES.indexOf(parsed.protocol) === -1) return null;
        return parsed.href;
    }

    /**
     * URL for an `<img src>`. Returns `fallback` when the value is unusable,
     * so a broken avatar degrades to the local placeholder instead of an
     * empty src (which browsers treat as a re-request of the current page).
     */
    function safeImageUrl(value, fallback, relativePrefix) {
        var ok = safeUrl(value, relativePrefix);
        if (ok) return ok;
        return typeof fallback === 'string' ? fallback : '';
    }

    /** URL for an `<a href>`. Returns '' when unsafe, so callers can skip the link. */
    function safeLinkUrl(value, relativePrefix) {
        return safeUrl(value, relativePrefix) || '';
    }

    /**
     * Build an element.
     *
     * `attrs.text` is assigned via `textContent` — the only supported way to
     * place untrusted content. `href` and `src` are routed through the URL
     * validators, so a caller cannot accidentally bypass them. There is
     * intentionally no `innerHTML` / `html` option.
     */
    function el(tag, attrs, children) {
        var node = document.createElement(tag);
        if (attrs) {
            Object.keys(attrs).forEach(function (key) {
                var value = attrs[key];
                if (value === null || value === undefined || value === false) return;

                if (key === 'text') {
                    node.textContent = text(value);
                } else if (key === 'class') {
                    node.className = String(value);
                } else if (key === 'dataset') {
                    Object.keys(value).forEach(function (dataKey) {
                        node.dataset[dataKey] = text(value[dataKey]);
                    });
                } else if (key === 'href') {
                    var href = safeLinkUrl(value);
                    if (href) node.setAttribute('href', href);
                } else if (key === 'src') {
                    node.setAttribute('src', safeImageUrl(value, ''));
                } else if (value === true) {
                    node.setAttribute(key, '');
                } else if (DENY_ATTRIBUTES[key.toLowerCase()]) {
                    // Refuse rather than silently skip: a caller reaching for
                    // `onclick` here is reintroducing the exact pattern F21
                    // removed, and quietly dropping the attribute would hide
                    // that. Use addEventListener instead.
                    throw new Error(
                        'FrameIQDOM.el: attribute ' + key + ' is not allowed; '
                        + 'use addEventListener for behaviour and a validated '
                        + 'property (href/src) for URLs');
                } else {
                    node.setAttribute(key, String(value));
                }
            });
        }
        appendChildren(node, children);
        return node;
    }

    function appendChildren(node, children) {
        if (children === null || children === undefined) return;
        var list = Array.isArray(children) ? children : [children];
        list.forEach(function (child) {
            if (child === null || child === undefined || child === false) return;
            if (Array.isArray(child)) {
                appendChildren(node, child);
            } else if (typeof child === 'object' && child.nodeType) {
                node.appendChild(child);
            } else {
                node.appendChild(document.createTextNode(text(child)));
            }
        });
    }

    /** Remove every child of a node. */
    function clear(node) {
        if (!node) return node;
        while (node.firstChild) node.removeChild(node.firstChild);
        return node;
    }

    /**
     * Validate an integer id before it is used in a URL or a request.
     * Returns null for anything that is not a safe non-negative integer.
     */
    function safeId(value) {
        // One bound for both input types: 15 digits is far beyond any real row
        // id and keeps the numeric and string paths consistent.
        if (typeof value === 'number' &&
            Number.isInteger(value) && value >= 0 && value <= 999999999999999) {
            return value;
        }
        if (typeof value === 'string' && /^\d{1,15}$/.test(value)) {
            return Number(value);
        }
        return null;
    }

    /**
     * Validate a CSS colour before it reaches a `style` declaration.
     * Only plain hex / rgb / hsl values survive, so a stored colour field
     * cannot inject additional CSS declarations or a `url()` fetch.
     */
    function safeCssColor(value, fallback) {
        var fb = typeof fallback === 'string' ? fallback : '';
        if (typeof value !== 'string') return fb;
        var candidate = value.trim();
        if (/^#[0-9a-fA-F]{3,8}$/.test(candidate)) return candidate;
        if (/^rgba?\([0-9.,%\s]+\)$/.test(candidate)) return candidate;
        if (/^hsla?\([0-9.,%\sdeg]+\)$/.test(candidate)) return candidate;
        return fb;
    }

    /**
     * Escape a value for interpolation into HTML TEXT.
     *
     * Use this ONLY when the surrounding code is already assembling an HTML
     * string and cannot be switched to `textContent`. Where the markup can be
     * built as real nodes, prefer `el()` / `textContent`: that is immune to
     * context confusion, whereas an escaper is not. `escapeText` and
     * `escapeAttr` escape the same character set and differ only in intent —
     * use `escapeAttr` inside a quoted attribute so the call site documents
     * which context it reasoned about.
     *
     * This is the ONE shared escaper. F38 consolidated the per-file copies that
     * had accumulated so there is a single implementation to audit.
     */
    function escapeText(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /** Escape a value for a double-quoted HTML attribute. See escapeText. */
    function escapeAttr(value) {
        return escapeText(value);
    }

    // ── TMDb image policy ──────────────────────────────────────────────────
    //
    // Shared because seven templates need the identical rule, and a per-file
    // copy is a per-file chance to get the host allowlist wrong.

    var TMDB_IMAGE_ORIGIN = 'https://image.tmdb.org';
    var TMDB_IMAGE_HOST = 'image.tmdb.org';
    var LOOKS_LIKE_SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.\-]*:/;

    /**
     * Build a TMDb image URL from a stored poster / still / profile value.
     *
     * `safeImageUrl()` deliberately cannot be used here: it enforces a SCHEME
     * allowlist and is host-agnostic, so it would return
     * `https://attacker.example/pixel.png` unchanged. An `<img src>` is a
     * request the browser makes on the visitor's behalf, so the host has to be
     * pinned.
     *
     * Policy:
     *   1. empty / non-string / control chars / spaces   -> fallback
     *   2. protocol-relative `//host`                    -> fallback
     *   3. absolute URL: accepted only when it parses, is `https:`, has host
     *      exactly `image.tmdb.org`, and carries no credentials
     *   4. root-relative TMDb path: rejected if it contains `..`, a
     *      backslash, `?`, `#`, or a leading segment that looks like a scheme;
     *      otherwise prefixed with the fixed origin
     *   5. anything else                                 -> fallback
     *
     * Absolute values are legitimate — `api/continue_watching.py` stores and
     * re-emits them — so case 3 is a compatibility requirement.
     *
     * The relative branch builds the full URL and then parses it, so the URL
     * parser percent-encodes anything unsafe. Plain concatenation let `/a"b.jpg`
     * through with the quote intact.
     */
    function tmdbImageUrl(value, size, fallback) {
        var fb = typeof fallback === 'string'
            ? fallback : '/static/images/no-poster.png';
        if (typeof value !== 'string') return fb;

        var raw = value.trim();
        if (!raw) return fb;
        // Browsers strip control characters and spaces before resolving, so
        // they are used to smuggle schemes past prefix checks.
        if (URL_SMUGGLING_RE.test(raw)) return fb;

        // Protocol-relative: resolves to another origin.
        if (raw.slice(0, 2) === '//') return fb;

        if (LOOKS_LIKE_SCHEME_RE.test(raw)) {
            var parsed;
            try {
                parsed = new URL(raw);
            } catch (err) {
                return fb;
            }
            if (parsed.protocol !== 'https:') return fb;
            if (parsed.hostname !== TMDB_IMAGE_HOST) return fb;
            // F41: `hostname` excludes the port, so the host allowlist alone
            // would admit a non-default port on an approved host.
            if (parsed.port && parsed.port !== '443') return fb;
            if (parsed.username || parsed.password) return fb;
            return parsed.href;
        }

        if (raw.charAt(0) === '/') {
            if (raw.indexOf('\\') !== -1) return fb;
            if (raw.indexOf('..') !== -1) return fb;
            // A poster path never carries a query or fragment; allowing them
            // would silently truncate the request at the first one.
            if (raw.indexOf('?') !== -1) return fb;
            if (raw.indexOf('#') !== -1) return fb;
            if (LOOKS_LIKE_SCHEME_RE.test(raw.slice(1))) return fb;

            // The size segment is part of the string being parsed:
            // `new URL(path, base)` would resolve against `base` and drop it.
            var built;
            try {
                built = new URL(TMDB_IMAGE_ORIGIN + '/t/p/' + size + raw);
            } catch (err) {
                return fb;
            }
            if (built.protocol !== 'https:') return fb;
            if (built.hostname !== TMDB_IMAGE_HOST) return fb;
            if (built.port && built.port !== '443') return fb;
            return built.href;
        }

        return fb;
    }

    /**
     * Image URL restricted to a fixed set of hosts, or to this origin.
     *
     * `safeImageUrl()` cannot be used for user-supplied image URLs: it is a
     * scheme allowlist, so `https://anywhere.example/pixel.png` passes. For a
     * list cover — which `PUT /api/lists/<id>/cover` stores verbatim from the
     * request body with no server-side validation — that hands the list owner a
     * way to make every visitor's browser fetch a URL of their choosing, which
     * is a tracking / visitor-IP disclosure vector rather than script
     * execution.
     *
     * POLICY
     *   - root-relative path        -> accepted (same origin by construction)
     *   - absolute URL              -> accepted only when it is `https:`, has a
     *                                  host in `hosts`, and has no credentials
     *   - protocol-relative / other -> `fallback`
     */
    function safeImageUrlFromHosts(value, hosts, fallback) {
        var fb = typeof fallback === 'string' ? fallback : '';
        if (typeof value !== 'string') return fb;

        var raw = value.trim();
        if (!raw) return fb;
        if (URL_SMUGGLING_RE.test(raw)) return fb;
        if (raw.slice(0, 2) === '//') return fb;

        var allowed = Array.isArray(hosts) ? hosts : [];
        if (LOOKS_LIKE_SCHEME_RE.test(raw)) {
            var parsed;
            try {
                parsed = new URL(raw);
            } catch (err) {
                return fb;
            }
            if (parsed.protocol !== 'https:') return fb;
            // F41: `hostname` excludes the port, so an allowlist check alone
            // would accept `image.tmdb.org:8443` — a different service on an
            // otherwise-approved host. The server refuses that form too.
            if (allowed.indexOf(parsed.hostname) === -1) return fb;
            if (parsed.port && parsed.port !== '443') return fb;
            if (parsed.username || parsed.password) return fb;
            return parsed.href;
        }

        if (raw.charAt(0) === '/') {
            if (raw.indexOf('\\') !== -1) return fb;
            if (raw.indexOf('..') !== -1) return fb;
            // F41: a query or fragment truncates the request at the first `?`
            // or `#`, so it is never a plain image path. The server refuses
            // these for the same reason.
            if (raw.indexOf('?') !== -1) return fb;
            if (raw.indexOf('#') !== -1) return fb;
            if (LOOKS_LIKE_SCHEME_RE.test(raw.slice(1))) return fb;
            return raw;
        }

        return fb;
    }

    /**
     * Accept only a same-origin, root-relative path. Returns '' otherwise, so
     * the caller omits the link rather than emitting a broken one.
     *
     * Stricter than `safeLinkUrl()`, which is the right default for a genuinely
     * outbound link. For values the server always builds from Integer columns —
     * `/watch/tv/{show_id}/{season}/{episode}` — an absolute or off-origin
     * target is never legitimate data, and an off-site "Watch Next" button is an
     * open-redirect and phishing surface.
     */
    function safeInternalUrl(value) {
        if (typeof value !== 'string') return '';
        var raw = value.trim();
        if (!raw) return '';
        if (URL_SMUGGLING_RE.test(raw)) return '';
        if (raw.charAt(0) !== '/') return '';
        if (raw.slice(0, 2) === '//') return '';
        if (raw.indexOf('\\') !== -1) return '';
        if (raw.indexOf('..') !== -1) return '';
        if (LOOKS_LIKE_SCHEME_RE.test(raw.slice(1))) return '';
        return raw;
    }

    /** Build a Font Awesome <i> icon. `name` comes from our own code. */
    function icon(name, extraClass) {
        var cls = 'fas fa-' + String(name).replace(/[^a-z0-9-]/gi, '');
        if (extraClass) cls += ' ' + String(extraClass);
        return el('i', { class: cls });
    }

    global.FrameIQDOM = {
        text: text,
        escapeText: escapeText,
        escapeAttr: escapeAttr,
        safeUrl: safeUrl,
        safeImageUrl: safeImageUrl,
        safeLinkUrl: safeLinkUrl,
        tmdbImageUrl: tmdbImageUrl,
        safeImageUrlFromHosts: safeImageUrlFromHosts,
        safeInternalUrl: safeInternalUrl,
        el: el,
        clear: clear,
        safeId: safeId,
        safeCssColor: safeCssColor,
        icon: icon,
    };
}(window));
