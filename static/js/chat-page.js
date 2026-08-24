/**
 * CineBot chat page behaviors.
 * Requires window.__CHAT_BOOTSTRAP__ = { mobileAuthLinks } rendered by the template.
 */
const BOOT = window.__CHAT_BOOTSTRAP__ || {};
      const chatMessages = document.getElementById("chat-messages");
      const userInput = document.getElementById("user-input");
      const sendButton = document.getElementById("send-button");
      const BOT_NAME = "CineBot";

      // Markdown → sanitized HTML for bot messages
      function renderMarkdown(text) {
        if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
          return null; // CDN unavailable — caller falls back to textContent
        }
        return DOMPurify.sanitize(marked.parse(text || ""));
      }

      function addMessage(sender, text) {
        const messageElement = document.createElement("div");
        messageElement.classList.add("message", sender);

        // Add bot name to bot messages
        if (sender === "bot") {
          const botNameElement = document.createElement("div");
          botNameElement.classList.add(
            "font-semibold",
            "text-indigo-300",
            "mb-1",
          );
          botNameElement.textContent = BOT_NAME;
          messageElement.appendChild(botNameElement);

          const html = renderMarkdown(text);
          if (html !== null) {
            const textElement = document.createElement("div");
            textElement.innerHTML = html;
            messageElement.appendChild(textElement);
          } else {
            const textElement = document.createElement("div");
            textElement.textContent = text;
            messageElement.appendChild(textElement);
          }
        } else {
          messageElement.textContent = text;
        }

        chatMessages.appendChild(messageElement);

        // Scroll to the latest message
        chatMessages.scrollTop = chatMessages.scrollHeight;
      }

        function createThinkingPanel() {
            const panel = document.createElement("details");
            panel.classList.add("thinking-panel");
            panel.innerHTML = `<summary>Agent is thinking…</summary>`;
            chatMessages.appendChild(panel);
            chatMessages.scrollTop = chatMessages.scrollHeight;
            return panel;
        }

        function addToolLine(panel, label, input) {
            const line = document.createElement("div");
            line.classList.add("tool-line");
            line.textContent = input ? `${label}  (${input})` : label;
            panel.appendChild(line);
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }

        function createStreamingBubble() {
            const wrap = document.createElement("div");
            wrap.classList.add("message", "bot");
            const nameEl = document.createElement("div");
            nameEl.classList.add("font-semibold", "text-indigo-300", "mb-1");
            nameEl.textContent = BOT_NAME;
            const textEl = document.createElement("div");
            textEl.classList.add("stream-text");
            const cursor = document.createElement("span");
            cursor.classList.add("cursor-blink");
            textEl.appendChild(cursor);
            wrap.appendChild(nameEl);
            wrap.appendChild(textEl);
            chatMessages.appendChild(wrap);
            chatMessages.scrollTop = chatMessages.scrollHeight;
            return { wrap, textEl, cursor, raw: "" };
        }

        function appendToken(bubble, token) {
            bubble.raw += token;
            const html = renderMarkdown(bubble.raw);
            if (html !== null) {
                // Re-render markdown live; keep cursor pinned at the end
                bubble.textEl.innerHTML = html;
                bubble.textEl.appendChild(bubble.cursor);
            } else {
                bubble.textEl.insertBefore(document.createTextNode(token), bubble.cursor);
            }
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }

        async function sendMessage() {
            const userMessage = userInput.value.trim();
            if (!userMessage) return;

            addMessage("user", userMessage);
            userInput.value = "";
            userInput.focus();
            sendButton.disabled = true;

            let thinkingPanel = null;
            let streamBubble = null;
            let hasTokens = false;

            try {
                const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const response = await fetch("/chat_api", {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
                    body: JSON.stringify({ message: userMessage })
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
                            addToolLine(thinkingPanel, data.label, data.input);

                        } else if (data.type === 'token') {
                            if (!streamBubble) streamBubble = createStreamingBubble();
                            appendToken(streamBubble, data.content);
                            hasTokens = true;

                        } else if (data.type === 'final') {
                            // Remove blinking cursor
                            if (streamBubble) streamBubble.cursor.remove();
                            if (!hasTokens && data.reply) addMessage("bot", data.reply);
                            // Collapse thinking panel
                            if (thinkingPanel) thinkingPanel.open = false;
                            // Posters
                            const hasMeta = (data.movies?.length > 0) || (data.tv_shows?.length > 0);
                            if (hasMeta) {
                                const mc = document.createElement("div");
                                mc.classList.add("media-container");
                                if (data.movies?.length > 0) displayMedia(data.movies, "Movies", mc);
                                if (data.tv_shows?.length > 0) displayMedia(data.tv_shows, "TV Shows", mc);
                                chatMessages.appendChild(mc);
                                chatMessages.scrollTop = chatMessages.scrollHeight;
                            }

                        } else if (data.type === 'error') {
                            if (streamBubble) streamBubble.cursor.remove();
                            addMessage("bot", `Sorry, something went wrong: ${data.error}`);
                        }
                    }
                }
            } catch (err) {
                if (streamBubble) streamBubble.cursor.remove();
                addMessage("bot", "Sorry, something went wrong. Please try again.");
            } finally {
                sendButton.disabled = false;
            }
        }
      function displayMedia(items, type, container) {
        let section = document.createElement("div");
        section.classList.add("media-section");

        let title = document.createElement("div");
        title.classList.add("media-title");
        title.textContent = type;

        let itemsContainer = document.createElement("div");
        itemsContainer.classList.add("media-items");

        items.forEach((item) => {
          const mediaElement = document.createElement("div");
          mediaElement.classList.add("media");

          const link = document.createElement("a");
          link.href = item.tmdb_link || "#";

          const img = document.createElement("img");
          img.src = item.poster_url || "";
          img.alt = item.title || "";
          img.onerror = function() { this.src = "/static/images/no-poster.svg"; };

          const titleDiv = document.createElement("div");
          titleDiv.className = "media-title-text";
          titleDiv.textContent = `${item.title || ""}${item.year ? " (" + item.year + ")" : ""}${item.release_status || ""}`;

          link.appendChild(img);
          link.appendChild(titleDiv);
          mediaElement.appendChild(link);
          itemsContainer.appendChild(mediaElement);
        });

        section.appendChild(title);
        section.appendChild(itemsContainer);
        container.appendChild(section);
      }

      // Allow Shift + Enter for multiline input
      userInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault(); // Prevents new line
          sendMessage();
        }
      });

      sendButton.addEventListener("click", sendMessage);

      // Setup mobile menu
      function setupMobileMenu() {
        const mobileMenuButton = document.getElementById("mobile-menu-button");
        const mobileMenu = document.createElement("div");
        mobileMenu.className = "fixed top-16 left-0 right-0 bg-[#1A4A4F] glass-effect hidden z-40";
        mobileMenu.innerHTML = `
                <div class="container mx-auto px-4 py-4">
                    <div class="flex flex-col space-y-4">
                        <a href="/" class="text-white hover:text-[#00C4CC]">Home</a>
                        <a href="/movies" class="text-white hover:text-[#00C4CC]">Movies</a>
                        <a href="/tv_shows" class="text-white hover:text-[#00C4CC]">TV Shows</a>
                        <a href="/news" class="text-white hover:text-[#00C4CC]">News</a>
                        <a href="/feed" class="text-white hover:text-[#00C4CC]">Social Feed</a>
                        ${BOOT.mobileAuthLinks}
                    </div>
                </div>
            `;
        document.body.appendChild(mobileMenu);

        mobileMenuButton.addEventListener("click", () => {
          mobileMenu.classList.toggle("hidden");
        });
      }

      document.addEventListener("DOMContentLoaded", function () {
        setupMobileMenu();
        
        // Profile dropdown
        const profileButton = document.getElementById('profile-button');
        const profileMenu = document.getElementById('profile-menu');

        if (profileButton && profileMenu) {
          profileButton.addEventListener('click', function(e) {
            e.stopPropagation();
            profileMenu.classList.toggle('hidden');
          });

          document.addEventListener('click', function(e) {
            if (!profileButton.contains(e.target) && !profileMenu.contains(e.target)) {
              profileMenu.classList.add('hidden');
            }
          });

          document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
              profileMenu.classList.add('hidden');
            }
          });
        }
        
        // Add welcome message
        setTimeout(() => {
          addMessage(
            "bot",
            `Hello! I'm ${BOT_NAME}, your FrameIQ AI Assistant. Ask me about movies, TV shows, or get personalized recommendations!`,
          );
        }, 500);
      });
