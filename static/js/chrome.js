/**
 * FrameIQ Chrome — command palette (⌘K), CineBot slide-over, toasts.
 * Loaded site-wide via base.html. Requires: tokens.css + chrome.css.
 * Optional: marked + DOMPurify for markdown in CineBot replies.
 */
(function () {
    'use strict';

    function isAuthed() {
        return window.__IS_AUTH__ === true;
    }

    function requireAuth() {
        if (isAuthed()) return true;
        window.location.href = '/login';
        return false;
    }

    /* ── Global toast ─────────────────────────────────────────────── */

    window.frameToast = function (message, type) {
        let toast = document.querySelector('.fi-toast');
        if (!toast) {
            toast = document.createElement('div');
            toast.className = 'fi-toast';
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.className = 'fi-toast show' + (type ? ' ' + type : '');
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => toast.classList.remove('show'), 3200);
    };

    /* ── Markdown helper (shared with palette/panel) ──────────────── */

    function renderMarkdown(text) {
        if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            return DOMPurify.sanitize(marked.parse(text || ''));
        }
        const el = document.createElement('div');
        el.textContent = text;
        return el.innerHTML;
    }

    /* ── Command palette (⌘K) ─────────────────────────────────────── */

    const PALETTE_ACTIONS = [
        { title: 'Movies', sub: '/movies', href: '/movies', icon: 'film' },
        { title: 'TV Shows', sub: '/tv_shows', href: '/tv_shows', icon: 'tv' },
        { title: 'Discover', sub: '/discover', href: '/discover', icon: 'compass' },
        { title: 'Trending', sub: '/trending', href: '/trending', icon: 'trending-up' },
        { title: 'News', sub: '/news', href: '/news', icon: 'newspaper' },
        { title: 'Social Feed', sub: '/feed', href: '/feed', icon: 'users' },
        { title: 'Track TV', sub: '/tv/dashboard', href: '/tv/dashboard', icon: 'list-video' },
        { title: 'My Diary', sub: '/diary', href: '/diary', icon: 'book-open' },
        { title: 'Watchlist', sub: '/watchlist', href: '/watchlist', icon: 'bookmark' },
        { title: 'My Stats', sub: '/stats', href: '/stats', icon: 'bar-chart-3' },
    ];

    let cmdk = null;          // DOM refs, created lazily
    let cmdkItems = [];       // flat list for keyboard nav
    let cmdkIndex = 0;
    let cmdkDebounce = null;

    function buildPalette() {
        const overlay = document.createElement('div');
        overlay.className = 'cmdk-overlay';
        overlay.innerHTML = `
            <div class="cmdk-panel" role="dialog" aria-label="Command palette">
                <input class="cmdk-input" type="text" autocomplete="off"
                       placeholder="Search titles, people… or type a question"
                       aria-label="Search" />
                <div class="cmdk-results"></div>
                <div class="cmdk-hint">
                    <span><kbd>↑↓</kbd> navigate · <kbd>↵</kbd> open</span>
                    <span><kbd>esc</kbd> close</span>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        cmdk = {
            overlay,
            input: overlay.querySelector('.cmdk-input'),
            results: overlay.querySelector('.cmdk-results'),
        };

        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closePalette();
        });
        cmdk.input.addEventListener('input', () => {
            clearTimeout(cmdkDebounce);
            cmdkDebounce = setTimeout(paletteSearch, 180);
        });
        cmdk.input.addEventListener('keydown', paletteKeys);
        overlay.addEventListener('click', (e) => {
            const item = e.target.closest('[data-href], [data-cinebot]');
            if (!item) return;
            if (item.dataset.cinebot !== undefined) {
                closePalette();
                openCinebot(item.dataset.cinebot || '');
            } else if (item.dataset.href) {
                window.location.href = item.dataset.href;
            }
        });
    }

    function openPalette() {
        if (!cmdk) buildPalette();
        cmdk.overlay.classList.add('open');
        cmdk.input.value = '';
        renderResults([], '');
        setTimeout(() => cmdk.input.focus(), 30);
    }

    function closePalette() {
        if (cmdk) cmdk.overlay.classList.remove('open');
    }

    function togglePalette() {
        if (cmdk && cmdk.overlay.classList.contains('open')) closePalette();
        else openPalette();
    }

    function itemHTML(html, attrs) {
        return `<div class="cmdk-item" ${attrs}>${html}</div>`;
    }

    async function paletteSearch() {
        const q = cmdk.input.value.trim();

        // Actions always shown (filtered by query)
        const actions = PALETTE_ACTIONS.filter(a =>
            !q || a.title.toLowerCase().includes(q.toLowerCase()));

        let media = { movies: [], shows: [], people: [] };
        if (q.length >= 2) {
            try {
                const r = await fetch(`/autocomplete?query=${encodeURIComponent(q)}`);
                media = await r.json();
            } catch { /* network hiccup — actions still work */ }
        }

        const cinebotFirst = q.length >= 2;
        let html = '';
        cmdkItems = [];
        cmdkIndex = 0;

        if (cinebotFirst) {
            html += '<div class="cmdk-group">Ask</div>';
            html += itemHTML(
                `<span class="cmdk-icon">✦</span>
                 <div><div class="cmdk-item-title">Ask CineBot</div>
                 <div class="cmdk-item-sub">"${q.replace(/</g, '&lt;')}"</div></div>`,
                'data-cinebot="' + q.replace(/"/g, '&quot;') + '"');
            cmdkItems.push({ cinebot: q });
        }

        if (actions.length) {
            html += '<div class="cmdk-group">Go to</div>';
            for (const a of actions) {
                html += itemHTML(
                    `<span class="cmdk-icon"><i data-lucide="${a.icon}"></i></span>
                     <div class="cmdk-item-title">${a.title}</div>
                     <div class="cmdk-item-sub">${a.sub}</div>`,
                    `data-href="${a.href}"`);
                cmdkItems.push({ href: a.href });
            }
        }

        const sections = [
            ['Movies', media.movies, 'movie'],
            ['TV Shows', media.shows, 'tv'],
            ['People', media.people, 'person'],
        ];
        for (const [label, items, kind] of sections) {
            if (!items || !items.length) continue;
            html += `<div class="cmdk-group">${label}</div>`;
            for (const it of items.slice(0, 5)) {
                const href = kind === 'person'
                    ? `/actor/${it.id}`
                    : `/${kind}/${it.id}`;
                const title = it.title || it.name || 'Unknown';
                const sub = it.release_date || it.first_air_date || it.known_for || '';
                const poster = it.poster_path
                    ? `<img class="cmdk-poster" src="https://image.tmdb.org/t/p/w92${it.poster_path}" alt="">`
                    : `<span class="cmdk-icon"><i data-lucide="${kind === 'person' ? 'user' : 'film'}"></i></span>`;
                html += itemHTML(
                    `${poster}<div><div class="cmdk-item-title">${title}</div>
                     <div class="cmdk-item-sub">${sub}</div></div>`,
                    `data-href="${href}"`);
                cmdkItems.push({ href });
            }
        }

        if (!html) {
            html = q.length >= 2
                ? '<div class="cmdk-empty">No matches — try fewer letters, or ask CineBot above.</div>'
                : '';
        }

        cmdk.results.innerHTML = html || '<div class="cmdk-empty">Type to search…</div>';
        if (window.lucide) lucide.createIcons();
        highlightItem();
    }

    function highlightItem() {
        cmdk.results.querySelectorAll('.cmdk-item').forEach((el, i) => {
            el.classList.toggle('selected', i === cmdkIndex);
        });
        const sel = cmdk.results.querySelectorAll('.cmdk-item')[cmdkIndex];
        if (sel) sel.scrollIntoView({ block: 'nearest' });
    }

    function paletteKeys(e) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            cmdkIndex = Math.min(cmdkIndex + 1, cmdkItems.length - 1);
            highlightItem();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            cmdkIndex = Math.max(cmdkIndex - 1, 0);
            highlightItem();
        } else if (e.key === 'Enter') {
            e.preventDefault();
            const item = cmdkItems[cmdkIndex];
            if (!item) return;
            closePalette();
            if (item.cinebot !== undefined) openCinebot(item.cinebot);
            else if (item.href) window.location.href = item.href;
        }
    }

    /* ── CineBot slide-over ───────────────────────────────────────── */

    let panel = null;
    let panelBusy = false;

    function buildPanel() {
        const overlay = document.createElement('div');
        overlay.className = 'cinebot-overlay';
        const el = document.createElement('div');
        el.className = 'cinebot-panel';
        el.setAttribute('role', 'dialog');
        el.setAttribute('aria-label', 'CineBot AI assistant');
        el.innerHTML = `
            <div class="cinebot-header">
                <div class="cinebot-title">
                    <span data-lucide="sparkles" class="w-4 h-4 text-[var(--accent-hi)]"></span>
                    CineBot <span class="cinebot-dot" title="online"></span>
                </div>
                <div class="flex items-center gap-2">
                    <a href="/chat" class="font-slate text-[10px] text-[var(--text-low)] hover:text-[var(--text-mid)]"
                       title="Open full-page chat">full page ↗</a>
                    <button class="cinebot-close text-[var(--text-low)] hover:text-white p-1" aria-label="Close">
                        <i data-lucide="x" class="w-5 h-5"></i>
                    </button>
                </div>
            </div>
            <div class="cinebot-context"></div>
            <div class="cinebot-messages">
                <div class="cinebot-msg bot">Hey — I'm CineBot. Ask me anything about movies or shows,
                or what to watch next.</div>
            </div>
            <div class="cinebot-inputbar">
                <input type="text" placeholder="Ask about any movie, show, or your taste…"
                       aria-label="Message CineBot">
                <button aria-label="Send">
                    <i data-lucide="send" class="w-4 h-4"></i>
                </button>
            </div>`;
        document.body.appendChild(overlay);
        document.body.appendChild(el);

        panel = {
            overlay,
            el,
            messages: el.querySelector('.cinebot-messages'),
            input: el.querySelector('input'),
            send: el.querySelector('.cinebot-inputbar button'),
            context: el.querySelector('.cinebot-context'),
        };

        overlay.addEventListener('click', closeCinebot);
        el.querySelector('.cinebot-close').addEventListener('click', closeCinebot);
        panel.send.addEventListener('click', sendPanelMessage);
        panel.input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendPanelMessage();
            }
        });
    }

    function openCinebot(prefill) {
        if (!requireAuth()) return;
        if (!panel) buildPanel();
        panel.overlay.classList.add('open');
        panel.el.classList.add('open');

        // Page context — lets CineBot answers reference what you're viewing
        const ctx = document.title.replace(' — FrameIQ', '').replace(/ — Watch on FrameIQ/, '');
        panel.context.textContent = ctx ? `viewing: ${ctx}` : '';
        panel.context.style.display = ctx ? 'block' : 'none';

        if (window.lucide) lucide.createIcons();
        setTimeout(() => {
            panel.input.focus();
            if (prefill) {
                panel.input.value = prefill;
                sendPanelMessage();
            }
        }, 250);
    }

    function closeCinebot() {
        if (!panel) return;
        panel.overlay.classList.remove('open');
        panel.el.classList.remove('open');
    }

    function toggleCinebot() {
        if (panel && panel.el.classList.contains('open')) closeCinebot();
        else openCinebot('');
    }

    function addMsg(sender, html) {
        const div = document.createElement('div');
        div.className = `cinebot-msg ${sender}`;
        div.innerHTML = html;
        panel.messages.appendChild(div);
        panel.messages.scrollTop = panel.messages.scrollHeight;
        return div;
    }

    async function sendPanelMessage() {
        const text = panel.input.value.trim();
        if (!text || panelBusy) return;
        panelBusy = true;
        panel.send.disabled = true;
        panel.input.value = '';

        addMsg('user', renderMarkdown(text));

        // Page context on the first message of a conversation
        const ctx = panel.context.textContent.replace('viewing: ', '');
        const message = panel.messages.querySelectorAll('.cinebot-msg').length <= 2 && ctx
            ? `${text} (brief context: I'm currently on the "${ctx}" page)`
            : text;

        const bubble = addMsg('bot', '<span class="cursor-blink"></span>');
        let acc = '';

        try {
            const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
            const resp = await fetch('/chat_api', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n\n');
                buffer = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    let data;
                    try { data = JSON.parse(line.slice(6)); } catch { continue; }

                    if (data.type === 'token') {
                        acc += data.content;
                        bubble.innerHTML = renderMarkdown(acc) + '<span class="cursor-blink"></span>';
                        panel.messages.scrollTop = panel.messages.scrollHeight;
                    } else if (data.type === 'error') {
                        acc = `Sorry — ${data.error}`;
                        bubble.innerHTML = renderMarkdown(acc);
                    }
                }
            }
            bubble.innerHTML = renderMarkdown(acc) || '…';
        } catch (err) {
            bubble.innerHTML = renderMarkdown(
                'Sorry, I hit a snag reaching the server. Try again in a moment.');
        } finally {
            panelBusy = false;
            panel.send.disabled = false;
            panel.messages.scrollTop = panel.messages.scrollHeight;
        }
    }

    /* ── Global wiring ────────────────────────────────────────────── */

    document.addEventListener('DOMContentLoaded', () => {
        // ⌘K / Ctrl+K toggles palette; ⌘J toggles CineBot
        document.addEventListener('keydown', (e) => {
            const mod = e.metaKey || e.ctrlKey;
            if (mod && e.key.toLowerCase() === 'k') {
                e.preventDefault();
                togglePalette();
            } else if (mod && e.key.toLowerCase() === 'j') {
                e.preventDefault();
                toggleCinebot();
            } else if (e.key === 'Escape') {
                closePalette();
                // don't force-close cinebot — Esc inside inputs is common
            }
        });

        // Nav "Ask CineBot" buttons open the slide-over instead of navigating
        document.querySelectorAll('[data-cinebot-open]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                openCinebot('');
            });
        });

        // Search icon in nav opens palette
        const searchBtn = document.getElementById('nav-search-button');
        if (searchBtn) searchBtn.addEventListener('click', openPalette);

        if (window.lucide) lucide.createIcons();
    });
})();
