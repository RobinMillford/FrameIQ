        function tmdbUrl(path, params) {
            const q = new URLSearchParams(Object.assign({path}, params || {}));
            return '/api/tmdb/proxy?' + q.toString();
        }

        document.addEventListener("DOMContentLoaded", function() {
            fetchTrendingMovies();
            fetchLatestTrailers();
            setupMobileMenu();
            fetchMoviesByGenres();
        });

        function setupMobileMenu() {
            const mobileMenuButton = document.getElementById('mobile-menu-button');
            const mobileMenu = document.createElement('div');
            mobileMenu.className = 'fixed top-16 left-0 right-0 bg-gray-900 glass-effect hidden';
            mobileMenu.innerHTML = `
                <div class="container mx-auto px-4 py-4">
                    <div class="flex flex-col space-y-4">
                        <a href="/" class="text-white hover:text-indigo-400">Home</a>
                        <a href="/tv_shows" class="text-white hover:text-indigo-400">TV Shows</a>
                        <a href="/chat" class="text-white hover:text-indigo-400">AI Chat</a>
                    </div>
                </div>
            `;
            document.body.appendChild(mobileMenu);

            mobileMenuButton.addEventListener('click', () => {
                mobileMenu.classList.toggle('hidden');
            });
        }
    
        function fetchTrendingMovies() {
            fetch(tmdbUrl('/trending/movie/week'))
            .then(response => response.json())
            .then(data => {
                const slider = document.getElementById("trending-slider");
                slider.innerHTML = ''; // Clear loading skeletons
                data.results.forEach(movie => {
                    const posterUrl = movie.poster_path 
                        ? `https://image.tmdb.org/t/p/w500${movie.poster_path}` 
                        : "/static/images/no-poster.svg";
                    
                    const movieCard = document.createElement("div");
                    movieCard.className = "movie-card flex-shrink-0 w-48 glass-effect rounded-lg overflow-hidden";
                    movieCard.innerHTML = `
                        <a href="/movie/${movie.id}" class="block">
                            <img src="${posterUrl}" alt="${movie.title}" class="w-full h-72 object-cover">
                            <div class="p-4">
                                <h3 class="font-semibold text-sm">${movie.title}</h3>
                                <p class="text-gray-400 text-xs">${movie.release_date}</p>
                            </div>
                        </a>
                    `;
                    slider.appendChild(movieCard);
                });
            })
        }

        async function fetchLatestTrailers() {
            try {
    const upcomingUrl = tmdbUrl('/movie/upcoming', {language: 'en-US', page: 1});
                const response = await fetch(upcomingUrl);
                const data = await response.json();
                
                const movies = data.results.slice(0, 6); // Get first 6 upcoming movies
        const trailerPromises = movies.map(movie => fetchMovieTrailer(movie.id));
        const trailers = await Promise.all(trailerPromises);

                const featuredTrailers = document.getElementById('featured-trailers');
                featuredTrailers.innerHTML = ''; // Clear loading skeletons

                movies.forEach((movie, index) => {
                    if (trailers[index]) {
                        const trailerCard = document.createElement('div');
                        trailerCard.className = 'trailer-card glass-effect rounded-lg overflow-hidden';
                        trailerCard.innerHTML = `
                            <div class="aspect-w-16 aspect-h-9">
                                <iframe src="${trailers[index]}" 
                                        class="w-full h-64 rounded-lg"
                                        allowFullscreen>
                                </iframe>
                            </div>
                            <div class="p-4">
                                <h3 class="font-semibold text-lg mb-2">${movie.title}</h3>
                                <p class="text-gray-400 text-sm">${movie.release_date}</p>
                            </div>
                        `;
                        featuredTrailers.appendChild(trailerCard);
                    }
                });
    } catch (error) {
    }
}

async function fetchMovieTrailer(movieId) {
    const url = tmdbUrl(`/movie/${movieId}/videos`, {language: 'en-US'});

    try {
        const response = await fetch(url);
        const data = await response.json();

                let trailer = data.results.find(video => 
                    video.type === 'Trailer' && 
                    video.site === "YouTube" && 
                    video.official
                );
                
        if (!trailer) {
                    trailer = data.results.find(video => 
                        video.type === 'Teaser' && 
                        video.site === "YouTube"
                    );
        }
                
        if (!trailer) {
            trailer = data.results.find(video => video.site === "YouTube");
        }

        return trailer ? `https://www.youtube.com/embed/${trailer.key}` : null;
    } catch (error) {
        return null;
    }
}

        // Search functionality with modern styling
        document.getElementById("movie_name").addEventListener("input", function() {
            const input = this.value.toLowerCase();
            const autocompleteContainer = document.getElementById("autocomplete-items");
            autocompleteContainer.innerHTML = "";

            if (input.length < 2) {
                autocompleteContainer.style.display = "none";
                return;
            }

            fetch(tmdbUrl('/search/movie', {language: 'en-US', query: input, page: 1, include_adult: true}))
            .then(response => response.json())
            .then(data => {
                data.results.forEach(movie => {
                    const suggestion = document.createElement("div");
                    const posterUrl = movie.poster_path 
                        ? `https://image.tmdb.org/t/p/w92/${movie.poster_path}` 
                        : "/static/images/no-poster.svg";
                    
                    suggestion.className = "autocomplete-item flex items-center p-2 glass-effect";
                    suggestion.innerHTML = `
                        <img src="${posterUrl}" alt="Poster" class="w-12 h-18 object-cover rounded mr-3 flex-shrink-0">
                        <div class="min-w-0 flex-1 overflow-hidden">
                            <div class="font-medium truncate">${movie.title}</div>
                            <div class="text-sm text-gray-400 truncate">${movie.release_date}</div>
                        </div>
                    `;
                    
                    suggestion.addEventListener("click", function() {
                        document.getElementById("movie_name").value = movie.title;
                        autocompleteContainer.style.display = "none";
                    });
                    
                    autocompleteContainer.appendChild(suggestion);
                });

                autocompleteContainer.style.display = autocompleteContainer.children.length > 0 ? "block" : "none";
            })
        });

        document.addEventListener("click", function(e) {
            if (!e.target.matches("#movie_name")) {
                document.getElementById("autocomplete-items").style.display = "none";
            }
        });

        function openChat() {
            window.location.href = "/chat";
        }

        // Add this new function to fetch movies by genres
        async function fetchMoviesByGenres() {
            const genres = {
                'action': 28,
                'comedy': 35,
                'drama': 18,
                'romance': 10749,
                'thriller': 53,
                'horror': 27
            };

            for (const [genre, id] of Object.entries(genres)) {
                try {
                    const response = await fetch(tmdbUrl('/discover/movie', {with_genres: id, sort_by: 'popularity.desc', page: 1}));
                    const data = await response.json();
                    
                    const container = document.getElementById(`${genre}-movies`);
                    container.innerHTML = ''; // Clear loading skeletons
                    
                    data.results.slice(0, 5).forEach(movie => {
                        const posterUrl = movie.poster_path 
                            ? `https://image.tmdb.org/t/p/w500${movie.poster_path}` 
                            : "/static/images/no-poster.svg";
                        
                        const movieCard = document.createElement("div");
                        movieCard.className = "movie-card flex-shrink-0 w-32 glass-effect rounded-lg overflow-hidden";
                        movieCard.innerHTML = `
                            <a href="/movie/${movie.id}" class="block">
                                <img src="${posterUrl}" alt="${movie.title}" class="w-full h-48 object-cover">
                                <div class="p-2">
                                    <h3 class="font-semibold text-xs truncate">${movie.title}</h3>
                                    <p class="text-gray-400 text-xs">${movie.release_date}</p>
                                </div>
                            </a>
                        `;
                        container.appendChild(movieCard);
                    });
                } catch (error) {
                }
            }
        }
