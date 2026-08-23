        function tmdbUrl(path, params) {
            const q = new URLSearchParams(Object.assign({path}, params || {}));
            return '/api/tmdb/proxy?' + q.toString();
        }

            document.addEventListener("DOMContentLoaded", function() {
                fetchTrendingTVShows();
            fetchBestTVShows();
            setupMobileMenu();
            fetchTVShowsByGenres();
        });

        function setupMobileMenu() {
            const mobileMenuButton = document.getElementById('mobile-menu-button');
            const mobileMenu = document.createElement('div');
            mobileMenu.className = 'fixed top-16 left-0 right-0 bg-gray-900 glass-effect hidden';
            mobileMenu.innerHTML = `
                <div class="container mx-auto px-4 py-4">
                    <div class="flex flex-col space-y-4">
                        <a href="/" class="text-white hover:text-blue-400">Home</a>
                        <a href="/tv_shows" class="text-white hover:text-blue-400">TV Shows</a>
                        <a href="/chat" class="text-white hover:text-blue-400">AI Chat</a>
                    </div>
                </div>
            `;
            document.body.appendChild(mobileMenu);

            mobileMenuButton.addEventListener('click', () => {
                mobileMenu.classList.toggle('hidden');
            });
        }
        
            function fetchTrendingTVShows() {
            fetch(tmdbUrl('/trending/tv/week'))
                .then(response => response.json())
                .then(data => {
                const slider = document.getElementById("trending-slider");
                slider.innerHTML = ''; // Clear loading skeletons
                    data.results.forEach(show => {
                    const posterUrl = show.poster_path 
                        ? `https://image.tmdb.org/t/p/w500${show.poster_path}` 
                        : "/static/images/no-poster.svg";
                    
                    const showCard = document.createElement("div");
                    showCard.className = "tv-card flex-shrink-0 w-48 glass-effect rounded-lg overflow-hidden";
                        showCard.innerHTML = `
                        <a href="/tv/${show.id}" class="block">
                            <img src="${posterUrl}" alt="${show.name}" class="w-full h-72 object-cover">
                            <div class="p-4">
                                <h3 class="font-semibold text-sm">${show.name}</h3>
                                <p class="text-gray-400 text-xs">${show.first_air_date}</p>
                            </div>
                            </a>
                        `;
                        slider.appendChild(showCard);
                    });
                })
                .catch(() => {});
            }

        // Add new function to fetch collection images
        async function fetchBestTVShows() {
            try {
                // Fetch top-rated TV shows
                const response = await fetch(tmdbUrl('/tv/top_rated', {language: 'en-US', page: 1}));
                const data = await response.json();
                
                if (data.results && data.results.length > 0) {
                    const slideshow = document.getElementById('bestShowsSlideshow');
                    const indicators = document.getElementById('slideIndicators');
                    
                    // Clear existing content
                    slideshow.innerHTML = '';
                    indicators.innerHTML = '';
                    
                    // Group shows into slides (3 shows per slide)
                    const showsPerSlide = 3;
                    const totalSlides = Math.ceil(data.results.length / showsPerSlide);
                    
                    for (let i = 0; i < totalSlides; i++) {
                        const slide = document.createElement('div');
                        slide.className = 'slide';
                        
                        // Add shows to this slide
                        const slideShows = data.results.slice(i * showsPerSlide, (i + 1) * showsPerSlide);
                        slideShows.forEach(show => {
                            const posterUrl = show.poster_path 
                                ? `https://image.tmdb.org/t/p/w500${show.poster_path}` 
                                : "/static/images/no-poster.svg";
                            
                            const showCard = document.createElement('div');
                            showCard.className = 'show-card glass-effect';
                            showCard.innerHTML = `
                                <div class="relative">
                                    <a href="/tv/${show.id}">
                                        <img src="${posterUrl}" alt="${show.name}" class="w-full">
                                        <div class="show-rating">
                                            <svg class="w-5 h-5 text-yellow-400" fill="currentColor" viewBox="0 0 20 20">
                                                <path d="M9.049 2.927c.3-.921 1.603-.921 1.902 0l1.07 3.292a1 1 0 00.95.69h3.462c.969 0 1.371 1.24.588 1.81l-2.8 2.034a1 1 0 00-.364 1.118l1.07 3.292c.3.921-.755 1.688-1.54 1.118l-2.8-2.034a1 1 0 00-1.175 0l-2.8 2.034c-.784.57-1.838-.197-1.539-1.118l1.07-3.292a1 1 0 00-.364-1.118L2.98 8.72c-.783-.57-.38-1.81.588-1.81h3.461a1 1 0 00.951-.69l1.07-3.292z"/>
                                            </svg>
                                            <span>${show.vote_average.toFixed(1)}</span>
                                        </div>
                                        <div class="show-info">
                                            <h3 class="text-xl font-bold mb-2">${show.name}</h3>
                                            <p class="text-sm text-gray-300">${show.first_air_date}</p>
                                        </div>
                                    </a>
                                </div>
                            `;
                            slide.appendChild(showCard);
                        });
                        
                        slideshow.appendChild(slide);
                        
                        // Add indicator
                        const indicator = document.createElement('div');
                        indicator.className = `indicator ${i === 0 ? 'active' : ''}`;
                        indicator.addEventListener('click', () => goToSlide(i));
                        indicators.appendChild(indicator);
                    }
                    
                    // Initialize slideshow controls
                    initializeSlideshow(totalSlides);
                }
    } catch (error) {
            }
        }

        function initializeSlideshow(totalSlides) {
            let currentSlide = 0;
            const slideshow = document.getElementById('bestShowsSlideshow');
            const indicators = document.querySelectorAll('.indicator');
            
            function updateSlide() {
                slideshow.style.transform = `translateX(-${currentSlide * 100}%)`;
                indicators.forEach((indicator, index) => {
                    indicator.classList.toggle('active', index === currentSlide);
                });
            }
            
            function goToSlide(index) {
                currentSlide = index;
                updateSlide();
            }
            
            document.getElementById('prevSlide').addEventListener('click', () => {
                currentSlide = (currentSlide - 1 + totalSlides) % totalSlides;
                updateSlide();
            });
            
            document.getElementById('nextSlide').addEventListener('click', () => {
                currentSlide = (currentSlide + 1) % totalSlides;
                updateSlide();
            });
            
            // Auto-advance slides every 5 seconds
            setInterval(() => {
                currentSlide = (currentSlide + 1) % totalSlides;
                updateSlide();
            }, 5000);
        }

        // Search functionality with modern styling
        document.getElementById("show_name").addEventListener("input", function() {
            const input = this.value.toLowerCase();
            const autocompleteContainer = document.getElementById("autocomplete-items");
            autocompleteContainer.innerHTML = "";

            if (input.length < 2) {
                autocompleteContainer.style.display = "none";
                return;
            }

            fetch(tmdbUrl('/search/tv', {language: 'en-US', query: input, page: 1, include_adult: true}))
            .then(response => response.json())
            .then(data => {
                data.results.forEach(show => {
                    const suggestion = document.createElement("div");
                    const posterUrl = show.poster_path 
                        ? `https://image.tmdb.org/t/p/w92/${show.poster_path}` 
                        : "/static/images/no-poster.svg";
                    
                    suggestion.className = "autocomplete-item flex items-center p-2 glass-effect";
                    suggestion.innerHTML = `
                        <img src="${posterUrl}" alt="Poster" class="w-12 h-18 object-cover rounded mr-3 flex-shrink-0">
                        <div class="min-w-0 flex-1 overflow-hidden">
                            <div class="font-medium truncate">${show.name}</div>
                            <div class="text-sm text-gray-400 truncate">${show.first_air_date}</div>
                        </div>
                    `;
                    
                    suggestion.addEventListener("click", function() {
                        document.getElementById("show_name").value = show.name;
                        autocompleteContainer.style.display = "none";
                        // Submit the form for recommendations
                        document.querySelector('form[action="/tv_recommend"]').submit();
                    });
                    
                    autocompleteContainer.appendChild(suggestion);
                });

                autocompleteContainer.style.display = autocompleteContainer.children.length > 0 ? "block" : "none";
            })
            .catch(() => {});
        });

        document.addEventListener("click", function(e) {
            if (!e.target.matches("#show_name")) {
                document.getElementById("autocomplete-items").style.display = "none";
            }
        });

        function openChat() {
            window.location.href = "/chat";
        }

        // Add this new function to fetch TV shows by genres
        async function fetchTVShowsByGenres() {
            const genres = {
                'action': 10759,
                'comedy': 35,
                'drama': 18,
                'family': 10751,
                'kids': 10762,
                'animation': 16,
                'crime': 80,
                'documentary': 99,
                'mystery': 9648,
                'scifi': 10765,  // Updated to the correct ID for Sci-Fi & Fantasy
                'war': 10768,
                'western': 37
            };

            for (const [genre, id] of Object.entries(genres)) {
                try {
                    let url = tmdbUrl('/discover/tv', {with_genres: id, sort_by: 'popularity.desc', page: 1});
                    
                    const response = await fetch(url);
                    const data = await response.json();
                    
                    const container = document.getElementById(`${genre}-tv`);
                    if (container) {
                        container.innerHTML = ''; // Clear loading skeletons
                        
                        if (data.results && data.results.length > 0) {
                            data.results.slice(0, 5).forEach(show => {
                                const posterUrl = show.poster_path 
                                    ? `https://image.tmdb.org/t/p/w500${show.poster_path}` 
                                    : "/static/images/no-poster.svg";
                                
                                const showCard = document.createElement("div");
                                showCard.className = "tv-card flex-shrink-0 w-32 glass-effect rounded-lg overflow-hidden";
                                showCard.innerHTML = `
                                    <a href="/tv/${show.id}" class="block">
                                        <img src="${posterUrl}" alt="${show.name}" class="w-full h-48 object-cover">
                                        <div class="p-2">
                                            <h3 class="font-semibold text-xs truncate">${show.name}</h3>
                                            <p class="text-gray-400 text-xs">${show.first_air_date}</p>
                                        </div>
                                    </a>
                                `;
                                container.appendChild(showCard);
                            });
                        } else {
                            // If no results, show a message
                            container.innerHTML = '<p class="text-gray-400 text-center">No shows found</p>';
                        }
                    }
                } catch (error) {
                    
                    // Show error message in the container
                    const container = document.getElementById(`${genre}-tv`);
                    if (container) {
                        container.innerHTML = '<p class="text-red-400 text-center">Error loading shows</p>';
                    }
                }
            }
        }
