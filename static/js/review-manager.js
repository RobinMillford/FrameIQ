/**
 * ReviewManager - Frontend manager for enhanced review system
 * Handles helpful votes, review feed, popular reviews, and interactions
 */

class ReviewManager {
    constructor(options = {}) {
        this.mediaId = options.mediaId || null;
        this.mediaType = options.mediaType || null;
        this.currentSort = options.defaultSort || 'all';
        this.currentPage = 1;
        this.perPage = 10;
        this.isLoading = false;
        this.hasMore = true;
        
        this.init();
    }

    init() {
        console.log('ReviewManager initialized');
        this.attachEventListeners();
        
        // Auto-load reviews if on media detail page
        if (this.mediaId) {
            this.loadMediaReviews();
        }
    }

    attachEventListeners() {
        // Helpful vote buttons
        document.addEventListener('click', (e) => {
            if (e.target.closest('.helpful-btn')) {
                e.preventDefault();
                const btn = e.target.closest('.helpful-btn');
                const reviewId = btn.dataset.reviewId;
                const isHelpful = btn.dataset.isHelpful === 'true';
                this.voteHelpful(reviewId, isHelpful);
            }
        });

        // Like review buttons
        document.addEventListener('click', (e) => {
            if (e.target.closest('.review-like-btn')) {
                e.preventDefault();
                const btn = e.target.closest('.review-like-btn');
                const reviewId = btn.dataset.reviewId;
                this.toggleLike(reviewId);
            }
        });

        // Sort dropdown
        const sortSelect = document.getElementById('review-sort');
        if (sortSelect) {
            sortSelect.addEventListener('change', (e) => {
                this.currentSort = e.target.value;
                this.currentPage = 1;
                this.loadMediaReviews();
            });
        }

        // Filter buttons (friends only, etc.)
        const friendsToggle = document.getElementById('friends-only-toggle');
        if (friendsToggle) {
            friendsToggle.addEventListener('change', (e) => {
                this.loadReviewFeed(e.target.checked);
            });
        }

        // Timeframe selector for popular reviews
        const timeframeSelect = document.getElementById('popular-timeframe');
        if (timeframeSelect) {
            timeframeSelect.addEventListener('change', (e) => {
                this.loadPopularReviews(e.target.value);
            });
        }

        // Infinite scroll
        window.addEventListener('scroll', () => {
            if (this.shouldLoadMore()) {
                this.loadMore();
            }
        });
    }

    /**
     * Vote on review helpfulness
     */
    async voteHelpful(reviewId, isHelpful) {
        try {
            const response = await fetch(`/api/reviews/${reviewId}/helpful`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ is_helpful: isHelpful })
            });

            const data = await response.json();

            if (response.ok) {
                this.updateHelpfulUI(reviewId, data);
                this.showToast(isHelpful ? 'Marked as helpful' : 'Marked as not helpful', 'success');
            } else {
                this.showToast(data.error || 'Failed to vote', 'error');
            }
        } catch (error) {
            console.error('Error voting helpful:', error);
            this.showToast('Network error', 'error');
        }
    }

    /**
     * Toggle like on review
     */
    async toggleLike(reviewId) {
        try {
            const card = document.querySelector(`[data-review-id="${reviewId}"]`);
            const isLiked = card?.querySelector('.review-like-btn')?.classList.contains('liked');
            const method = isLiked ? 'DELETE' : 'POST';

            const response = await fetch(`/api/reviews/${reviewId}/like`, {
                method: method,
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            const data = await response.json();

            if (response.ok) {
                this.updateLikeUI(reviewId, data);
            } else {
                this.showToast(data.error || 'Failed to like review', 'error');
            }
        } catch (error) {
            console.error('Error toggling like:', error);
            this.showToast('Network error', 'error');
        }
    }

    /**
     * Load reviews for specific media
     */
    async loadMediaReviews(append = false) {
        if (this.isLoading || !this.mediaId) return;

        this.isLoading = true;
        const container = document.getElementById('reviews-list');
        
        if (!append) {
            container.innerHTML = '<div class="loading-spinner">Loading reviews...</div>';
        }

        try {
            const url = `/api/media/${this.mediaId}/reviews?` +
                       `media_type=${this.mediaType}&sort=${this.currentSort}&page=${this.currentPage}&per_page=${this.perPage}`;
            
            const response = await fetch(url);
            const data = await response.json();

            if (response.ok) {
                if (!append) {
                    container.innerHTML = '';
                }

                if (data.reviews.length === 0 && !append) {
                    container.innerHTML = '<div class="no-reviews">No reviews yet. Be the first to review!</div>';
                } else {
                    data.reviews.forEach(review => {
                        container.appendChild(this.createReviewCard(review));
                    });
                }

                this.hasMore = data.has_next;
            } else {
                container.innerHTML = '<div class="error-message">Failed to load reviews</div>';
            }
        } catch (error) {
            console.error('Error loading reviews:', error);
            container.innerHTML = '<div class="error-message">Network error</div>';
        } finally {
            this.isLoading = false;
        }
    }

    /**
     * Load review feed (all users or friends only)
     */
    async loadReviewFeed(friendsOnly = false, append = false) {
        if (this.isLoading) return;

        this.isLoading = true;
        const container = document.getElementById('review-feed-container');
        
        if (!append) {
            container.innerHTML = '<div class="loading-spinner">Loading reviews...</div>';
        }

        try {
            const url = `/api/reviews/feed?` +
                       `friends_only=${friendsOnly}&page=${this.currentPage}&per_page=${this.perPage}`;
            
            const response = await fetch(url);
            const data = await response.json();

            if (response.ok) {
                if (!append) {
                    container.innerHTML = '';
                }

                if (data.reviews.length === 0 && !append) {
                    const msg = friendsOnly ? 
                        'No reviews from friends yet' : 
                        'No reviews yet';
                    container.innerHTML = `<div class="no-reviews">${msg}</div>`;
                } else {
                    data.reviews.forEach(review => {
                        container.appendChild(this.createReviewCard(review));
                    });
                }

                this.hasMore = data.has_next;
            } else {
                container.innerHTML = '<div class="error-message">Failed to load feed</div>';
            }
        } catch (error) {
            console.error('Error loading feed:', error);
            container.innerHTML = '<div class="error-message">Network error</div>';
        } finally {
            this.isLoading = false;
        }
    }

    /**
     * Load popular reviews by timeframe
     */
    async loadPopularReviews(timeframe = 'week', append = false) {
        if (this.isLoading) return;

        this.isLoading = true;
        const container = document.getElementById('popular-reviews-container');
        
        if (!append) {
            container.innerHTML = '<div class="loading-spinner">Loading popular reviews...</div>';
        }

        try {
            const url = `/api/reviews/popular?` +
                       `timeframe=${timeframe}&page=${this.currentPage}&per_page=${this.perPage}`;
            
            const response = await fetch(url);
            const data = await response.json();

            if (response.ok) {
                if (!append) {
                    container.innerHTML = '';
                }

                if (data.reviews.length === 0 && !append) {
                    container.innerHTML = '<div class="no-reviews">No popular reviews yet</div>';
                } else {
                    data.reviews.forEach(review => {
                        container.appendChild(this.createReviewCard(review, true));
                    });
                }

                this.hasMore = data.has_next;
            } else {
                container.innerHTML = '<div class="error-message">Failed to load popular reviews</div>';
            }
        } catch (error) {
            console.error('Error loading popular reviews:', error);
            container.innerHTML = '<div class="error-message">Network error</div>';
        } finally {
            this.isLoading = false;
        }
    }

    /**
     * Create a review card element
     */
    createReviewCard(review, showPopularity = false) {
        const card = document.createElement('div');
        card.className = 'review-card';
        card.dataset.reviewId = review.id;

        const formattedDate = this.formatDate(review.created_at);
        const isEdited = review.updated_at !== review.created_at;
        const rewatchBadge = review.rewatch ? '<span class="rewatch-badge">🔁 Rewatch</span>' : '';
        const spoilerWarning = review.contains_spoilers ? '<span class="spoiler-badge">⚠️ Spoilers</span>' : '';

        // User section
        const userSection = `
            <div class="review-header">
                <div class="review-user-info">
                    <img src="${review.user.profile_picture || '/static/images/default-avatar.png'}" 
                         alt="${review.user.username}" 
                         class="review-user-avatar">
                    <div class="review-user-details">
                        <a href="/user/${review.user.id}" class="review-username">${review.user.username}</a>
                        <span class="review-date">${formattedDate}${isEdited ? ' (edited)' : ''}</span>
                    </div>
                </div>
                ${this.createStarRating(review.rating)}
            </div>
        `;

        // Media info (only show in feed, not on media detail page)
        const mediaSection = !this.mediaId ? `
            <div class="review-media-info">
                <a href="/${review.media.media_type}/${review.media.id}">
                    <img src="https://image.tmdb.org/t/p/w92${review.media.poster_path}" 
                         alt="${review.media.title}" 
                         class="review-media-poster">
                    <span class="review-media-title">${review.media.title}</span>
                </a>
            </div>
        ` : '';

        // Review title
        const titleSection = review.title ? `
            <h3 class="review-title">${this.escapeHtml(review.title)}</h3>
        ` : '';

        // Review content
        const contentSection = `
            <div class="review-content ${review.contains_spoilers ? 'has-spoilers' : ''}">
                ${rewatchBadge} ${spoilerWarning}
                <p>${this.escapeHtml(review.content)}</p>
            </div>
        `;

        // Popularity score (for popular reviews page)
        const popularitySection = showPopularity ? `
            <div class="review-popularity">
                <span class="popularity-score">
                    📊 Score: ${review.helpful_count + review.likes_count}
                </span>
            </div>
        ` : '';

        // Interaction buttons
        const interactionSection = `
            <div class="review-interactions">
                <div class="review-stats">
                    <button class="review-like-btn ${review.user_liked ? 'liked' : ''}" 
                            data-review-id="${review.id}">
                        <i class="fas fa-heart"></i>
                        <span class="like-count">${review.likes_count}</span>
                    </button>
                    <span class="review-comments">
                        <i class="fas fa-comment"></i> ${review.comments_count}
                    </span>
                </div>
                
                <div class="helpful-section">
                    <span class="helpful-label">Was this helpful?</span>
                    <div class="helpful-buttons">
                        <button class="helpful-btn ${review.user_helpful_vote === true ? 'active' : ''}" 
                                data-review-id="${review.id}" 
                                data-is-helpful="true" 
                                title="Helpful">
                            <i class="fas fa-thumbs-up"></i>
                            <span class="helpful-count">${review.helpful_count}</span>
                        </button>
                        <button class="helpful-btn ${review.user_helpful_vote === false ? 'active' : ''}" 
                                data-review-id="${review.id}" 
                                data-is-helpful="false" 
                                title="Not helpful">
                            <i class="fas fa-thumbs-down"></i>
                            <span class="not-helpful-count">${review.not_helpful_count}</span>
                        </button>
                    </div>
                </div>
            </div>
        `;

        // Edit/delete buttons (only for author)
        const actionButtons = review.is_author_self ? `
            <div class="review-actions">
                <button class="btn-edit-review" data-review-id="${review.id}">
                    <i class="fas fa-edit"></i> Edit
                </button>
                <button class="btn-delete-review" data-review-id="${review.id}">
                    <i class="fas fa-trash"></i> Delete
                </button>
            </div>
        ` : '';

        card.innerHTML = `
            ${userSection}
            ${mediaSection}
            ${titleSection}
            ${contentSection}
            ${popularitySection}
            ${interactionSection}
            ${actionButtons}
        `;

        return card;
    }

    /**
     * Create star rating display
     */
    createStarRating(rating) {
        const fullStars = Math.floor(rating);
        const hasHalfStar = rating % 1 !== 0;
        const emptyStars = 5 - fullStars - (hasHalfStar ? 1 : 0);

        let stars = '<div class="review-rating">';
        
        // Full stars
        for (let i = 0; i < fullStars; i++) {
            stars += '<i class="fas fa-star"></i>';
        }
        
        // Half star
        if (hasHalfStar) {
            stars += '<i class="fas fa-star-half-alt"></i>';
        }
        
        // Empty stars
        for (let i = 0; i < emptyStars; i++) {
            stars += '<i class="far fa-star"></i>';
        }
        
        stars += `<span class="rating-value">${rating.toFixed(1)}</span>`;
        stars += '</div>';
        
        return stars;
    }

    /**
     * Update helpful vote UI
     */
    updateHelpfulUI(reviewId, data) {
        const card = document.querySelector(`[data-review-id="${reviewId}"]`);
        if (!card) return;

        // Update counts
        card.querySelector('.helpful-count').textContent = data.helpful_count;
        card.querySelector('.not-helpful-count').textContent = data.not_helpful_count;

        // Update active state
        const helpfulBtn = card.querySelector('.helpful-btn[data-is-helpful="true"]');
        const notHelpfulBtn = card.querySelector('.helpful-btn[data-is-helpful="false"]');

        helpfulBtn.classList.remove('active');
        notHelpfulBtn.classList.remove('active');

        if (data.user_helpful_vote === true) {
            helpfulBtn.classList.add('active');
        } else if (data.user_helpful_vote === false) {
            notHelpfulBtn.classList.add('active');
        }
    }

    /**
     * Update like UI
     */
    updateLikeUI(reviewId, data) {
        const card = document.querySelector(`[data-review-id="${reviewId}"]`);
        if (!card) return;

        const likeBtn = card.querySelector('.review-like-btn');
        const likeCount = card.querySelector('.like-count');

        likeCount.textContent = data.likes_count;

        if (data.user_liked) {
            likeBtn.classList.add('liked');
        } else {
            likeBtn.classList.remove('liked');
        }
    }

    /**
     * Check if should load more (infinite scroll)
     */
    shouldLoadMore() {
        if (this.isLoading || !this.hasMore) return false;

        const scrollPosition = window.innerHeight + window.scrollY;
        const threshold = document.body.offsetHeight - 500;

        return scrollPosition >= threshold;
    }

    /**
     * Load more reviews (pagination)
     */
    loadMore() {
        this.currentPage++;
        
        if (document.getElementById('review-feed-container')) {
            const friendsOnly = document.getElementById('friends-only-toggle')?.checked || false;
            this.loadReviewFeed(friendsOnly, true);
        } else if (document.getElementById('popular-reviews-container')) {
            const timeframe = document.getElementById('popular-timeframe')?.value || 'week';
            this.loadPopularReviews(timeframe, true);
        } else if (this.mediaId) {
            this.loadMediaReviews(true);
        }
    }

    /**
     * Format date relative to now
     */
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
        
        return date.toLocaleDateString('en-US', { 
            month: 'short', 
            day: 'numeric', 
            year: date.getFullYear() !== now.getFullYear() ? 'numeric' : undefined 
        });
    }

    /**
     * Escape HTML to prevent XSS
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Show toast notification
     */
    showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        
        document.body.appendChild(toast);
        
        setTimeout(() => {
            toast.classList.add('show');
        }, 10);
        
        setTimeout(() => {
            toast.classList.remove('show');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
}

// Auto-initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    // Check if we're on a media detail page
    const mediaElement = document.querySelector('[data-media-id]');
    if (mediaElement) {
        window.reviewManager = new ReviewManager({
            mediaId: mediaElement.dataset.mediaId,
            mediaType: mediaElement.dataset.mediaType,
            defaultSort: 'recent'
        });
    }
    
    // Check if we're on review feed page
    if (document.getElementById('review-feed-container')) {
        window.reviewManager = new ReviewManager();
        reviewManager.loadReviewFeed(false);
    }
    
    // Check if we're on popular reviews page
    if (document.getElementById('popular-reviews-container')) {
        window.reviewManager = new ReviewManager();
        reviewManager.loadPopularReviews('week');
    }
});
