/**
 * More Like This Manager
 * Displays similar movies/TV shows recommendations
 */

class MoreLikeThisManager {
    constructor(mediaId, mediaType) {
        this.mediaId = mediaId;
        this.mediaType = mediaType;
        this.recommendations = null;
    }

    async load() {
        try {
            const response = await fetch(`/api/media/${this.mediaId}/recommendations?media_type=${this.mediaType}&limit=12`);
            const data = await response.json();
            
            if (data.success) {
                this.recommendations = data.recommendations;
                this.render();
            } else {
                console.error('Failed to load recommendations:', data.error);
                this.renderError();
            }
        } catch (error) {
            console.error('Error loading recommendations:', error);
            this.renderError();
        }
    }

    render() {
        const container = document.getElementById('more-like-this');
        if (!container) return;

        if (!this.recommendations || this.recommendations.length === 0) {
            container.innerHTML = `
                <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                    <h3 class="text-xl font-bold text-white mb-4">
                        <i class="fas fa-film"></i> More Like This
                    </h3>
                    <p class="text-gray-400 text-center py-4">
                        No recommendations available at this time.
                    </p>
                </div>
            `;
            return;
        }

        let html = `
            <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                <h3 class="text-2xl font-bold text-white mb-6">
                    <i class="fas fa-film"></i> More Like This
                </h3>
                
                <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-4">
        `;

        this.recommendations.forEach(item => {
            const url = `/${this.mediaType}/${item.id}`;
            const rating = item.vote_average ? item.vote_average.toFixed(1) : 'N/A';
            const year = item.release_date ? new Date(item.release_date).getFullYear() : '';
            
            html += `
                <a href="${url}" class="group block">
                    <div class="relative overflow-hidden rounded-lg shadow-lg transition-transform duration-300 group-hover:scale-105 group-hover:shadow-2xl">
                        ${item.poster_path ? `
                            <img src="${item.poster_path}" 
                                 alt="${item.title}"
                                 class="w-full h-auto aspect-[2/3] object-cover">
                        ` : `
                            <div class="w-full aspect-[2/3] bg-gray-700 flex items-center justify-center">
                                <i class="fas fa-film text-4xl text-gray-500"></i>
                            </div>
                        `}
                        
                        <!-- Overlay -->
                        <div class="absolute inset-0 bg-gradient-to-t from-black via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity duration-300">
                            <div class="absolute bottom-0 left-0 right-0 p-3">
                                <div class="flex items-center justify-between mb-1">
                                    ${item.vote_average ? `
                                        <span class="text-yellow-400 text-sm font-semibold">
                                            <i class="fas fa-star"></i> ${rating}
                                        </span>
                                    ` : ''}
                                    ${year ? `
                                        <span class="text-gray-300 text-xs">${year}</span>
                                    ` : ''}
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <h4 class="mt-2 text-sm font-medium text-white line-clamp-2 group-hover:text-indigo-400 transition-colors">
                        ${item.title}
                    </h4>
                </a>
            `;
        });

        html += `
                </div>
            </div>
        `;

        container.innerHTML = html;
    }

    renderError() {
        const container = document.getElementById('more-like-this');
        if (!container) return;

        container.innerHTML = `
            <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                <h3 class="text-xl font-bold text-white mb-4">
                    <i class="fas fa-film"></i> More Like This
                </h3>
                <p class="text-red-400 text-center py-4">
                    <i class="fas fa-exclamation-triangle"></i> 
                    Failed to load recommendations. Please try again later.
                </p>
            </div>
        `;
    }
}

// Auto-initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('more-like-this');
    if (container) {
        const mediaId = container.dataset.mediaId;
        const mediaType = container.dataset.mediaType;
        
        if (mediaId && mediaType) {
            const manager = new MoreLikeThisManager(mediaId, mediaType);
            manager.load();
        }
    }
});
