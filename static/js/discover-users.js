/**
 * User Discovery Manager
 * Handles user search, suggestions, and popular users
 */

class UserDiscoveryManager {
    constructor() {
        this.searchInput = document.getElementById('user-search-input');
        this.searchResults = document.getElementById('search-results');
        this.suggestedContainer = document.getElementById('suggested-users');
        this.popularContainer = document.getElementById('popular-users');
        this.popularPagination = document.getElementById('popular-pagination');
        
        this.searchTimeout = null;
        this.currentTimeframe = 'all';
        this.currentPage = 1;
        
        this.init();
    }
    
    init() {
        // Search input handler
        if (this.searchInput) {
            this.searchInput.addEventListener('input', (e) => {
                clearTimeout(this.searchTimeout);
                const query = e.target.value.trim();
                
                if (query.length === 0) {
                    this.searchResults.style.display = 'none';
                    return;
                }
                
                if (query.length < 2) return;
                
                this.searchTimeout = setTimeout(() => {
                    this.searchUsers(query);
                }, 300);
            });
        }
        
        // Timeframe selector
        document.querySelectorAll('.timeframe-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                document.querySelectorAll('.timeframe-btn').forEach(b => b.classList.remove('active'));
                e.target.classList.add('active');
                this.currentTimeframe = e.target.dataset.timeframe;
                this.currentPage = 1;
                this.loadPopularUsers();
            });
        });
        
        // Load initial data
        if (this.suggestedContainer) {
            this.loadSuggestedUsers();
        }
        this.loadPopularUsers();
    }
    
    async searchUsers(query) {
        try {
            const response = await fetch(`/api/users/search?q=${encodeURIComponent(query)}`);
            const data = await response.json();
            
            this.searchResults.innerHTML = '';
            this.searchResults.style.display = 'grid';
            
            if (data.users.length === 0) {
                const empty = this.createEmptyState('No users found');
                this.searchResults.appendChild(empty);
                return;
            }
            
            data.users.forEach(user => {
                const card = this.createUserCard(user);
                this.searchResults.appendChild(card);
            });
            
        } catch (error) {
            console.error('Search error:', error);
            this.showToast('Failed to search users', 'error');
        }
    }
    
    async loadSuggestedUsers() {
        try {
            const response = await fetch('/api/users/suggested?limit=6');
            const data = await response.json();
            
            this.suggestedContainer.innerHTML = '';
            this.suggestedContainer.classList.remove('loading');
            
            if (data.users.length === 0) {
                const empty = this.createEmptyState('No suggestions available yet');
                this.suggestedContainer.appendChild(empty);
                return;
            }
            
            data.users.forEach(user => {
                const card = this.createUserCard(user, true);
                this.suggestedContainer.appendChild(card);
            });
            
        } catch (error) {
            console.error('Suggestions error:', error);
            this.suggestedContainer.innerHTML = '<p class="error-message">Failed to load suggestions</p>';
        }
    }
    
    async loadPopularUsers(page = 1) {
        this.popularContainer.classList.add('loading');
        
        try {
            const response = await fetch(
                `/api/users/popular?page=${page}&per_page=12&timeframe=${this.currentTimeframe}`
            );
            const data = await response.json();
            
            this.popularContainer.innerHTML = '';
            this.popularContainer.classList.remove('loading');
            
            if (data.users.length === 0) {
                const empty = this.createEmptyState('No users found');
                this.popularContainer.appendChild(empty);
                return;
            }
            
            data.users.forEach(user => {
                const card = this.createUserCard(user);
                this.popularContainer.appendChild(card);
            });
            
            // Update pagination
            this.renderPagination(data);
            
        } catch (error) {
            console.error('Popular users error:', error);
            this.popularContainer.innerHTML = '<p class="error-message">Failed to load users</p>';
        }
    }
    
    createUserCard(user, showReason = false) {
        const template = document.getElementById('user-card-template');
        const card = template.content.cloneNode(true);
        
        const cardDiv = card.querySelector('.user-card');
        cardDiv.dataset.userId = user.id;
        
        // Avatar
        const avatar = card.querySelector('.user-avatar');
        avatar.src = user.profile_picture || '/static/images/default-avatar.png';
        avatar.alt = user.username;
        
        // Name and username
        const nameLink = card.querySelector('.user-name');
        nameLink.href = `/profile/${user.username}`;
        nameLink.textContent = user.first_name && user.last_name 
            ? `${user.first_name} ${user.last_name}` 
            : user.username;
        
        card.querySelector('.user-username').textContent = `@${user.username}`;
        
        // Bio
        const bioDiv = card.querySelector('.user-bio');
        if (user.bio) {
            bioDiv.textContent = user.bio;
        } else {
            bioDiv.style.display = 'none';
        }
        
        // Stats
        card.querySelector('.followers-count').textContent = user.followers_count || 0;
        card.querySelector('.reviews-count').textContent = user.total_reviews || 0;
        
        // Reason (for suggestions)
        const reasonDiv = card.querySelector('.user-reason');
        if (showReason && user.reason) {
            reasonDiv.textContent = user.reason;
            reasonDiv.style.display = 'block';
        } else {
            reasonDiv.remove();
        }
        
        // Follow button
        const followBtn = card.querySelector('.follow-btn');
        followBtn.dataset.userId = user.id;
        
        if (user.is_following) {
            followBtn.classList.add('following');
        }
        
        followBtn.addEventListener('click', () => this.toggleFollow(user.id, followBtn));
        
        return card;
    }
    
    async toggleFollow(userId, button) {
        const isFollowing = button.classList.contains('following');
        const url = isFollowing ? `/api/users/${userId}/unfollow` : `/api/users/${userId}/follow`;
        
        try {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                }
            });
            
            const data = await response.json();
            
            if (response.ok) {
                button.classList.toggle('following');
                
                // Update follower count
                const card = button.closest('.user-card');
                const followerCount = card.querySelector('.followers-count');
                let count = parseInt(followerCount.textContent);
                count += isFollowing ? -1 : 1;
                followerCount.textContent = Math.max(0, count);
                
                this.showToast(data.message, 'success');
            } else {
                this.showToast(data.error || 'Failed to update follow status', 'error');
            }
            
        } catch (error) {
            console.error('Follow error:', error);
            this.showToast('Failed to update follow status', 'error');
        }
    }
    
    renderPagination(data) {
        if (!this.popularPagination) return;
        
        this.popularPagination.innerHTML = '';
        
        if (data.pages <= 1) return;
        
        const nav = document.createElement('div');
        nav.className = 'pagination';
        
        // Previous button
        if (data.has_prev) {
            const prevBtn = document.createElement('button');
            prevBtn.textContent = '← Previous';
            prevBtn.className = 'pagination-btn';
            prevBtn.addEventListener('click', () => {
                this.currentPage--;
                this.loadPopularUsers(this.currentPage);
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });
            nav.appendChild(prevBtn);
        }
        
        // Page info
        const pageInfo = document.createElement('span');
        pageInfo.className = 'page-info';
        pageInfo.textContent = `Page ${data.current_page} of ${data.pages}`;
        nav.appendChild(pageInfo);
        
        // Next button
        if (data.has_next) {
            const nextBtn = document.createElement('button');
            nextBtn.textContent = 'Next →';
            nextBtn.className = 'pagination-btn';
            nextBtn.addEventListener('click', () => {
                this.currentPage++;
                this.loadPopularUsers(this.currentPage);
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });
            nav.appendChild(nextBtn);
        }
        
        this.popularPagination.appendChild(nav);
    }
    
    createEmptyState(message) {
        const template = document.getElementById('empty-state-template');
        const empty = template.content.cloneNode(true);
        empty.querySelector('p').textContent = message;
        return empty;
    }
    
    showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        document.body.appendChild(toast);
        
        setTimeout(() => {
            toast.classList.add('show');
        }, 100);
        
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    new UserDiscoveryManager();
});
