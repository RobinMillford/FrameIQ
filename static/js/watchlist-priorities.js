// File: static/js/watchlist-priorities.js
// Priority management for watchlist items

class WatchlistPrioritiesManager {
    constructor() {
        this.init();
    }

    async init() {
        await this.loadStats();
        this.setupEventListeners();
        this.updateAllPriorityBadges();
    }

    async loadStats() {
        try {
            const response = await fetch('/api/watchlist/stats');
            const stats = await response.json();
            this.updateStatsDisplay(stats);
        } catch (error) {
            console.error('Error loading watchlist stats:', error);
        }
    }

    updateStatsDisplay(stats) {
        const statsContainer = document.getElementById('priority-stats');
        if (!statsContainer) return;

        statsContainer.innerHTML = `
            <div class="flex flex-wrap gap-4 text-sm">
                <div class="flex items-center gap-2">
                    <span class="priority-badge priority-high">HIGH</span>
                    <span class="text-gray-300">${stats.high}</span>
                </div>
                <div class="flex items-center gap-2">
                    <span class="priority-badge priority-medium">MEDIUM</span>
                    <span class="text-gray-300">${stats.medium}</span>
                </div>
                <div class="flex items-center gap-2">
                    <span class="priority-badge priority-low">LOW</span>
                    <span class="text-gray-300">${stats.low}</span>
                </div>
                <div class="flex items-center gap-2">
                    <span class="text-gray-400">Total:</span>
                    <span class="text-white font-semibold">${stats.total}</span>
                </div>
            </div>
        `;
    }

    setupEventListeners() {
        // Priority dropdown changes
        document.addEventListener('change', (e) => {
            if (e.target.classList.contains('priority-select')) {
                const mediaId = e.target.dataset.mediaId;
                const mediaType = e.target.dataset.mediaType;
                const priority = e.target.value;
                this.updatePriority(mediaId, mediaType, priority);
            }
        });

        // Filter buttons
        const filterButtons = document.querySelectorAll('.priority-filter-btn');
        filterButtons.forEach(btn => {
            btn.addEventListener('click', () => {
                this.filterByPriority(btn.dataset.priority);
            });
        });

        // Sort dropdown
        const sortSelect = document.getElementById('watchlist-sort');
        if (sortSelect) {
            sortSelect.addEventListener('change', (e) => {
                this.sortWatchlist(e.target.value);
            });
        }
    }

    async updatePriority(mediaId, mediaType, priority) {
        try {
            const response = await fetch(`/api/watchlist/${mediaId}/priority`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    priority: priority,
                    media_type: mediaType
                })
            });

            const data = await response.json();

            if (response.ok) {
                this.showMessage('Priority updated!', 'success');
                this.updatePriorityBadge(mediaId, priority);
                await this.loadStats();  // Refresh stats
            } else {
                this.showMessage(data.error || 'Failed to update priority', 'error');
            }
        } catch (error) {
            console.error('Error updating priority:', error);
            this.showMessage('An error occurred', 'error');
        }
    }

    updatePriorityBadge(mediaId, priority) {
        const badge = document.querySelector(`[data-media-id="${mediaId}"] .priority-badge`);
        if (badge) {
            badge.className = `priority-badge priority-${priority}`;
            badge.textContent = priority.toUpperCase();
        }
    }

    updateAllPriorityBadges() {
        // Get all priority selects and update their corresponding badges
        const selects = document.querySelectorAll('.priority-select');
        selects.forEach(select => {
            const mediaId = select.dataset.mediaId;
            const priority = select.value;
            this.updatePriorityBadge(mediaId, priority);
        });
    }

    filterByPriority(priority) {
        const cards = document.querySelectorAll('.watchlist-card');
        const filterButtons = document.querySelectorAll('.priority-filter-btn');

        // Update button states
        filterButtons.forEach(btn => {
            if (btn.dataset.priority === priority) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });

        // Filter cards
        cards.forEach(card => {
            if (priority === 'all') {
                card.style.display = '';
            } else {
                const select = card.querySelector('.priority-select');
                if (select && select.value === priority) {
                    card.style.display = '';
                } else {
                    card.style.display = 'none';
                }
            }
        });

        // Update count
        const visibleCards = Array.from(cards).filter(card => card.style.display !== 'none');
        this.updateFilteredCount(visibleCards.length);
    }

    sortWatchlist(sortBy) {
        const container = document.querySelector('.watchlist-grid');
        if (!container) return;

        const cards = Array.from(container.querySelectorAll('.watchlist-card'));

        cards.sort((a, b) => {
            if (sortBy === 'priority') {
                const priorityOrder = { 'high': 1, 'medium': 2, 'low': 3 };
                const aPriority = a.querySelector('.priority-select').value;
                const bPriority = b.querySelector('.priority-select').value;
                return priorityOrder[aPriority] - priorityOrder[bPriority];
            } else if (sortBy === 'date_added') {
                const aDate = a.dataset.dateAdded;
                const bDate = b.dataset.dateAdded;
                return new Date(bDate) - new Date(aDate);
            }
            return 0;
        });

        // Re-append in sorted order
        cards.forEach(card => container.appendChild(card));
    }

    updateFilteredCount(count) {
        const countDisplay = document.getElementById('filtered-count');
        if (countDisplay) {
            countDisplay.textContent = count;
        }
    }

    showMessage(message, type) {
        const messageDiv = document.getElementById('priority-message');
        if (messageDiv) {
            messageDiv.textContent = message;
            messageDiv.className = type === 'success' ? 'text-green-400 text-sm' : 'text-red-400 text-sm';
            messageDiv.classList.remove('hidden');
            setTimeout(() => messageDiv.classList.add('hidden'), 3000);
        }
    }
}

// Initialize on page load
let prioritiesManager;
document.addEventListener('DOMContentLoaded', () => {
    if (document.querySelector('.watchlist-page')) {
        prioritiesManager = new WatchlistPrioritiesManager();
    }
});
