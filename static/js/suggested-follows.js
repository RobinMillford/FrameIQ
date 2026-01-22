/**
 * Suggested Follows Component
 * Fetches and renders taste-based user recommendations
 */

class SuggestedFollows {
    constructor(containerId) {
        this.container = document.getElementById(containerId);
        this.isLoading = false;
        
        if (this.container) {
            this.init();
        }
    }
    
    async init() {
        await this.fetchSuggestions();
    }
    
    async fetchSuggestions() {
        this.setLoading(true);
        try {
            const response = await fetch('/api/social/suggested-follows?limit=4');
            const data = await response.json();
            
            if (response.ok) {
                this.render(data.suggestions);
            } else {
                console.error('Failed to fetch suggested follows:', data.error);
                this.container.innerHTML = '<p class="text-gray-500 italic">Could not load suggestions.</p>';
            }
        } catch (error) {
            console.error('Error fetching suggested follows:', error);
            this.container.innerHTML = '<p class="text-gray-500 italic">Network error.</p>';
        } finally {
            this.setLoading(false);
        }
    }
    
    setLoading(loading) {
        this.isLoading = loading;
        if (loading) {
            this.container.innerHTML = `
                <div class="col-span-full flex justify-center py-8">
                    <div class="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2 border-indigo-500"></div>
                </div>
            `;
        }
    }
    
    render(suggestions) {
        if (!suggestions || suggestions.length === 0) {
            this.container.innerHTML = '<p class="text-gray-400 italic py-4">Join communities to see suggestions here!</p>';
            return;
        }
        
        this.container.innerHTML = suggestions.map(user => `
            <div class="glass-effect rounded-xl p-5 flex flex-col items-center text-center animate__animated animate__fadeIn">
                <a href="/user/${user.id}" class="group mb-3">
                    <div class="w-20 h-20 rounded-full overflow-hidden border-2 border-indigo-500 group-hover:border-indigo-400 transition-colors mb-2">
                        <img src="${user.profile_picture || '/static/images/default_avatar.png'}" 
                             alt="${user.username}" 
                             class="w-full h-full object-cover">
                    </div>
                    <h3 class="font-bold text-white group-hover:text-indigo-400 transition-colors truncate w-32">@${user.username}</h3>
                </a>
                
                <p class="text-xs text-indigo-300 mb-4 bg-indigo-900/30 px-2 py-1 rounded-full">${user.reason}</p>
                
                <div class="flex items-center space-x-3 mb-5 text-xs text-gray-400">
                    <div class="flex flex-col">
                        <span class="text-white font-semibold">${user.total_reviews}</span>
                        <span>Reviews</span>
                    </div>
                    <div class="w-px h-6 bg-gray-700"></div>
                    <div class="flex flex-col">
                        <span class="text-white font-semibold" id="follower-count-${user.id}">${user.followers_count}</span>
                        <span>Followers</span>
                    </div>
                </div>

                <button 
                    data-follow-button 
                    data-user-id="${user.id}"
                    data-authenticated="true"
                    data-following="false"
                    class="w-full py-1.5 px-3 rounded-lg text-sm font-semibold transition-all duration-300 bg-indigo-600 hover:bg-indigo-700 text-white flex items-center justify-center">
                    <i class="fas fa-user-plus mr-1.5"></i> Follow
                </button>
            </div>
        `).join('');
        
        // Re-initialize follow buttons for the new elements
        if (window.FollowButton) {
            const followButtons = this.container.querySelectorAll('[data-follow-button]');
            followButtons.forEach(button => {
                const userId = parseInt(button.dataset.userId);
                if (userId) {
                    new FollowButton(userId, button);
                }
            });
        }
    }
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    new SuggestedFollows('suggested-follows-container');
});
