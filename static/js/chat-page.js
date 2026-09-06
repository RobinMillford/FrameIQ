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
    let currentConversationId = null;

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

    function clearMessages() {
        chatMessages.innerHTML = '';
    }

    function showWelcome() {
        addMessage('bot', "Hey — I'm CineBot. Ask me anything about movies, TV shows, or what to watch next.");
    }

    function renderStoredMessage(message) {
        addMessage(message.role === 'user' ? 'user' : 'bot', message.content);
        if (message.role === 'assistant' && message.metadata) {
            if (message.metadata.movies?.length) displayMedia(message.metadata.movies, 'Movies');
            if (message.metadata.tv_shows?.length) displayMedia(message.metadata.tv_shows, 'TV Shows');
        }
    }

    function updateQuota(quota) {
        const el = document.getElementById('chat-quota');
        if (el && quota) {
            el.textContent = `${quota.remaining} of ${quota.limit} questions remaining today`;
        }
    }

    async function loadConversation(id) {
        const response = await fetch(`/chat/conversations/${id}`);
        if (!response.ok) throw new Error('Unable to load conversation');
        const data = await response.json();
        currentConversationId = data.id;
        clearMessages();
        data.messages.forEach(renderStoredMessage);
        document.querySelectorAll('.chat-history-item').forEach((item) => {
            item.classList.toggle('active', item.dataset.conversationId === String(id));
        });
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    async function refreshHistory() {
        const response = await fetch('/chat/conversations');
        if (!response.ok) return;
        const data = await response.json();
        updateQuota(data.quota);
        const history = document.getElementById('chat-history');
        history.innerHTML = '';
        data.conversations.forEach((conversation) => {
            const button = document.createElement('button');
            button.className = 'chat-history-item';
            button.dataset.conversationId = conversation.id;
            button.textContent = conversation.title;
            button.addEventListener('click', () => loadConversation(conversation.id));
            history.appendChild(button);
        });
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
                body: JSON.stringify({
                    message: userMessage,
                    conversation_id: currentConversationId,
                }),
            });

            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                if (error.quota) updateQuota(error.quota);
                throw new Error(error.error || `HTTP ${response.status}`);
            }
            currentConversationId = response.headers.get('X-Chat-Conversation-ID') || currentConversationId;
            updateQuota({
                remaining: Number(response.headers.get('X-Chat-Remaining') || 0),
                limit: 5,
            });

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
            addMessage('bot', `Sorry, ${err.message || 'something went wrong. Please try again.'}`);
        } finally {
            refreshHistory();
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

    document.getElementById('new-chat-button')?.addEventListener('click', () => {
        currentConversationId = null;
        clearMessages();
        showWelcome();
        document.querySelectorAll('.chat-history-item').forEach((item) => item.classList.remove('active'));
        userInput.focus();
    });

    document.querySelectorAll('.chat-history-item').forEach((item) => {
        item.addEventListener('click', () => loadConversation(item.dataset.conversationId));
    });

    refreshHistory();

    if (window.lucide) lucide.createIcons();
})();
