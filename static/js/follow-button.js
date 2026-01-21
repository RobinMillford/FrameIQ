/**
 * Follow Button Component
 * Reusable component for follow/unfollow functionality
 */

class FollowButton {
    constructor(userId, buttonElement) {
        this.userId = userId;
        this.button = buttonElement;
        this.isFollowing = buttonElement.dataset.following === 'true';
        this.isLoading = false;
        
        this.init();
    }
    
    init() {
        this.button.addEventListener('click', () => this.handleClick());
        this.updateButtonState();
    }
    
    async handleClick() {
        if (this.isLoading) return;
        
        // Check if user is logged in
        if (!this.button.dataset.authenticated || this.button.dataset.authenticated === 'false') {
            alert('Please login to follow users');
            window.location.href = '/login';
            return;
        }
        
        this.setLoading(true);
        
        try {
            const response = await fetch(`/api/users/${this.userId}/follow`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.isFollowing = data.is_following;
                this.updateButtonState();
                
                // Update follower count if element exists
                const followerCountEl = document.getElementById(`follower-count-${this.userId}`);
                if (followerCountEl && data.followers_count !== undefined) {
                    followerCountEl.textContent = data.followers_count;
                }
                
                // Dispatch custom event for other components to listen to
                const event = new CustomEvent('followStatusChanged', {
                    detail: {
                        userId: this.userId,
                        isFollowing: this.isFollowing,
                        followersCount: data.followers_count
                    }
                });
                document.dispatchEvent(event);
                
                // Show success message
                this.showToast(data.message, 'success');
            } else {
                this.showToast(data.error || 'Failed to update follow status', 'error');
            }
        } catch (error) {
            console.error('Error toggling follow:', error);
            this.showToast('Network error. Please try again.', 'error');
        } finally {
            this.setLoading(false);
        }
    }
    
    setLoading(loading) {
        this.isLoading = loading;
        this.button.disabled = loading;
        
        if (loading) {
            this.button.dataset.originalText = this.button.innerHTML;
            this.button.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i> Loading...';
        } else {
            this.updateButtonState();
        }
    }
    
    updateButtonState() {
        if (this.isFollowing) {
            this.button.innerHTML = '<i class="fas fa-user-check mr-1"></i> Following';
            this.button.classList.remove('bg-indigo-600', 'hover:bg-indigo-700');
            this.button.classList.add('bg-gray-600', 'hover:bg-gray-700');
        } else {
            this.button.innerHTML = '<i class="fas fa-user-plus mr-1"></i> Follow';
            this.button.classList.remove('bg-gray-600', 'hover:bg-gray-700');
            this.button.classList.add('bg-indigo-600', 'hover:bg-indigo-700');
        }
        
        this.button.dataset.following = this.isFollowing;
    }
    
    showToast(message, type = 'info') {
        // Create toast notification
        const toast = document.createElement('div');
        toast.className = `fixed bottom-4 right-4 px-6 py-3 rounded-lg shadow-lg text-white z-50 transition-opacity duration-300 ${
            type === 'success' ? 'bg-green-600' : 
            type === 'error' ? 'bg-red-600' : 
            'bg-blue-600'
        }`;
        toast.textContent = message;
        
        document.body.appendChild(toast);
        
        // Fade out and remove after 3 seconds
        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
}

// Initialize all follow buttons on page load
document.addEventListener('DOMContentLoaded', () => {
    const followButtons = document.querySelectorAll('[data-follow-button]');
    followButtons.forEach(button => {
        const userId = parseInt(button.dataset.userId);
        if (userId) {
            new FollowButton(userId, button);
        }
    });
});

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = FollowButton;
}
