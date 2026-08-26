/**
 * CineBot chat page — streaming, markdown, suggestions.
 * Template renders the layout; this file handles all interactions.
 */
(function () {
    'use strict';

    const chatMessages = document.getElementById('chat-messages');
    const userInput = document.getElementById('user-input');
    const sendButton = document.getElementById('send-button');
    const BOT_NAME = 'CineBot';

    let lastSaved = 0;

    /* ── Markdown ── */
    function renderMarkdown(text) {
        if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
            return DOMPurify.sanitize(marked.parse(text || ''));
        }
        const el = document.createElement('div');
        el.textContent = text;
        return el.innerHTML;
    }

    /* ── Message builders ── */

    function addMessage(sender, text) {
        const div = document.createElement('div');
        div.className = `chat-msg ${sender}`;

        const name = document.createElement('div');
        name.className = 'chat-msg-name';
        name.textContent = sender === 'bot' ? BOT_NAME : 'You';
        div.appendChild(name);

        const body = document.createElement('div');
        if (sender === 'bot') {
            body.innerHTML = renderMarkdown(text);
        } else {
            body.textContent = text;
        }
        div.appendChild(body);
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
        return div;
    }

    function createThinkingPanel() {
        const panel = document.createElement('details');
        panel.className = 'thinking-panel';
        panel.innerHTML = '<summary>Thinking…</summary>';
        chatMessages.appendChild(panel);
        chatMessages.scrollTop = chatMessages.scrollHeight;
        return panel;
    }

    function addToolLine(panel, label) {
        const line = document.createElement('div');
        line.className = 'tool-line';
        line.textContent = label;
        panel.appendChild(line);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function createStreamingBubble() {
        const div = document.createElement('div');
        div.className = 'chat-msg bot';

        const name = document.createElement('div');
        name.className = 'chat-msg-name';
        name.textContent = BOT_NAME;
        div.appendChild(name);

        const body = document.createElement('div');
        body.innerHTML = '<span class="cursor-blink"></span>';
        div.appendChild(body);
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
        return { div, body, raw: '' };
    }

    function appendToken(bubble, token) {
        bubble.raw += token;
        bubble.body.innerHTML = renderMarkdown(bubble.raw) + '<span class="cursor-blink"></span>';
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    /* ── Media cards ── */

    function displayMedia(items, type) {
        const section = document.createElement('div');
        section.className = 'chat-media-section';

        const title = document.createElement('div');
        title.className = 'chat-media-title';
        title.textContent = type;
        section.appendChild(title);

        const container = document.createElement('div');
        container.className = 'chat-media-items';

        items.forEach((item) => {
            const link = document.createElement('a');
            link.href = item.tmdb_link || '#';
            const div = document.createElement('div');
            div.className = 'chat-media-item';

            const img = document.createElement('img');
            img.src = item.poster_url || '';
            img.alt = item.title || '';
            img.loading = 'lazy';
            img.onerror = function () { this.src = '/static/images/no-poster.svg'; };
            div.appendChild(img);

            const label = document.createElement('div');
            label.className = 'media-label';
            label.textContent = `${item.title || ''}${item.year ? ' (' + item.year + ')' : ''}${item.release_status || ''}`;
            div.appendChild(label);

            link.appendChild(div);
            container.appendChild(link);
        });

        section.appendChild(container);
        chatMessages.appendChild(section);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    /* ── Send message (SSE streaming) ── */

    async function sendMessage() {
        const userMessage = userInput.value.trim();
        if (!userMessage) return;

        addMessage('user', userMessage);
        userInput.value = '';
        userInput.focus();
        sendButton.disabled = true;

        let thinkingPanel = null;
        let streamBubble = null;
        let hasTokens = false;

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
            const response = await fetch('/chat_api', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ message: userMessage }),
            });

            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            const reader = response.body.getReader();
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

                    if (data.type === 'tool_call') {
                        if (!thinkingPanel) thinkingPanel = createThinkingPanel();
                        addToolLine(thinkingPanel, data.label);

                    } else if (data.type === 'token') {
                        if (!streamBubble) streamBubble = createStreamingBubble();
                        appendToken(streamBubble, data.content);
                        hasTokens = true;

                    } else if (data.type === 'final') {
                        if (streamBubble) {
                            streamBubble.body.innerHTML = renderMarkdown(streamBubble.raw);
                        } else if (data.reply) {
                            addMessage('bot', data.reply);
                        }

                        const hasMeta = (data.movies?.length > 0) || (data.tv_shows?.length > 0);
                        if (hasMeta) {
                            if (data.movies?.length > 0) displayMedia(data.movies, 'Movies');
                            if (data.tv_shows?.length > 0) displayMedia(data.tv_shows, 'TV Shows');
                        }
                    } else if (data.type === 'error') {
                        if (streamBubble) streamBubble.body.innerHTML = '';
                        addMessage('bot', `Sorry, something went wrong: ${data.error}`);
                    }
                }
            }
        } catch (err) {
            addMessage('bot', 'Sorry, something went wrong. Please try again.');
        } finally {
            sendButton.disabled = false;
            userInput.focus();
        }
    }

    /* ── Suggestions ── */

    window.useSuggestion = function (btn) {
        userInput.value = btn.textContent;
        sendMessage();
        // Hide suggestions after first use
        const el = document.getElementById('chat-suggestions');
        if (el) el.style.display = 'none';
    };

    /* ── Init ── */

    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    sendButton.addEventListener('click', sendMessage);

    if (window.lucide) lucide.createIcons();
})();
