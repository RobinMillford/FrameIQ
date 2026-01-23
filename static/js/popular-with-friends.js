/**
 * Popular with Friends Manager
 * Displays which friends have watched/reviewed a movie/TV show
 */

class PopularWithFriendsManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.friendsData = null;
    }

    async load() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/popular-with-friends`);
            const data = await response.json();
            
            if (data.success) {
                this.friendsData = data;
                this.render();
            } else {
                console.error('Failed to load popular with friends:', data.error);
            }
        } catch (error) {
            console.error('Error loading popular with friends:', error);
        }
    }

    render() {
        const container = document.getElementById('popular-with-friends');
        if (!container) return;

        const { friends, total_count, average_rating } = this.friendsData;

        if (total_count === 0) {
            container.innerHTML = `
                <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                    <h3 class="text-xl font-bold text-white mb-4">
                        <i class="fas fa-users"></i> Popular with Friends
                    </h3>
                    <p class="text-gray-400 text-center py-4">
                        None of your friends have watched this yet. Be the first!
                    </p>
                </div>
            `;
            return;
        }

        let html = `
            <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-bold text-white">
                        <i class="fas fa-users"></i> Popular with Friends
                    </h3>
                    ${average_rating ? `
                        <div class="flex items-center gap-2">
                            <span class="text-yellow-400">
                                <i class="fas fa-star"></i> ${average_rating}
                            </span>
                            <span class="text-gray-400 text-sm">avg. rating</span>
                        </div>
                    ` : ''}
                </div>
                
                <p class="text-gray-400 mb-4">
                    ${total_count} ${total_count === 1 ? 'friend has' : 'friends have'} watched this
                </p>
                
                <div class="space-y-3">
        `;

        friends.forEach(friend => {
            const displayRating = friend.review_rating || friend.diary_rating;
            html += `
                <div class="flex items-start gap-3 p-3 bg-gray-700 rounded-lg hover:bg-gray-650 transition-colors">
                    <img src="${friend.profile_picture || '/static/images/default-avatar.png'}" 
                         alt="${friend.username}"
                         class="w-12 h-12 rounded-full object-cover">
                    
                    <div class="flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <a href="/profile/${friend.id}" class="font-semibold text-white hover:text-indigo-400">
                                ${friend.username}
                            </a>
                            ${displayRating ? `
                                <span class="text-yellow-400 text-sm">
                                    <i class="fas fa-star"></i> ${displayRating}
                                </span>
                            ` : ''}
                        </div>
                        
                        ${friend.watched_date ? `
                            <p class="text-gray-400 text-sm mb-1">
                                <i class="fas fa-check-circle text-green-400"></i> 
                                Watched ${this.formatDate(friend.watched_date)}
                            </p>
                        ` : ''}
                        
                        ${friend.has_review ? `
                            <p class="text-gray-300 text-sm italic">
                                <i class="fas fa-comment"></i> Left a review
                            </p>
                        ` : ''}
                    </div>
                </div>
            `;
        });

        html += `
                </div>
            </div>
        `;

        container.innerHTML = html;
    }

    formatDate(dateString) {
        const date = new Date(dateString);
        const now = new Date();
        const diffTime = Math.abs(now - date);
        const diffDays = Math.floor(diffTime / (1000 * 60 * 60 * 24));

        if (diffDays === 0) return 'today';
        if (diffDays === 1) return 'yesterday';
        if (diffDays < 7) return `${diffDays} days ago`;
        if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
        if (diffDays < 365) return `${Math.floor(diffDays / 30)} months ago`;
        return date.toLocaleDateString();
    }
}

// Auto-initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('popular-with-friends');
    if (container) {
        const mediaId = container.dataset.mediaId;
        const mediaType = container.dataset.mediaType;
        
        if (mediaId && mediaType) {
            const manager = new PopularWithFriendsManager(mediaId, mediaType);
            manager.load();
        }
    }
});
