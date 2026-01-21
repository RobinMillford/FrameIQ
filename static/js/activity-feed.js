/**
 * Activity Feed Component
 * Handles fetching and rendering the social activity feed on the home page
 */
class ActivityFeed {
    constructor(containerId, options = {}) {
        this.container = document.getElementById(containerId);
        this.loadMoreBtn = document.getElementById(options.loadMoreBtnId || 'load-more-feed');
        this.followingBtn = document.getElementById(options.followingBtnId || 'feed-btn-following');
        this.globalBtn = document.getElementById(options.globalBtnId || 'feed-btn-global');
        
        this.currentPage = 1;
        this.currentMode = options.defaultMode || 'global';
        this.isLoading = false;
        this.hasMore = true;
        
        this.init();
    }
    
    init() {
        if (!this.container) return;
        
        // Tab switching
        if (this.followingBtn) {
            this.followingBtn.addEventListener('click', () => this.switchMode('following'));
        }
        if (this.globalBtn) {
            this.globalBtn.addEventListener('click', () => this.switchMode('global'));
        }
        
        // Load more
        if (this.loadMoreBtn) {
            this.loadMoreBtn.addEventListener('click', () => this.loadFeed(true));
        }
        
        // Initial load
        this.loadFeed();
    }
    
    switchMode(mode) {
        if (this.currentMode === mode || this.isLoading) return;
        
        this.currentMode = mode;
        this.currentPage = 1;
        this.hasMore = true;
        
        // Update UI buttons
        if (this.followingBtn && this.globalBtn) {
            if (mode === 'following') {
                this.followingBtn.className = 'px-6 py-2 rounded-md transition-all duration-300 text-sm font-medium bg-indigo-600 text-white shadow-lg';
                this.globalBtn.className = 'px-6 py-2 rounded-md transition-all duration-300 text-sm font-medium text-gray-400 hover:text-white';
            } else {
                this.globalBtn.className = 'px-6 py-2 rounded-md transition-all duration-300 text-sm font-medium bg-indigo-600 text-white shadow-lg';
                this.followingBtn.className = 'px-6 py-2 rounded-md transition-all duration-300 text-sm font-medium text-gray-400 hover:text-white';
            }
        }
        
        // Clear container and show loader
        this.container.innerHTML = `
            <div class="col-span-full flex justify-center py-12">
                <div class="animate-spin rounded-full h-12 w-12 border-t-2 border-b-2 border-indigo-500"></div>
            </div>
        `;
        
        this.loadFeed();
    }
    
    async loadFeed(append = false) {
        if (this.isLoading) return;
        
        this.isLoading = true;
        if (this.loadMoreBtn) this.loadMoreBtn.classList.add('hidden');
        
        const endpoint = this.currentMode === 'following' ? '/api/social/feed' : '/api/social/global-feed';
        const url = `${endpoint}?page=${this.currentPage}&per_page=9`;
        
        try {
            const response = await fetch(url);
            const data = await response.json();
            
            if (response.ok) {
                this.renderFeed(data.reviews, append);
                this.hasMore = data.has_next;
                if (this.hasMore && this.loadMoreBtn) {
                    this.loadMoreBtn.classList.remove('hidden');
                }
                this.currentPage++;
            } else {
                this.showError(data.error || 'Failed to load activities');
            }
        } catch (error) {
            console.error('Error loading activity feed:', error);
            this.showError('Network error. Please try again.');
        } finally {
            this.isLoading = false;
        }
    }
    
    renderFeed(reviews, append = false) {
        if (!append) this.container.innerHTML = '';
        
        if (reviews.length === 0) {
            if (!append) {
                this.container.innerHTML = `
                    <div class="col-span-full text-center py-12 glass-effect rounded-xl">
                        <i class="fas fa-stream text-4xl text-gray-600 mb-4"></i>
                        <p class="text-gray-400">${this.currentMode === 'following' ? 'Connect with others to see their reviews here!' : 'No activity found.'}</p>
                    </div>
                `;
            }
            return;
        }
        
        reviews.forEach(review => {
            const card = this.createFeedCard(review);
            this.container.appendChild(card);
        });
        
        // Initialize follow buttons if they exist
        if (typeof FollowButton !== 'undefined') {
            const newButtons = this.container.querySelectorAll('[data-follow-button]');
            newButtons.forEach(btn => {
                if (!btn.dataset.initialized) {
                    new FollowButton(parseInt(btn.dataset.userId), btn);
                    btn.dataset.initialized = "true";
                }
            });
        }
    }
    
    createFeedCard(review) {
        const movieUrl = review.media_type === 'movie' ? `/movie/${review.media.id}` : `/tv/${review.media.id}`;
        const posterUrl = review.media.poster_path ? (review.media.poster_path.startsWith('http') ? review.media.poster_path : `https://image.tmdb.org/t/p/w200${review.media.poster_path}`) : 'https://via.placeholder.com/200x300?text=No+Poster';
        const profilePic = review.user.profile_picture ? (review.user.profile_picture.startsWith('http') ? review.user.profile_picture : `/static/${review.user.profile_picture}`) : `https://via.placeholder.com/100x100?text=${review.user.username[0].toUpperCase()}`;
        
        const card = document.createElement('div');
        card.className = 'glass-effect rounded-xl p-5 flex flex-col h-full animate__animated animate__fadeIn';
        
        card.innerHTML = `
            <div class="flex items-center gap-3 mb-4">
                <a href="/user/${review.user.id}" class="relative flex-shrink-0">
                    <img src="${profilePic}" alt="${review.user.username}" class="w-10 h-10 rounded-full border-2 border-indigo-500 object-cover" onerror="this.src='https://via.placeholder.com/100x100?text=User'">
                </a>
                <div class="flex-grow min-w-0">
                    <div class="flex items-center gap-2">
                        <a href="/user/${review.user.id}" class="font-bold text-sm truncate hover:text-indigo-400 transition-colors">
                            ${review.user.username}
                        </a>
                        ${!review.is_author_self ? `
                            <button class="text-[10px] bg-indigo-600/30 hover:bg-indigo-600 text-indigo-300 hover:text-white px-2 py-0.5 rounded transition-all" 
                                    data-follow-button 
                                    data-user-id="${review.user.id}" 
                                    data-following="false"
                                    data-authenticated="true">
                                Follow
                            </button>
                        ` : ''}
                    </div>
                    <p class="text-[10px] text-gray-500">${this.formatRelativeTime(review.created_at)}</p>
                </div>
            </div>
            
            <div class="flex gap-4 flex-grow">
                <a href="${movieUrl}" class="flex-shrink-0">
                    <img src="${posterUrl}" alt="${review.media.title}" class="w-20 h-28 rounded shadow-lg object-cover border border-gray-700 hover:scale-105 transition-transform duration-300" onerror="this.src='https://via.placeholder.com/200x300?text=No+Poster'">
                </a>
                <div class="flex-grow min-w-0">
                    <h4 class="font-bold text-sm mb-1 leading-tight">
                        <a href="${movieUrl}" class="hover:text-indigo-400 transition-colors">${review.media.title}</a>
                    </h4>
                    <div class="flex text-yellow-500 text-[10px] mb-2">
                        ${'★'.repeat(Math.round(review.rating))}${'☆'.repeat(5 - Math.round(review.rating))}
                    </div>
                    <p class="text-gray-300 text-xs line-clamp-3 italic">
                        "${review.content || 'No written review.'}"
                    </p>
                </div>
            </div>
            
            <div class="mt-4 pt-4 border-t border-gray-700/50 flex justify-between items-center text-xs">
                <div class="flex items-center gap-3 text-gray-400">
                    <span><i class="far fa-heart mr-1"></i> ${review.likes_count}</span>
                    <span><i class="far fa-comment mr-1"></i> ${review.comments_count}</span>
                </div>
                <a href="${movieUrl}" class="text-indigo-400 hover:text-indigo-300 font-medium tracking-wide">
                    Detail <i class="fas fa-chevron-right ml-1 text-[10px]"></i>
                </a>
            </div>
        `;
        
        return card;
    }
    
    formatRelativeTime(isoString) {
        const date = new Date(isoString);
        const now = new Date();
        const diffInSeconds = Math.floor((now - date) / 1000);
        
        if (diffInSeconds < 60) return 'just now';
        
        const diffInMinutes = Math.floor(diffInSeconds / 60);
        if (diffInMinutes < 60) return `${diffInMinutes}m ago`;
        
        const diffInHours = Math.floor(diffInMinutes / 60);
        if (diffInHours < 24) return `${diffInHours}h ago`;
        
        const diffInDays = Math.floor(diffInHours / 24);
        if (diffInDays < 7) return `${diffInDays}d ago`;
        
        return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }
    
    showError(message) {
        this.container.innerHTML = `
            <div class="col-span-full text-center py-12 text-red-400">
                <i class="fas fa-exclamation-circle text-4xl mb-4"></i>
                <p>${message}</p>
                <button onclick="window.location.reload()" class="mt-4 text-sm underline">Retry</button>
            </div>
        `;
    }
}

// Initialize on the main page
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('activity-feed-container')) {
        const isUserAuthenticated = document.getElementById('feed-btn-following') !== null;
        window.activityFeed = new ActivityFeed('activity-feed-container', {
            defaultMode: isUserAuthenticated ? 'following' : 'global'
        });
    }
});
