// File: static/js/tag-manager.js
// Letterboxd-style tag management for FrameIQ

/**
 * Build the static "remove tag" icon with DOM APIs.
 *
 * F21: the previous implementation inlined this SVG through an innerHTML
 * string. The markup is ours and contains no dynamic data, but building it
 * with createElementNS means this file contains no innerHTML assignment at
 * all — a property that is trivial to audit and cannot regress.
 */
function buildTrashIcon() {
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'w-3 h-3');
    svg.setAttribute('fill', 'currentColor');
    svg.setAttribute('viewBox', '0 0 20 20');
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('fill-rule', 'evenodd');
    path.setAttribute('clip-rule', 'evenodd');
    path.setAttribute('d',
        'M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10' +
        'l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414' +
        'L8.586 10 4.293 5.707a1 1 0 010-1.414z');
    svg.appendChild(path);
    return svg;
}

class TagManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.tags = [];
        this.allTags = [];
        this.userTags = [];
        this.init();
    }

    async init() {
        await this.loadTags();
        await this.loadUserTags();
        this.renderTags();
        this.setupEventListeners();
    }

    async loadTags() {
        try {
            const userId = document.body.dataset.userId;
            if (!userId) return;

            const response = await fetch(`/api/media/${this.mediaId}/tags?media_type=${this.mediaType}&user_id=${userId}`);
            const data = await response.json();
            this.tags = data.tags || [];
        } catch (error) {
        }
    }

    async loadUserTags() {
        try {
            const userId = document.body.dataset.userId;
            if (!userId) return;

            const response = await fetch(`/api/users/${userId}/tags`);
            const data = await response.json();
            this.userTags = data.tags || [];
        } catch (error) {
        }
    }

    async searchTags(query) {
        if (!query) return [];
        
        try {
            const response = await fetch(`/api/tags/search?q=${encodeURIComponent(query)}&limit=10`);
            const data = await response.json();
            return data.tags || [];
        } catch (error) {
            return [];
        }
    }

    async addTag(tagName) {
        tagName = tagName.trim().toLowerCase();
        
        if (!tagName) return;
        if (tagName.length > 30) {
            this.showError('Tag must be 30 characters or less');
            return;
        }
        if (this.tags.some(t => t.name === tagName)) {
            this.showError('Tag already added');
            return;
        }
        if (this.tags.length >= 20) {
            this.showError('Maximum 20 tags allowed');
            return;
        }

        try {
            const response = await fetch(`/api/media/${this.mediaId}/tags`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    tags: [tagName],
                    media_type: this.mediaType
                })
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.error || 'Failed to add tag');
            }

            const data = await response.json();
            if (data.tags && data.tags.length > 0) {
                this.tags.push(data.tags[0]);
                this.renderTags();
                this.clearInput();
                this.showSuccess('Tag added!');
            }
        } catch (error) {
            this.showError(error.message);
        }
    }

    async removeTag(tagId) {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/tags/${tagId}?media_type=${this.mediaType}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                throw new Error('Failed to remove tag');
            }

            this.tags = this.tags.filter(t => t.id !== tagId);
            this.renderTags();
            this.showSuccess('Tag removed');
        } catch (error) {
            this.showError(error.message);
        }
    }

    // F21 — helpers come from static/js/dom-safety.js, which must be loaded
    // before this file. They place untrusted values via textContent and
    // validate every URL, so no value here is ever parsed as markup.
    dom() {
        return window.FrameIQDOM;
    }

    createTagPill(tag) {
        const D = this.dom();
        const pill = D.el('span', {
            class: 'tag-pill inline-flex items-center gap-1 bg-indigo-600 hover:bg-indigo-700 ' +
                   'text-white px-3 py-1 rounded-full text-sm transition-colors',
        });
        pill.appendChild(D.el('span', { text: tag.name }));

        const tagId = D.safeId(tag.id);
        const removeBtn = D.el('button', {
            class: 'hover:text-red-300 transition-colors',
            type: 'button',
            'aria-label': 'Remove tag',
            title: 'Remove tag',
        });
        removeBtn.appendChild(buildTrashIcon());
        // Behaviour via a closure over the validated id — never an inline
        // handler string, so the tag name can never reach an expression.
        if (tagId !== null) {
            removeBtn.addEventListener('click', () => this.removeTag(tagId));
        } else {
            removeBtn.disabled = true;
            removeBtn.setAttribute('aria-disabled', 'true');
        }
        pill.appendChild(removeBtn);
        return pill;
    }

    renderTags() {
        const D = this.dom();
        const container = document.getElementById('tags-display');
        if (!container) return;

        D.clear(container);

        if (this.tags.length === 0) {
            container.appendChild(D.el('p', {
                class: 'text-gray-400 text-sm',
                text: 'No tags yet. Add tags to organize your media!',
            }));
            return;
        }

        this.tags.forEach(tag => container.appendChild(this.createTagPill(tag)));
    }

    setupEventListeners() {
        const input = document.getElementById('tag-input');
        const suggestionsDiv = document.getElementById('tag-suggestions');
        
        if (!input) return;

        // Add tag on Enter
        input.addEventListener('keydown', async (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const tagName = input.value.trim();
                if (tagName) {
                    await this.addTag(tagName);
                }
            }
        });

        // Autocomplete
        let debounceTimer;
        input.addEventListener('input', async (e) => {
            clearTimeout(debounceTimer);
            const query = e.target.value.trim();

            if (!query) {
                suggestionsDiv.classList.add('hidden');
                return;
            }

            debounceTimer = setTimeout(async () => {
                await this.showSuggestions(query);
            }, 300);
        });

        // Hide suggestions when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.tag-input-container')) {
                suggestionsDiv.classList.add('hidden');
            }
        });
    }

    async showSuggestions(query) {
        const D = this.dom();
        const suggestionsDiv = document.getElementById('tag-suggestions');
        if (!suggestionsDiv) return;

        // Combine user's tags and search results
        const userMatches = this.userTags.filter(t =>
            t.name.includes(query.toLowerCase()) &&
            !this.tags.some(existingTag => existingTag.id === t.id)
        ).slice(0, 5);

        const searchResults = await this.searchTags(query);
        const popularMatches = searchResults.filter(t =>
            !userMatches.some(ut => ut.id === t.id) &&
            !this.tags.some(existingTag => existingTag.id === t.id)
        ).slice(0, 5);

        const allSuggestions = [...userMatches, ...popularMatches];

        D.clear(suggestionsDiv);

        if (allSuggestions.length === 0) {
            // The query is typed text — rendered as text, never as markup.
            const empty = D.el('div', { class: 'p-2 text-gray-400 text-sm' });
            empty.appendChild(document.createTextNode('No suggestions. Press Enter to create "'));
            empty.appendChild(D.el('span', { text: query }));
            empty.appendChild(document.createTextNode('"'));
            suggestionsDiv.appendChild(empty);
            suggestionsDiv.classList.remove('hidden');
            return;
        }

        allSuggestions.forEach(tag => {
            const name = D.text(tag.name);
            const btn = D.el('button', {
                class: 'w-full text-left px-3 py-2 hover:bg-gray-700 flex items-center justify-between',
                type: 'button',
            });
            btn.appendChild(D.el('span', { text: tag.name }));
            btn.appendChild(D.el('span', {
                class: 'text-xs text-gray-400',
                text: (D.safeId(tag.usage_count) ?? 0) + ' uses',
            }));
            // Closure over the tag object: the name never becomes code.
            btn.addEventListener('click', () => this.addTag(name));
            suggestionsDiv.appendChild(btn);
        });

        suggestionsDiv.classList.remove('hidden');
    }

    clearInput() {
        const input = document.getElementById('tag-input');
        const suggestionsDiv = document.getElementById('tag-suggestions');
        if (input) input.value = '';
        if (suggestionsDiv) suggestionsDiv.classList.add('hidden');
    }

    showError(message) {
        const errorDiv = document.getElementById('tag-error');
        if (errorDiv) {
            errorDiv.textContent = message;
            errorDiv.classList.remove('hidden');
            setTimeout(() => errorDiv.classList.add('hidden'), 3000);
        }
    }

    showSuccess(message) {
        const successDiv = document.getElementById('tag-success');
        if (successDiv) {
            successDiv.textContent = message;
            successDiv.classList.remove('hidden');
            setTimeout(() => successDiv.classList.add('hidden'), 2000);
        }
    }
}

// Initialize tag manager when page loads
let tagManager;
document.addEventListener('DOMContentLoaded', () => {
    const mediaId = document.body.dataset.mediaId;
    const mediaType = document.body.dataset.mediaType;
    
    if (mediaId && mediaType) {
        tagManager = new TagManager(mediaId, mediaType);
    }
});
