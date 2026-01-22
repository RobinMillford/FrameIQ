// File: static/js/tag-manager.js
// Letterboxd-style tag management for FrameIQ

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
            console.error('Error loading tags:', error);
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
            console.error('Error loading user tags:', error);
        }
    }

    async searchTags(query) {
        if (!query) return [];
        
        try {
            const response = await fetch(`/api/tags/search?q=${encodeURIComponent(query)}&limit=10`);
            const data = await response.json();
            return data.tags || [];
        } catch (error) {
            console.error('Error searching tags:', error);
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

    renderTags() {
        const container = document.getElementById('tags-display');
        if (!container) return;

        if (this.tags.length === 0) {
            container.innerHTML = '<p class="text-gray-400 text-sm">No tags yet. Add tags to organize your media!</p>';
            return;
        }

        container.innerHTML = this.tags.map(tag => `
            <span class="tag-pill inline-flex items-center gap-1 bg-indigo-600 hover:bg-indigo-700 text-white px-3 py-1 rounded-full text-sm transition-colors">
                <span>${this.escapeHtml(tag.name)}</span>
                <button onclick="tagManager.removeTag(${tag.id})" class="hover:text-red-300 transition-colors">
                    <svg class="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
                        <path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd"></path>
                    </svg>
                </button>
            </span>
        `).join('');
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

        if (allSuggestions.length === 0) {
            suggestionsDiv.innerHTML = `
                <div class="p-2 text-gray-400 text-sm">
                    No suggestions. Press Enter to create "${this.escapeHtml(query)}"
                </div>
            `;
            suggestionsDiv.classList.remove('hidden');
            return;
        }

        suggestionsDiv.innerHTML = allSuggestions.map(tag => `
            <button onclick="tagManager.addTag('${this.escapeHtml(tag.name)}')" 
                    class="w-full text-left px-3 py-2 hover:bg-gray-700 flex items-center justify-between">
                <span>${this.escapeHtml(tag.name)}</span>
                <span class="text-xs text-gray-400">${tag.usage_count || 0} uses</span>
            </button>
        `).join('');

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

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
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
