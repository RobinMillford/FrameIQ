/**
 * TV Seasons & Episodes Manager - Clean Implementation
 * Handles season overview, episode tracking with real-time database sync
 */

console.log('TV Seasons script loaded!');

class TVSeasonsManager {
    constructor(showId, apiKey) {
        this.showId = showId;
        this.apiKey = apiKey;
        this.seasons = [];
        this.watchedEpisodes = new Set();
        this.showProgress = null;
        console.log('TVSeasonsManager initialized', { showId, hasApiKey: !!apiKey });
    }

    async initialize() {
        try {
            console.log('TVSeasonsManager: Initializing...');
            const container = document.getElementById('seasons-list');
            if (container) {
                container.innerHTML = '<div class="text-center py-8"><div class="animate-spin w-12 h-12 border-4 border-indigo-500 border-t-transparent rounded-full mx-auto"></div><p class="text-gray-400 mt-4">Loading seasons...</p></div>';
            }

            await Promise.all([
                this.loadShowDetails(),
                this.loadWatchedEpisodes(),
                this.loadShowProgress()
            ]);

            console.log('TVSeasonsManager: Data loaded', {
                seasons: this.seasons.length,
                watchedEpisodes: this.watchedEpisodes.size,
                hasProgress: !!this.showProgress
            });

            this.renderSeasonsList();
        } catch (error) {
            console.error('TVSeasonsManager: Initialization error:', error);
            const container = document.getElementById('seasons-list');
            if (container) {
                container.innerHTML = `
                    <div class="text-center py-8 text-red-400">
                        <p class="font-semibold mb-2">Error loading seasons</p>
                        <p class="text-sm text-gray-400 mb-4">${error.message}</p>
                        <button onclick="location.reload()" class="px-4 py-2 bg-indigo-600 rounded-lg hover:bg-indigo-700">
                            Retry
                        </button>
                    </div>
                `;
            }
        }
    }

    async loadShowDetails() {
        console.log('TVSeasonsManager: Fetching show details from TMDb...');
        const response = await fetch(`https://api.themoviedb.org/3/tv/${this.showId}?api_key=${this.apiKey}`);
        
        if (!response.ok) {
            throw new Error(`TMDb API error: ${response.status}`);
        }
        
        const data = await response.json();
        this.seasons = data.seasons.filter(s => s.season_number !== 0);
        console.log(`TVSeasonsManager: Loaded ${this.seasons.length} seasons`);
    }

    async loadWatchedEpisodes() {
        console.log('TVSeasonsManager: Loading watched episodes from database...');
        this.watchedEpisodes.clear();
        
        const timestamp = Date.now();
        console.log(`TVSeasonsManager: Fetching /api/tv/${this.showId}/watched-episodes?_=${timestamp}`);
        
        const response = await fetch(`/api/tv/${this.showId}/watched-episodes?_=${timestamp}`);
        const data = await response.json();
        
        console.log('TVSeasonsManager: API response:', data);
        
        if (data.success) {
            data.episodes.forEach(ep => {
                this.watchedEpisodes.add(`${ep.season_number}-${ep.episode_number}`);
            });
            console.log(`TVSeasonsManager: Loaded ${data.episodes.length} watched episodes from database`);
            console.log('TVSeasonsManager: Watched episodes set:', Array.from(this.watchedEpisodes));
        } else {
            console.error('TVSeasonsManager: Failed to load episodes:', data.error);
        }
    }

    async loadShowProgress() {
        console.log('TVSeasonsManager: Loading progress from database...');
        const timestamp = Date.now();
        const response = await fetch(`/api/tv/${this.showId}/progress?_=${timestamp}`);
        const data = await response.json();
        
        this.showProgress = data.progress;
        console.log('TVSeasonsManager: Progress loaded from database:', this.showProgress);
    }

    async refresh() {
        console.log('TVSeasonsManager: Refreshing data from database...');
        await this.loadWatchedEpisodes();
        await this.loadShowProgress();
        this.renderSeasonsList();
    }

    renderSeasonsList() {
        const container = document.getElementById('seasons-list');
        
        if (!container) {
            console.error('TVSeasonsManager: seasons-list container not found');
            return;
        }
        
        if (this.seasons.length === 0) {
            container.innerHTML = '<p class="text-gray-400 text-center py-4">No seasons available</p>';
            return;
        }

        console.log('TVSeasonsManager: Rendering seasons...');
        
        container.innerHTML = this.seasons.map(season => {
            const watchedCount = this.getWatchedCountForSeason(season.season_number);
            const totalEpisodes = season.episode_count;
            const progress = (watchedCount / totalEpisodes) * 100;
            const isCompleted = watchedCount === totalEpisodes;

            return `
                <div class="bg-white/5 hover:bg-white/10 rounded-xl p-4 transition-all border border-white/10">
                    <div class="flex gap-4">
                        <div class="flex-shrink-0">
                            <img src="https://image.tmdb.org/t/p/w200${season.poster_path}" 
                                 alt="Season ${season.season_number}"
                                 class="w-24 h-36 rounded-lg object-cover cursor-pointer hover:ring-2 hover:ring-indigo-500 transition-all"
                                 onclick="window.location.href='/tv/${this.showId}/season/${season.season_number}'"
                                 onerror="this.src='https://via.placeholder.com/200x300?text=No+Poster'">
                        </div>
                        
                        <div class="flex-1">
                            <div class="flex justify-between items-start mb-2">
                                <div>
                                    <h4 class="text-xl font-bold text-white hover:text-indigo-400 cursor-pointer"
                                        onclick="window.location.href='/tv/${this.showId}/season/${season.season_number}'">
                                        ${season.name}
                                    </h4>
                                    <p class="text-sm text-gray-400">
                                        ${totalEpisodes} Episode${totalEpisodes !== 1 ? 's' : ''} 
                                        ${season.air_date ? `• ${new Date(season.air_date).getFullYear()}` : ''}
                                    </p>
                                </div>
                                
                                ${isCompleted ? `
                                    <span class="px-3 py-1 bg-green-500/20 text-green-400 rounded-full text-sm font-semibold flex items-center gap-1">
                                        <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                                            <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>
                                        </svg>
                                        Completed
                                    </span>
                                ` : watchedCount > 0 ? `
                                    <span class="px-3 py-1 bg-indigo-500/20 text-indigo-400 rounded-full text-sm font-semibold">
                                        ${watchedCount}/${totalEpisodes} Watched
                                    </span>
                                ` : ''}
                            </div>
                            
                            ${season.overview ? `
                                <p class="text-gray-300 text-sm mb-3 line-clamp-2">${season.overview}</p>
                            ` : ''}
                            
                            <div class="mb-3">
                                <div class="h-2 bg-gray-700 rounded-full overflow-hidden">
                                    <div class="h-full ${isCompleted ? 'bg-gradient-to-r from-green-500 to-green-600' : 'bg-gradient-to-r from-indigo-500 to-purple-500'} transition-all duration-500"
                                         style="width: ${progress}%"></div>
                                </div>
                                <p class="text-xs text-gray-400 mt-1">${Math.round(progress)}% Complete</p>
                            </div>
                            
                            <div class="flex flex-wrap gap-2">
                                <button onclick="window.location.href='/tv/${this.showId}/season/${season.season_number}'"
                                        class="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm font-semibold transition-all flex items-center gap-2">
                                    <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                                        <path d="M7 3a1 1 0 000 2h6a1 1 0 100-2H7zM4 7a1 1 0 011-1h10a1 1 0 110 2H5a1 1 0 01-1-1zM2 11a2 2 0 012-2h12a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4z"/>
                                    </svg>
                                    View Episodes
                                </button>
                                
                                ${!isCompleted ? `
                                    <button onclick="tvSeasonsManager.markSeasonWatched(${season.season_number})"
                                            class="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded-lg text-sm font-semibold transition-all flex items-center gap-2">
                                        <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                                            <path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd"/>
                                        </svg>
                                        Mark Season Watched
                                    </button>
                                ` : `
                                    <button onclick="tvSeasonsManager.unmarkSeasonWatched(${season.season_number})"
                                            class="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded-lg text-sm font-semibold transition-all flex items-center gap-2">
                                        <svg class="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                                            <path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clip-rule="evenodd"/>
                                        </svg>
                                        Unmark Season
                                    </button>
                                `}
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }).join('');

        console.log('TVSeasonsManager: Seasons rendered');
    }

    getWatchedCountForSeason(seasonNumber) {
        let count = 0;
        for (let ep of this.watchedEpisodes) {
            if (ep.startsWith(`${seasonNumber}-`)) {
                count++;
            }
        }
        return count;
    }

    async markSeasonWatched(seasonNumber) {
        if (!confirm(`Mark all episodes in Season ${seasonNumber} as watched?`)) {
            return;
        }

        try {
            const season = this.seasons.find(s => s.season_number === seasonNumber);
            if (!season) return;

            console.log(`TVSeasonsManager: Marking season ${seasonNumber} as watched (${season.episode_count} episodes)...`);

            // Optimistic UI update
            for (let i = 1; i <= season.episode_count; i++) {
                this.watchedEpisodes.add(`${seasonNumber}-${i}`);
            }
            this.renderSeasonsList();

            // Save to database
            const response = await fetch(`/api/tv/${this.showId}/season/${seasonNumber}/mark-watched`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ watched_date: new Date().toISOString().split('T')[0] })
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();

            if (data.success) {
                console.log('TVSeasonsManager: Mark season API SUCCESS:', data);
                
                const episodeText = data.marked_episodes === 1 ? 'episode' : 'episodes';
                this.showNotification(`✓ Season ${seasonNumber} marked as watched! (${data.marked_episodes} ${episodeText})`, 'success');
                
                // Reload fresh data from database
                console.log('TVSeasonsManager: Reloading data from database after 300ms delay...');
                await new Promise(resolve => setTimeout(resolve, 300));
                
                console.log('TVSeasonsManager: Calling loadWatchedEpisodes()...');
                await this.loadWatchedEpisodes();
                
                console.log('TVSeasonsManager: Calling loadShowProgress()...');
                await this.loadShowProgress();
                
                console.log('TVSeasonsManager: Calling renderSeasonsList()...');
                this.renderSeasonsList();

                // Update tracker
                if (window.tvTracker) {
                    console.log('TVSeasonsManager: Updating tvTracker UI...');
                    window.tvTracker.progress = data.progress;
                    window.tvTracker.renderTrackingUI();
                }
                
                console.log('TVSeasonsManager: Mark season complete!');
            } else {
                throw new Error(data.error || 'Failed to mark season');
            }
        } catch (error) {
            console.error('TVSeasonsManager: Error marking season:', error);
            this.showNotification('Failed to mark season: ' + error.message, 'error');
            
            // Rollback UI
            await this.loadWatchedEpisodes();
            this.renderSeasonsList();
        }
    }

    async unmarkSeasonWatched(seasonNumber) {
        if (!confirm(`Unmark all episodes in Season ${seasonNumber}?`)) {
            return;
        }

        try {
            const season = this.seasons.find(s => s.season_number === seasonNumber);
            if (!season) return;

            console.log(`TVSeasonsManager: Unmarking season ${seasonNumber}...`);

            // Optimistic UI update
            for (let i = 1; i <= season.episode_count; i++) {
                this.watchedEpisodes.delete(`${seasonNumber}-${i}`);
            }
            this.renderSeasonsList();

            // Save to database
            const response = await fetch(`/api/tv/${this.showId}/season/${seasonNumber}/unmark-watched`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();

            if (data.success) {
                this.showNotification(`✓ Season ${seasonNumber} unmarked`, 'success');
                
                // Reload fresh data from database
                console.log('TVSeasonsManager: Reloading data from database...');
                await new Promise(resolve => setTimeout(resolve, 200));
                await this.loadWatchedEpisodes();
                await this.loadShowProgress();
                this.renderSeasonsList();

                // Update tracker
                if (window.tvTracker) {
                    window.tvTracker.progress = data.progress;
                    window.tvTracker.renderTrackingUI();
                }
            } else {
                throw new Error(data.error || 'Failed to unmark season');
            }
        } catch (error) {
            console.error('TVSeasonsManager: Error unmarking season:', error);
            this.showNotification('Failed to unmark season: ' + error.message, 'error');
            
            // Rollback UI
            await this.loadWatchedEpisodes();
            this.renderSeasonsList();
        }
    }

    async markAllWatched() {
        if (!confirm('Mark ALL episodes in ALL seasons as watched?')) {
            return;
        }

        try {
            console.log('TVSeasonsManager: Marking all episodes as watched...');

            const response = await fetch(`/api/tv/${this.showId}/mark-all-watched`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });

            const data = await response.json();

            if (data.success) {
                this.showNotification(`✓ All episodes marked as watched! (${data.marked_episodes} episodes)`, 'success');
                
                // Reload data from database
                await this.loadWatchedEpisodes();
                await this.loadShowProgress();
                this.renderSeasonsList();

                // Update tracker
                if (window.tvTracker) {
                    await window.tvTracker.loadProgress();
                    window.tvTracker.renderTrackingUI();
                }
            } else {
                this.showNotification('Error: ' + (data.error || 'Failed to mark all episodes'), 'error');
            }
        } catch (error) {
            console.error('TVSeasonsManager: Error marking all watched:', error);
            this.showNotification('Failed to mark all episodes as watched', 'error');
        }
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

        setTimeout(() => notification.remove(), 4000);
    }
}

// Initialize when page loads
let tvSeasonsManager;
document.addEventListener('DOMContentLoaded', () => {
    const showIdElement = document.getElementById('show-id');
    if (showIdElement) {
        const showId = showIdElement.value;
        const apiKey = window.TMDB_API_KEY;
        
        console.log('Initializing TV Seasons Manager', { showId, hasApiKey: !!apiKey });
        
        if (apiKey) {
            tvSeasonsManager = new TVSeasonsManager(showId, apiKey);
            window.tvSeasonsManager = tvSeasonsManager;
            tvSeasonsManager.initialize();
            
            // Setup Mark All Watched button
            const markAllBtn = document.getElementById('mark-all-watched-btn');
            if (markAllBtn) {
                markAllBtn.addEventListener('click', () => tvSeasonsManager.markAllWatched());
            }
        } else {
            console.error('TMDB API Key is missing!');
            const container = document.getElementById('seasons-list');
            if (container) {
                container.innerHTML = '<p class="text-red-400 text-center py-4">ERROR: TMDB API Key is missing!</p>';
            }
        }
    }
});
