/**
 * Profile Enhancements Manager
 * Displays badges, achievements, and enhanced stats on profile pages
 */

class ProfileEnhancementsManager {
    constructor(userId) {
        this.userId = userId;
    }

    async loadBadges(containerId) {
        const container = document.getElementById(containerId);
        if (!container) {
            console.error('Badges container not found');
            return;
        }

        try {
            const response = await fetch(`/api/users/${this.userId}/badges`);
            const data = await response.json();

            if (data.success) {
                this.renderBadges(container, data.badges);
            } else {
                container.innerHTML = '<p>Unable to load badges</p>';
            }
        } catch (error) {
            console.error('Error loading badges:', error);
            container.innerHTML = '<p>Error loading badges</p>';
        }
    }

    renderBadges(container, badges) {
        if (!badges || badges.length === 0) {
            container.innerHTML = `
                <div class="no-badges">
                    <p>No badges earned yet. Keep exploring!</p>
                </div>
            `;
            return;
        }

        let html = '<div class="badges-grid">';
        badges.forEach(badge => {
            html += `
                <div class="badge-item" title="${badge.description}">
                    <div class="badge-icon">${badge.icon}</div>
                    <div class="badge-name">${badge.name}</div>
                </div>
            `;
        });
        html += '</div>';
        html += `<p class="badges-count">Earned ${badges.length} badge${badges.length !== 1 ? 's' : ''}</p>`;

        container.innerHTML = html;
    }

    async loadAchievements(containerId) {
        const container = document.getElementById(containerId);
        if (!container) {
            console.error('Achievements container not found');
            return;
        }

        try {
            const response = await fetch(`/api/users/${this.userId}/achievements`);
            const data = await response.json();

            if (data.success) {
                this.renderAchievements(container, data);
            } else {
                container.innerHTML = '<p>Unable to load achievements</p>';
            }
        } catch (error) {
            console.error('Error loading achievements:', error);
            container.innerHTML = '<p>Error loading achievements</p>';
        }
    }

    renderAchievements(container, data) {
        let html = `
            <div class="achievements-header">
                <h3>🏆 Achievements</h3>
                <p>${data.total_badges} badges earned</p>
            </div>
        `;

        // Progress bars
        if (data.progress && data.progress.length > 0) {
            html += '<div class="progress-section"><h4>Next Goals</h4>';
            data.progress.forEach(item => {
                html += `
                    <div class="progress-item">
                        <div class="progress-header">
                            <span class="progress-goal">${item.goal}</span>
                            <span class="progress-numbers">${item.current}/${item.target}</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill" style="width: ${item.percentage}%"></div>
                        </div>
                    </div>
                `;
            });
            html += '</div>';
        }

        // Badge showcase
        if (data.badges && data.badges.length > 0) {
            html += '<div class="badges-showcase">';
            html += '<h4>Recent Badges</h4>';
            html += '<div class="badges-list">';
            
            // Show latest 6 badges
            const recentBadges = data.badges.slice(-6).reverse();
            recentBadges.forEach(badge => {
                html += `
                    <div class="badge-showcase-item" title="${badge.description}">
                        <span class="badge-icon-large">${badge.icon}</span>
                        <span class="badge-name-small">${badge.name}</span>
                    </div>
                `;
            });
            html += '</div></div>';
        }

        container.innerHTML = html;
    }

    async loadEnhancedStats(containerId) {
        const container = document.getElementById(containerId);
        if (!container) {
            console.error('Enhanced stats container not found');
            return;
        }

        try {
            const response = await fetch(`/api/users/${this.userId}/stats/enhanced`);
            const data = await response.json();

            if (data.success) {
                this.renderEnhancedStats(container, data.stats);
            } else {
                container.innerHTML = '<p>Unable to load statistics</p>';
            }
        } catch (error) {
            console.error('Error loading stats:', error);
            container.innerHTML = '<p>Error loading statistics</p>';
        }
    }

    renderEnhancedStats(container, stats) {
        let html = `
            <div class="enhanced-stats">
                <div class="stats-grid">
                    <div class="stat-box">
                        <div class="stat-value">${stats.total_reviews}</div>
                        <div class="stat-label">Reviews</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${stats.viewed_count}</div>
                        <div class="stat-label">Watched</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${stats.watchlist_count}</div>
                        <div class="stat-label">Watchlist</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${stats.followers_count}</div>
                        <div class="stat-label">Followers</div>
                    </div>
                </div>
        `;

        // Rating info
        if (stats.avg_rating) {
            html += `
                <div class="rating-summary">
                    <h4>Average Rating</h4>
                    <div class="avg-rating">${stats.avg_rating}/10</div>
                </div>
            `;
        }

        // Recent activity
        html += `
            <div class="recent-activity-summary">
                <h4>Last 30 Days</h4>
                <div class="activity-stats">
                    <span>${stats.recent_activity_30d.reviews} reviews</span>
                    <span>${stats.recent_activity_30d.likes} likes</span>
                    <span>${stats.recent_activity_30d.comments} comments</span>
                </div>
            </div>
        `;

        // Top tags
        if (stats.top_tags && stats.top_tags.length > 0) {
            html += '<div class="top-tags-section"><h4>Top Tags</h4><div class="top-tags-list">';
            stats.top_tags.forEach(tag => {
                html += `<span class="top-tag">${tag.name} (${tag.count})</span>`;
            });
            html += '</div></div>';
        }

        html += '</div>';
        container.innerHTML = html;
    }
}

// Auto-initialize if data attributes are present
document.addEventListener('DOMContentLoaded', () => {
    const badgesContainer = document.querySelector('[data-profile-badges]');
    if (badgesContainer) {
        const userId = badgesContainer.dataset.userId;
        if (userId) {
            const manager = new ProfileEnhancementsManager(userId);
            manager.loadBadges(badgesContainer.id);
        }
    }

    const achievementsContainer = document.querySelector('[data-profile-achievements]');
    if (achievementsContainer) {
        const userId = achievementsContainer.dataset.userId;
        if (userId) {
            const manager = new ProfileEnhancementsManager(userId);
            manager.loadAchievements(achievementsContainer.id);
        }
    }

    const statsContainer = document.querySelector('[data-enhanced-stats]');
    if (statsContainer) {
        const userId = statsContainer.dataset.userId;
        if (userId) {
            const manager = new ProfileEnhancementsManager(userId);
            manager.loadEnhancedStats(statsContainer.id);
        }
    }
});
