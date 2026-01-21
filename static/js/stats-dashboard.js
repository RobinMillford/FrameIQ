class StatsDashboard {
    constructor(userId, options = {}) {
        this.userId = userId;
        this.containerId = options.containerId || 'insights-content';
        this.genreChartId = options.genreChartId || 'genre-chart';
        this.activityChartId = options.activityChartId || 'activity-chart';
        this.ratingChartId = options.ratingChartId || 'rating-dist-chart';
        this.mediaMixChartId = options.mediaMixChartId || 'media-mix-chart';
        
        this.data = null;
        this.charts = {};
        
        this.init();
    }
    
    async init() {
        await this.fetchData();
        if (this.data) {
            this.renderCharts();
            this.updateQuickStats();
        }
    }
    
    async fetchData() {
        try {
            const response = await fetch(`/api/users/${this.userId}/stats`);
            if (!response.ok) throw new Error('Failed to fetch stats');
            this.data = await response.json();
        } catch (error) {
            console.error('Error fetching dashboard data:', error);
            const container = document.getElementById(this.containerId);
            if (container) {
                container.innerHTML = `<div class="text-center py-12 text-gray-500 bg-gray-800/50 rounded-xl border border-gray-700">
                    <i class="fas fa-exclamation-circle text-3xl mb-3 text-gray-600"></i>
                    <p>Failed to load statistics. Please try again later.</p>
                </div>`;
            }
        }
    }
    
    updateQuickStats() {
        const stats = this.data.stats;
        const elements = {
            'stat-total-watched': stats.total_watched,
            'stat-total-reviews': stats.total_reviews,
            'stat-avg-rating': stats.avg_rating,
            'stat-movie-tv-split': `${stats.movies_watched} Movies / ${stats.tv_watched} TV`,
            'stat-watchlist-completion': `${stats.watchlist_completion}%`,
            'stat-top-genre': this.data.genre_distribution.labels[0] || 'N/A',
            'stat-best-genre': this.data.genre_performance.labels[0] || 'N/A'
        };
        
        for (const [id, value] of Object.entries(elements)) {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        }
    }
    
    renderCharts() {
        this.renderGenreChart();
        this.renderActivityChart();
        this.renderRatingDistChart();
        this.renderMediaMixChart();
    }
    
    renderGenreChart() {
        const ctx = document.getElementById(this.genreChartId);
        if (!ctx) return;
        
        if (!this.data.genre_distribution.labels.length) {
            this.showEmptyState(ctx, 'No genre data available yet.');
            return;
        }
        
        this.charts.genre = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: this.data.genre_distribution.labels,
                datasets: [{
                    data: this.data.genre_distribution.data,
                    backgroundColor: [
                        '#6366f1', '#a855f7', '#ec4899', '#f43f5e', '#f59e0b'
                    ],
                    borderWidth: 0,
                    hoverOffset: 10
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: '#9ca3af',
                            font: { size: 11 },
                            padding: 20,
                            usePointStyle: true
                        }
                    }
                },
                cutout: '70%'
            }
        });
    }

    renderRatingDistChart() {
        const ctx = document.getElementById(this.ratingChartId);
        if (!ctx) return;

        if (!this.data.rating_distribution.data.length || this.data.stats.total_reviews === 0) {
            this.showEmptyState(ctx, 'Write reviews to see your rating spread.');
            return;
        }

        this.charts.rating = new Chart(ctx, {
            type: 'bar',
            data: {
                labels: this.data.rating_distribution.labels,
                datasets: [{
                    label: 'Count',
                    data: this.data.rating_distribution.data,
                    backgroundColor: 'rgba(99, 102, 241, 0.6)',
                    borderColor: '#6366f1',
                    borderWidth: 1,
                    borderRadius: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(75, 85, 99, 0.1)' },
                        ticks: { color: '#9ca3af', stepSize: 1 }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#9ca3af' }
                    }
                }
            }
        });
    }

    renderMediaMixChart() {
        const ctx = document.getElementById(this.mediaMixChartId);
        if (!ctx) return;

        const stats = this.data.stats;
        if (stats.total_watched === 0) {
            this.showEmptyState(ctx, 'Watch something to see your mix.');
            return;
        }

        this.charts.mediaMix = new Chart(ctx, {
            type: 'pie',
            data: {
                labels: ['Movies', 'TV Shows'],
                datasets: [{
                    data: [stats.movies_watched, stats.tv_watched],
                    backgroundColor: ['#6366f1', '#10b981'],
                    borderWidth: 0
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: {
                            color: '#9ca3af',
                            usePointStyle: true
                        }
                    }
                }
            }
        });
    }
    
    renderActivityChart() {
        const ctx = document.getElementById(this.activityChartId);
        if (!ctx) return;
        
        if (!this.data.monthly_activity.labels.length) {
            this.showEmptyState(ctx, 'No review activity tracked yet.');
            return;
        }
        
        this.charts.activity = new Chart(ctx, {
            type: 'line',
            data: {
                labels: this.data.monthly_activity.labels,
                datasets: [{
                    label: 'Reviews',
                    data: this.data.monthly_activity.data,
                    borderColor: '#6366f1',
                    backgroundColor: 'rgba(99, 102, 241, 0.1)',
                    fill: true,
                    tension: 0.4,
                    pointBackgroundColor: '#6366f1',
                    pointBorderColor: '#fff',
                    pointHoverRadius: 6
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: { color: 'rgba(75, 85, 99, 0.1)' },
                        ticks: { color: '#9ca3af', stepSize: 1 }
                    },
                    x: {
                        grid: { display: false },
                        ticks: { color: '#9ca3af' }
                    }
                }
            }
        });
    }

    showEmptyState(canvas, message) {
        const parent = canvas.parentElement;
        if (!parent) return;
        
        // Hide canvas
        canvas.style.display = 'none';
        
        // Add message
        const emptyDiv = document.createElement('div');
        emptyDiv.className = 'absolute inset-0 flex flex-col items-center justify-center text-gray-500 text-sm italic';
        emptyDiv.innerHTML = `
            <i class="fas fa-chart-pie mb-2 opacity-50"></i>
            <span>${message}</span>
        `;
        parent.appendChild(emptyDiv);
    }
}
