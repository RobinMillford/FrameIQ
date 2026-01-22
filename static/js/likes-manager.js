// File: static/js/likes-manager.js
// Heart/Like functionality for movies and TV shows

class LikesManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.liked = false;
        this.likesCount = 0;
        this.init();
    }

    async init() {
        await this.checkIfLiked();
        await this.loadLikesCount();
        this.renderButton();
        this.setupEventListeners();
    }

    async checkIfLiked() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/likes/check?media_type=${this.mediaType}`);
            if (response.ok) {
                const data = await response.json();
                this.liked = data.liked;
            }
        } catch (error) {
            console.error('Error checking like status:', error);
        }
    }

    async loadLikesCount() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/likes?media_type=${this.mediaType}`);
            if (response.ok) {
                const data = await response.json();
                this.likesCount = data.count;
            }
        } catch (error) {
            console.error('Error loading likes count:', error);
        }
    }

    renderButton() {
        const container = document.getElementById('like-button-container');
        if (!container) return;

        const heartIcon = this.liked ? '❤️' : '🤍';
        const buttonClass = this.liked 
            ? 'glass-effect text-red-400 px-4 py-2 rounded-lg font-semibold flex items-center gap-2 hover:bg-red-600 transition-colors'
            : 'glass-effect text-white px-4 py-2 rounded-lg font-semibold flex items-center gap-2 hover:bg-gray-700 transition-colors';

        container.innerHTML = `
            <button id="like-button" class="${buttonClass}">
                <span class="text-xl">${heartIcon}</span>
                <span>${this.liked ? 'Liked' : 'Like'}</span>
                ${this.likesCount > 0 ? `<span class="text-sm opacity-75">(${this.likesCount})</span>` : ''}
            </button>
        `;
    }

    setupEventListeners() {
        const button = document.getElementById('like-button');
        if (button) {
            button.addEventListener('click', () => this.toggleLike());
        }
    }

    async toggleLike() {
        if (this.liked) {
            await this.unlikeMedia();
        } else {
            await this.likeMedia();
        }
    }

    async likeMedia() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/like`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    media_type: this.mediaType
                })
            });

            if (response.ok) {
                this.liked = true;
                this.likesCount++;
                this.renderButton();
                this.setupEventListeners();
                this.showMessage('Liked! ❤️', 'success');
                this.animateHeart();
            } else {
                const error = await response.json();
                this.showMessage(error.error || 'Failed to like', 'error');
            }
        } catch (error) {
            console.error('Error liking media:', error);
            this.showMessage('An error occurred', 'error');
        }
    }

    async unlikeMedia() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/like?media_type=${this.mediaType}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                this.liked = false;
                this.likesCount--;
                this.renderButton();
                this.setupEventListeners();
                this.showMessage('Unliked', 'success');
            } else {
                const error = await response.json();
                this.showMessage(error.error || 'Failed to unlike', 'error');
            }
        } catch (error) {
            console.error('Error unliking media:', error);
            this.showMessage('An error occurred', 'error');
        }
    }

    animateHeart() {
        const button = document.getElementById('like-button');
        if (button) {
            button.classList.add('animate__animated', 'animate__pulse');
            setTimeout(() => {
                button.classList.remove('animate__animated', 'animate__pulse');
            }, 1000);
        }
    }

    showMessage(message, type) {
        const messageDiv = document.getElementById('like-message');
        if (messageDiv) {
            messageDiv.textContent = message;
            messageDiv.className = type === 'success' ? 'text-green-400 text-sm mt-2' : 'text-red-400 text-sm mt-2';
            messageDiv.classList.remove('hidden');
            setTimeout(() => messageDiv.classList.add('hidden'), 3000);
        }
    }
}

// Initialize on page load
let likesManager;
document.addEventListener('DOMContentLoaded', () => {
    const mediaId = document.body.dataset.mediaId;
    const mediaType = document.body.dataset.mediaType;
    
    if (mediaId && mediaType) {
        likesManager = new LikesManager(mediaId, mediaType);
    }
});
