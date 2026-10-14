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
            }
        } catch (error) {
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

        // F21 — the panel chrome below contains NO untrusted data, so a static
        // template string is safe here. Every friend-supplied value (username,
        // avatar URL, rating, watched date) is appended as a DOM node with
        // textContent instead of being concatenated into this string.
        container.innerHTML = `
            <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                <div class="flex justify-between items-center mb-4">
                    <h3 class="text-xl font-bold text-white">
                        <i class="fas fa-users"></i> Popular with Friends
                    </h3>
                    ${average_rating ? `
                        <div class="flex items-center gap-2">
                            <span class="text-yellow-400">
                                <i class="fas fa-star"></i> <span data-friend-rating></span>
                            </span>
                            <span class="text-gray-400 text-sm">avg. rating</span>
                        </div>
                    ` : ''}
                </div>

                <p class="text-gray-400 mb-4" data-friend-count></p>

                <div class="space-y-3" data-friend-rows></div>
            </div>
        `;

        const ratingSlot = container.querySelector('[data-friend-rating]');
        if (ratingSlot && average_rating) {
            ratingSlot.textContent = String(average_rating);
        }
        const countSlot = container.querySelector('[data-friend-count]');
        if (countSlot) {
            countSlot.textContent =
                `${total_count} ${total_count === 1 ? 'friend has' : 'friends have'} watched this`;
        }

        const rows = container.querySelector('[data-friend-rows]');
        const D = window.FrameIQDOM;
        if (!rows) return;

        friends.forEach(friend => {
            const displayRating = friend.review_rating || friend.diary_rating;
            const username = D.text(friend.username);

            const row = D.el('div', {
                class: 'flex items-start gap-3 p-3 bg-gray-700 rounded-lg hover:bg-gray-650 transition-colors',
            });
            row.appendChild(D.el('img', {
                src: D.safeImageUrl(friend.profile_picture,
                    '/static/images/default-avatar.png', '/static/'),
                alt: username,
                class: 'w-12 h-12 rounded-full object-cover',
            }));

            const body = D.el('div', { class: 'flex-1' });
            const topLine = D.el('div', { class: 'flex items-center gap-2 mb-1' });

            const friendId = D.safeId(friend.id);
            const profileHref = friendId === null ? '' : D.safeLinkUrl(`/profile/${friendId}`);
            const nameNode = D.el('a', {
                class: 'font-semibold text-white hover:text-indigo-400',
                text: username,
            });
            if (profileHref) nameNode.setAttribute('href', profileHref);
            topLine.appendChild(nameNode);

            if (displayRating) {
                const rating = D.el('span', { class: 'text-yellow-400 text-sm' });
                rating.appendChild(D.icon('star'));
                rating.appendChild(document.createTextNode(' ' + D.text(displayRating)));
                topLine.appendChild(rating);
            }
            body.appendChild(topLine);

            if (friend.watched_date) {
                const watched = D.el('p', { class: 'text-gray-400 text-sm mb-1' });
                watched.appendChild(D.icon('check-circle', 'text-green-400'));
                watched.appendChild(document.createTextNode(
                    ' Watched ' + this.formatDate(friend.watched_date)));
                body.appendChild(watched);
            }

            if (friend.has_review) {
                const reviewed = D.el('p', { class: 'text-gray-300 text-sm italic' });
                reviewed.appendChild(D.icon('comment'));
                reviewed.appendChild(document.createTextNode(' Left a review'));
                body.appendChild(reviewed);
            }

            row.appendChild(body);
            rows.appendChild(row);
        });
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
