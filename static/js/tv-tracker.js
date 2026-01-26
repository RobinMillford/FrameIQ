/**
 * TV Show Episode Tracking System - Clean Implementation
 * Handles progress tracking, status changes, and episode marking
 */

class TVTracker {
    constructor(showId) {
        this.showId = showId;
        this.progress = null;
        this.watchedEpisodes = [];
        console.log('TVTracker initialized for show:', showId);
    }

    async initialize() {
        console.log('TVTracker: Loading progress...');
        await this.loadProgress();
        this.renderTrackingUI();
    }

    async loadProgress() {
        try {
            const timestamp = Date.now();
            const response = await fetch(`/api/tv/${this.showId}/progress?_=${timestamp}`);
            const data = await response.json();
            
            this.progress = data.progress;
            this.watchedEpisodes = data.watched_episodes || [];
            
            console.log('TVTracker: Progress loaded', {
                hasProgress: !!this.progress,
                episodeCount: this.watchedEpisodes.length
            });
        } catch (error) {
            console.error('TVTracker: Error loading progress:', error);
        }
    }

    async startTracking() {
        try {
            console.log('TVTracker: Starting tracking...');
            const response = await fetch(`/api/tv/${this.showId}/start-tracking`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.progress = data.progress;
                this.renderTrackingUI();
                this.showNotification('✓ Started tracking show!', 'success');
                
                // Notify seasons manager
                if (window.tvSeasonsManager) {
                    await window.tvSeasonsManager.refresh();
                }
            } else {
                this.showNotification('Error: ' + (data.error || 'Failed to start tracking'), 'error');
            }
        } catch (error) {
            console.error('TVTracker: Error starting tracking:', error);
            this.showNotification('Failed to start tracking', 'error');
        }
    }

    async changeStatus(newStatus) {
        try {
            console.log('TVTracker: Changing status to:', newStatus);
            const response = await fetch(`/api/tv/${this.showId}/status`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ status: newStatus })
            });
            
            const data = await response.json();
            
            if (data.success) {
                this.progress = data.progress;
                this.renderTrackingUI();
                this.showNotification(`✓ Status changed to ${newStatus}`, 'success');
            } else {
                this.showNotification('Error: ' + (data.error || 'Failed to change status'), 'error');
            }
        } catch (error) {
            console.error('TVTracker: Error changing status:', error);
            this.showNotification('Failed to change status', 'error');
        }
    }

    renderTrackingUI() {
        const container = document.getElementById('tracking-container');
        if (!container) {
            console.warn('TVTracker: tracking-container not found');
            return;
        }

        if (!this.progress) {
            container.innerHTML = this.renderStartTrackingButton();
        } else {
            container.innerHTML = this.renderProgressCard();
        }

        console.log('TVTracker: UI rendered');
    }

    renderStartTrackingButton() {
        return `
            <div class="bg-gradient-to-br from-indigo-600 to-purple-600 rounded-2xl p-8 text-center shadow-2xl border border-white/10">
                <div class="flex flex-col items-center space-y-4">
                    <div class="w-16 h-16 bg-white/10 rounded-full flex items-center justify-center mb-2">
                        <svg class="w-8 h-8 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
                        </svg>
                    </div>
                    
                    <h3 class="text-2xl font-bold text-white mb-2">Start Tracking This Show</h3>
                    <p class="text-indigo-100 mb-6 max-w-md">Track your progress, mark episodes as watched, and never lose your place</p>
                    
                    <div class="grid grid-cols-3 gap-4 mb-6 w-full max-w-lg">
                        <div class="bg-white/5 rounded-lg p-3 backdrop-blur-sm">
                            <div class="flex items-center justify-center mb-1">
                                <svg class="w-5 h-5 text-indigo-300" fill="currentColor" viewBox="0 0 20 20">
                                    <path d="M7 3a1 1 0 000 2h6a1 1 0 100-2H7zM4 7a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1zM2 11a2 2 0 012-2h12a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4z"/>
                                </svg>
                            </div>
                            <p class="text-xs text-indigo-200 font-semibold">Episode Tracking</p>
                        </div>
                        <div class="bg-white/5 rounded-lg p-3 backdrop-blur-sm">
                            <div class="flex items-center justify-center mb-1">
                                <svg class="w-5 h-5 text-purple-300" fill="currentColor" viewBox="0 0 20 20">
                                    <path fill-rule="evenodd" d="M6 2a1 1 0 00-1 1v1H4a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-1V3a1 1 0 10-2 0v1H7V3a1 1 0 00-1-1zm0 5a1 1 0 000 2h8a1 1 0 100-2H6z" clip-rule="evenodd"/>
                                </svg>
                            </div>
                            <p class="text-xs text-purple-200 font-semibold">Calendar System</p>
                        </div>
                        <div class="bg-white/5 rounded-lg p-3 backdrop-blur-sm">
                            <div class="flex items-center justify-center mb-1">
                                <svg class="w-5 h-5 text-pink-300" fill="currentColor" viewBox="0 0 20 20">
                                    <path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zM8 7a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zM14 4a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"/>
                                </svg>
                            </div>
                            <p class="text-xs text-pink-200 font-semibold">Progress Stats</p>
                        </div>
                    </div>
                    
                    <button onclick="tvTracker.startTracking()" 
                            class="px-8 py-4 bg-white text-indigo-600 rounded-xl font-bold text-lg hover:bg-indigo-50 transition-all shadow-xl hover:shadow-2xl transform hover:scale-105">
                        ✨ Start Tracking
                    </button>
                </div>
            </div>
        `;
    }

    renderProgressCard() {
        const statusConfig = {
            'watching': { color: 'green', icon: '▶️', label: 'Watching' },
            'plan_to_watch': { color: 'blue', icon: '📋', label: 'Plan to Watch' },
            'completed': { color: 'purple', icon: '✅', label: 'Completed' },
            'on_hold': { color: 'yellow', icon: '⏸️', label: 'On Hold' },
            'dropped': { color: 'red', icon: '🚫', label: 'Dropped' }
        };

        const config = statusConfig[this.progress.status] || statusConfig['watching'];
        const progressPercent = this.progress.total_episodes > 0 
            ? Math.round((this.progress.watched_episodes / this.progress.total_episodes) * 100) 
            : 0;

        return `
            <div class="bg-gradient-to-br from-gray-800 to-gray-900 rounded-2xl p-6 border border-white/10 shadow-2xl">
                <div class="flex items-center justify-between mb-6">
                    <div class="flex items-center space-x-3">
                        <div class="w-10 h-10 bg-gradient-to-br from-orange-500 to-pink-500 rounded-full flex items-center justify-center">
                            <svg class="w-5 h-5 text-white" fill="currentColor" viewBox="0 0 20 20">
                                <path d="M10 12a2 2 0 100-4 2 2 0 000 4z"/>
                                <path fill-rule="evenodd" d="M.458 10C1.732 5.943 5.522 3 10 3s8.268 2.943 9.542 7c-1.274 4.057-5.064 7-9.542 7S1.732 14.057.458 10zM14 10a4 4 0 11-8 0 4 4 0 018 0z" clip-rule="evenodd"/>
                            </svg>
                        </div>
                        <h3 class="text-xl font-bold text-white">Your Progress</h3>
                    </div>
                    <a href="/tv/dashboard" class="text-indigo-400 hover:text-indigo-300 text-sm font-semibold transition-colors flex items-center gap-1">
                        View All
                        <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z" clip-rule="evenodd"/>
                        </svg>
                    </a>
                </div>

                <div class="mb-6">
                    <span class="px-4 py-2 bg-${config.color}-500/20 text-${config.color}-400 rounded-full text-sm font-bold inline-flex items-center gap-2">
                        <span>${config.icon}</span>
                        ${config.label}
                    </span>
                </div>

                <div class="grid grid-cols-2 gap-4 mb-6">
                    <div class="bg-gradient-to-br from-indigo-600/20 to-purple-600/20 rounded-xl p-4 border border-indigo-500/20">
                        <div class="flex items-center gap-2 mb-1">
                            <svg class="w-4 h-4 text-indigo-400" fill="currentColor" viewBox="0 0 20 20">
                                <path d="M7 3a1 1 0 000 2h6a1 1 0 100-2H7zM4 7a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1zM2 11a2 2 0 012-2h12a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4z"/>
                            </svg>
                            <p class="text-sm text-indigo-300 font-medium">Episodes</p>
                        </div>
                        <p class="text-3xl font-bold text-white">${this.progress.watched_episodes}<span class="text-lg text-gray-400">/${this.progress.total_episodes}</span></p>
                    </div>
                    
                    <div class="bg-gradient-to-br from-purple-600/20 to-pink-600/20 rounded-xl p-4 border border-purple-500/20">
                        <div class="flex items-center gap-2 mb-1">
                            <svg class="w-4 h-4 text-purple-400" fill="currentColor" viewBox="0 0 20 20">
                                <path fill-rule="evenodd" d="M3 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm0 4a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1z" clip-rule="evenodd"/>
                            </svg>
                            <p class="text-sm text-purple-300 font-medium">Seasons</p>
                        </div>
                        <p class="text-3xl font-bold text-white">${this.progress.watched_seasons}<span class="text-lg text-gray-400">/${this.progress.total_seasons}</span></p>
                    </div>
                </div>

                <div class="mb-6">
                    <div class="flex items-center justify-between mb-2">
                        <span class="text-sm font-semibold text-gray-300">Overall Progress</span>
                        <span class="text-lg font-bold text-white">${progressPercent}%</span>
                    </div>
                    <div class="h-3 bg-gray-700 rounded-full overflow-hidden">
                        <div class="h-full bg-gradient-to-r from-green-500 via-blue-500 to-purple-500 transition-all duration-500 ease-out" 
                             style="width: ${progressPercent}%"></div>
                    </div>
                </div>

                <div class="flex gap-3">
                    <button onclick="window.location.href='/tv/upcoming'" 
                            class="flex-1 px-4 py-3 bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-500 hover:to-purple-500 text-white rounded-xl font-semibold transition-all shadow-lg hover:shadow-xl transform hover:scale-105 flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                            <path fill-rule="evenodd" d="M6 2a1 1 0 00-1 1v1H4a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V6a2 2 0 00-2-2h-1V3a1 1 0 10-2 0v1H7V3a1 1 0 00-1-1zm0 5a1 1 0 000 2h8a1 1 0 100-2H6z" clip-rule="evenodd"/>
                        </svg>
                        📅 Calendar
                    </button>
                    <button onclick="tvTracker.openStatusModal()" 
                            class="flex-1 px-4 py-3 bg-gray-700 hover:bg-gray-600 text-white rounded-xl font-semibold transition-all flex items-center justify-center gap-2">
                        <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                            <path d="M4 3a2 2 0 100 4h12a2 2 0 100-4H4z"/>
                            <path fill-rule="evenodd" d="M3 8h14v7a2 2 0 01-2 2H5a2 2 0 01-2-2V8zm5 3a1 1 0 011-1h2a1 1 0 110 2H9a1 1 0 01-1-1z" clip-rule="evenodd"/>
                        </svg>
                        🔄 Change Status
                    </button>
                </div>
            </div>
        `;
    }

    openStatusModal() {
        const statuses = [
            { value: 'watching', label: 'Watching', icon: '▶️', color: 'green' },
            { value: 'plan_to_watch', label: 'Plan to Watch', icon: '📋', color: 'blue' },
            { value: 'completed', label: 'Completed', icon: '✅', color: 'purple' },
            { value: 'on_hold', label: 'On Hold', icon: '⏸️', color: 'yellow' },
            { value: 'dropped', label: 'Dropped', icon: '🚫', color: 'red' }
        ];

        const modal = document.createElement('div');
        modal.className = 'fixed inset-0 bg-black/70 backdrop-blur-sm z-50 flex items-center justify-center p-4';
        modal.innerHTML = `
            <div class="bg-gray-800 rounded-2xl p-6 max-w-md w-full shadow-2xl border border-white/10">
                <h3 class="text-2xl font-bold text-white mb-4">Change Status</h3>
                <div class="space-y-2">
                    ${statuses.map(status => `
                        <button onclick="tvTracker.changeStatus('${status.value}'); this.closest('.fixed').remove();"
                                class="w-full px-4 py-3 bg-gray-700 hover:bg-${status.color}-600 text-white rounded-lg transition-all text-left flex items-center gap-3 ${this.progress.status === status.value ? 'ring-2 ring-' + status.color + '-500' : ''}">
                            <span class="text-2xl">${status.icon}</span>
                            <span class="font-semibold">${status.label}</span>
                            ${this.progress.status === status.value ? '<span class="ml-auto text-xs bg-white/20 px-2 py-1 rounded">Current</span>' : ''}
                        </button>
                    `).join('')}
                </div>
                <button onclick="this.closest('.fixed').remove()" 
                        class="mt-4 w-full px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-all">
                    Cancel
                </button>
            </div>
        `;

        document.body.appendChild(modal);
    }

    showNotification(message, type = 'info') {
        const colors = {
            success: 'bg-green-500',
            error: 'bg-red-500',
            info: 'bg-indigo-500'
        };

        const notification = document.createElement('div');
        notification.className = `fixed bottom-4 right-4 ${colors[type]} text-white px-6 py-4 rounded-xl shadow-2xl z-[100] flex items-center gap-3`;
        notification.innerHTML = `
            <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 20 20">
                <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clip-rule="evenodd"/>
            </svg>
            <span class="font-semibold">${message}</span>
        `;
        document.body.appendChild(notification);

        setTimeout(() => notification.remove(), 3000);
    }
}

// Initialize when DOM is ready
let tvTracker;
document.addEventListener('DOMContentLoaded', function() {
    const showIdElement = document.getElementById('show-id');
    if (showIdElement) {
        const showId = parseInt(showIdElement.value);
        tvTracker = new TVTracker(showId);
        window.tvTracker = tvTracker;
        tvTracker.initialize();
    }
});
