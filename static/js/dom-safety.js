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

    /** Build a Font Awesome <i> icon. `name` comes from our own code. */
    function icon(name, extraClass) {
        var cls = 'fas fa-' + String(name).replace(/[^a-z0-9-]/gi, '');
        if (extraClass) cls += ' ' + String(extraClass);
        return el('i', { class: cls });
    }

    global.FrameIQDOM = {
        text: text,
        safeUrl: safeUrl,
        safeImageUrl: safeImageUrl,
        safeLinkUrl: safeLinkUrl,
        el: el,
        clear: clear,
        safeId: safeId,
        safeCssColor: safeCssColor,
        icon: icon,
    };
}(window));
