// File: static/js/media-comments.js
// Film page comments functionality

class MediaCommentsManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.comments = [];
        this.init();
    }

    async init() {
        await this.loadComments();
        this.renderComments();
        this.setupEventListeners();
    }

    async loadComments() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/comments?media_type=${this.mediaType}`);
            if (response.ok) {
                const data = await response.json();
                this.comments = data.comments || [];
            }
        } catch (error) {
            console.error('Error loading comments:', error);
        }
    }

    renderComments() {
        const container = document.getElementById('media-comments-list');
        if (!container) return;

        if (this.comments.length === 0) {
            container.innerHTML = `
                <p class="text-gray-400 text-center py-8">No comments yet. Be the first to share your thoughts!</p>
            `;
            return;
        }

        container.innerHTML = this.comments.map(comment => `
            <div class="comment-item bg-gray-800 rounded-lg p-4 mb-3" data-comment-id="${comment.id}">
                <div class="flex items-start gap-3">
                    <div class="flex-shrink-0">
                        <div class="w-10 h-10 rounded-full bg-indigo-600 flex items-center justify-center text-white font-bold">
                            ${comment.user.username.charAt(0).toUpperCase()}
                        </div>
                    </div>
                    <div class="flex-1">
                        <div class="flex items-center gap-2 mb-1">
                            <a href="/profile/${comment.user.username}" class="font-semibold text-indigo-400 hover:text-indigo-300">
                                ${this.escapeHtml(comment.user.username)}
                            </a>
                            <span class="text-gray-500 text-sm">${this.formatDate(comment.created_at)}</span>
                            ${comment.updated_at && comment.updated_at !== comment.created_at ? 
                                '<span class="text-gray-500 text-xs">(edited)</span>' : ''}
                        </div>
                        <p class="text-gray-300 whitespace-pre-wrap">${this.escapeHtml(comment.content)}</p>
                        ${comment.is_author ? `
                            <div class="flex gap-2 mt-2">
                                <button onclick="commentsManager.editComment(${comment.id})" class="text-sm text-indigo-400 hover:text-indigo-300">
                                    Edit
                                </button>
                                <button onclick="commentsManager.deleteComment(${comment.id})" class="text-sm text-red-400 hover:text-red-300">
                                    Delete
                                </button>
                            </div>
                        ` : ''}
                    </div>
                </div>
            </div>
        `).join('');
    }

    setupEventListeners() {
        const form = document.getElementById('comment-form');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                this.submitComment();
            });
        }
    }

    async submitComment() {
        const textarea = document.getElementById('comment-input');
        if (!textarea) return;

        const content = textarea.value.trim();
        if (!content) {
            this.showMessage('Comment cannot be empty', 'error');
            return;
        }

        try {
            const response = await fetch(`/api/media/${this.mediaId}/comments`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    content: content,
                    media_type: this.mediaType
                })
            });

            if (response.ok) {
                const data = await response.json();
                this.comments.push(data.comment);
                this.renderComments();
                textarea.value = '';
                this.showMessage('Comment added!', 'success');
            } else {
                const error = await response.json();
                this.showMessage(error.error || 'Failed to add comment', 'error');
            }
        } catch (error) {
            console.error('Error adding comment:', error);
            this.showMessage('An error occurred', 'error');
        }
    }

    async editComment(commentId) {
        const comment = this.comments.find(c => c.id === commentId);
        if (!comment) return;

        const newContent = prompt('Edit your comment:', comment.content);
        if (!newContent || newContent === comment.content) return;

        try {
            const response = await fetch(`/api/media/comments/${commentId}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    content: newContent
                })
            });

            if (response.ok) {
                await this.loadComments();
                this.renderComments();
                this.showMessage('Comment updated!', 'success');
            } else {
                const error = await response.json();
                this.showMessage(error.error || 'Failed to update comment', 'error');
            }
        } catch (error) {
            console.error('Error updating comment:', error);
            this.showMessage('An error occurred', 'error');
        }
    }

    async deleteComment(commentId) {
        if (!confirm('Are you sure you want to delete this comment?')) return;

        try {
            const response = await fetch(`/api/media/comments/${commentId}`, {
                method: 'DELETE'
            });

            if (response.ok) {
                this.comments = this.comments.filter(c => c.id !== commentId);
                this.renderComments();
                this.showMessage('Comment deleted!', 'success');
            } else {
                const error = await response.json();
                this.showMessage(error.error || 'Failed to delete comment', 'error');
            }
        } catch (error) {
            console.error('Error deleting comment:', error);
            this.showMessage('An error occurred', 'error');
        }
    }

    formatDate(dateString) {
        const date = new Date(dateString);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins}m ago`;
        if (diffHours < 24) return `${diffHours}h ago`;
        if (diffDays < 7) return `${diffDays}d ago`;
        
        return date.toLocaleDateString();
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    showMessage(message, type) {
        const messageDiv = document.getElementById('comments-message');
        if (messageDiv) {
            messageDiv.textContent = message;
            messageDiv.className = type === 'success' ? 'text-green-400 text-sm mt-2' : 'text-red-400 text-sm mt-2';
            messageDiv.classList.remove('hidden');
            setTimeout(() => messageDiv.classList.add('hidden'), 3000);
        }
    }
}

// Initialize on page load
let commentsManager;
document.addEventListener('DOMContentLoaded', () => {
    const mediaId = document.body.dataset.mediaId;
    const mediaType = document.body.dataset.mediaType;
    
    if (mediaId && mediaType) {
        commentsManager = new MediaCommentsManager(mediaId, mediaType);
    }
});
