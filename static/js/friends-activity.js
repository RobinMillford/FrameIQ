/**
 * Friends Activity Manager
 * Displays what friends have done with a specific media item
 */

class FriendsActivityManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.container = null;
    }

    async init(containerId) {
        this.container = document.getElementById(containerId);
        if (!this.container) {
            console.error('Friends activity container not found:', containerId);
            return;
        }

        await this.loadActivity();
    }

    async loadActivity() {
        try {
            const response = await fetch(
                `/api/media/${this.mediaId}/${this.mediaType}/friends-activity`
            );
            const data = await response.json();

            if (data.success) {
                this.renderActivity(data);
            } else {
                this.container.innerHTML = '<p>Unable to load friends activity</p>';
            }
        } catch (error) {
            console.error('Error loading friends activity:', error);
            this.container.innerHTML = '<p>Error loading friends activity</p>';
        }
    }

    renderActivity(data) {
        if (!data.has_friends) {
            this.container.innerHTML = `
                <div class="no-friends-activity">
                    <p>Follow other users to see what your friends think!</p>
                    <a href="/discover-users" class="btn-discover">Discover Users</a>
                </div>
            `;
            return;
        }

        const activity = data.activity;
        const hasAnyActivity = 
            activity.reviews.length > 0 ||
            activity.likes.length > 0 ||
            activity.comments.length > 0 ||
            activity.tags.length > 0 ||
            activity.watchlisted.length > 0 ||
            activity.watched.length > 0;

        if (!hasAnyActivity) {
            this.container.innerHTML = `
                <div class="no-friends-activity">
                    <p>None of your friends have interacted with this yet.</p>
                    <p class="hint">Be the first to review it!</p>
                </div>
            `;
            return;
        }

        let html = `
            <div class="friends-activity-header">
                <h3>👥 Friends Activity</h3>
                <p class="activity-summary">${data.engaging_friends_count} of your ${data.friends_count} friends have interacted with this</p>
            </div>
        `;

        // Reviews
        if (activity.reviews.length > 0) {
            html += '<div class="activity-section">';
            html += '<h4>📝 Reviews</h4>';
            activity.reviews.forEach(review => {
                const stars = review.rating ? '★'.repeat(review.rating) + '☆'.repeat(10 - review.rating) : '';
                html += `
                    <div class="friend-activity-item">
                        <div class="friend-info">
                            <img src="${review.user_avatar || '/static/images/default-avatar.png'}" 
                                 alt="${review.username}" 
                                 class="friend-avatar">
                            <div>
                                <a href="/profile/${review.user_id}" class="friend-name">${review.username}</a>
                                ${stars ? `<div class="friend-rating">${stars}</div>` : ''}
                            </div>
                        </div>
                        <p class="friend-review">${review.content}</p>
                        <span class="activity-time">${this.formatTime(review.created_at)}</span>
                    </div>
                `;
            });
            html += '</div>';
        }

        // Watched
        if (activity.watched.length > 0) {
            html += '<div class="activity-section">';
            html += '<h4>✓ Watched</h4>';
            html += '<div class="friend-avatars">';
            activity.watched.forEach(user => {
                html += `
                    <div class="friend-avatar-item" title="${user.username}${user.rating ? ' - ' + user.rating + '/10' : ''}">
                        <img src="${user.user_avatar || '/static/images/default-avatar.png'}" 
                             alt="${user.username}">
                        <span>${user.username}</span>
                    </div>
                `;
            });
            html += '</div></div>';
        }

        // Watchlisted
        if (activity.watchlisted.length > 0) {
            html += '<div class="activity-section">';
            html += '<h4>📋 On Watchlist</h4>';
            html += '<div class="friend-avatars">';
            activity.watchlisted.forEach(user => {
                html += `
                    <div class="friend-avatar-item" title="${user.username}">
                        <img src="${user.user_avatar || '/static/images/default-avatar.png'}" 
                             alt="${user.username}">
                        <span>${user.username}</span>
                    </div>
                `;
            });
            html += '</div></div>';
        }

        // Likes
        if (activity.likes.length > 0) {
            html += '<div class="activity-section">';
            html += '<h4>❤️ Likes</h4>';
            html += '<div class="friend-avatars">';
            activity.likes.forEach(user => {
                html += `
                    <div class="friend-avatar-item" title="${user.username}">
                        <img src="${user.user_avatar || '/static/images/default-avatar.png'}" 
                             alt="${user.username}">
                        <span>${user.username}</span>
                    </div>
                `;
            });
            html += '</div></div>';
        }

        // Tags
        if (activity.tags.length > 0) {
            html += '<div class="activity-section">';
            html += '<h4>🏷️ Tags</h4>';
            activity.tags.forEach(userTags => {
                html += `
                    <div class="friend-tags">
                        <a href="/profile/${userTags.user_id}" class="friend-name">${userTags.username}</a>:
                        ${userTags.tags.map(tag => `<span class="friend-tag">${tag}</span>`).join(' ')}
                    </div>
                `;
            });
            html += '</div>';
        }

        this.container.innerHTML = html;
    }

    formatTime(timestamp) {
        const date = new Date(timestamp);
        const now = new Date();
        const seconds = Math.floor((now - date) / 1000);

        if (seconds < 60) return 'Just now';
        if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
        if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
        if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
        return date.toLocaleDateString();
    }
}

// Auto-initialize if data attributes are present
document.addEventListener('DOMContentLoaded', () => {
    const container = document.querySelector('[data-friends-activity]');
    if (container) {
        const mediaId = container.dataset.mediaId;
        const mediaType = container.dataset.mediaType;
        const containerId = container.id;

        if (mediaId && mediaType) {
            const manager = new FriendsActivityManager(mediaId, mediaType);
            manager.init(containerId);
        }
    }
});
