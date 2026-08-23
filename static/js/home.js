/**
 * Home page behaviors: profile dropdown, mobile menu, chat button,
 * hero backdrop rotation, search autocomplete.
 *
 * Requires window.__HOME_BOOTSTRAP__ = { mobileAuthLinks, backdropUrls }
 * rendered inline by the template.
 */
document.addEventListener('DOMContentLoaded', function() {
    const BOOT = window.__HOME_BOOTSTRAP__ || {};

    // Profile dropdown
    const profileButton = document.getElementById('profile-button');
    const profileMenu = document.getElementById('profile-menu');

    if (profileButton && profileMenu) {
        profileButton.addEventListener('click', function(e) {
            e.stopPropagation();
            profileMenu.classList.toggle('hidden');
        });

        // Close dropdown when clicking outside
        document.addEventListener('click', function(e) {
            if (!profileButton.contains(e.target) && !profileMenu.contains(e.target)) {
                profileMenu.classList.add('hidden');
            }
        });

        // Close dropdown when pressing Escape key
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                profileMenu.classList.add('hidden');
            }
        });
    }

    // Mobile menu
    const mobileMenuButton = document.getElementById('mobile-menu-button');
    const mobileMenu = document.createElement('div');
    mobileMenu.className = 'fixed left-0 right-0 bg-[#1A4A4F] glass-effect hidden overflow-y-auto';
    mobileMenu.style.zIndex = '49';
    mobileMenu.style.maxHeight = '80vh';
    mobileMenu.innerHTML = `
        <div class="container mx-auto px-4 py-4">
            <div class="flex flex-col space-y-4">
                <a href="/" class="text-[#F5F6F5] hover:text-[#00C4CC]">Home</a>
                <a href="/movies" class="text-[#F5F6F5] hover:text-[#00C4CC]">Movies</a>
                <a href="/tv_shows" class="text-[#F5F6F5] hover:text-[#00C4CC]">TV Shows</a>
                <a href="/news" class="text-[#F5F6F5] hover:text-[#00C4CC]">News</a>
                <a href="/feed" class="text-[#F5F6F5] hover:text-[#00C4CC]">Social Feed</a>
                <a href="/discover" class="text-[#F5F6F5] hover:text-[#00C4CC]">Discover</a>
                <a href="/trending" class="text-[#F5F6F5] hover:text-[#00C4CC]">Trending</a>
                <a href="/stats" class="text-[#F5F6F5] hover:text-[#00C4CC]">Stats</a>
                <a href="/feed/enhanced" class="text-[#F5F6F5] hover:text-[#00C4CC]">Enhanced Feed</a>
                <a href="/chat" class="text-[#F5F6F5] hover:text-[#00C4CC] flex items-center">
                    <svg xmlns="http://www.w3.org/2000/svg" class="h-5 w-5 mr-1" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
                    </svg>
                    AI Chat
                </a>
                ${BOOT.mobileAuthLinks || ''}
            </div>
        </div>
    `;
    document.body.appendChild(mobileMenu);

    mobileMenuButton.addEventListener('click', () => {
        const nav = document.querySelector('nav');
        mobileMenu.style.top = nav.offsetHeight + 'px';
        mobileMenu.classList.toggle('hidden');
    });

    // Add floating chat button
    const chatButton = document.createElement('a');
    chatButton.href = '/chat';
    chatButton.id = 'chatbot-button';
    chatButton.className = 'fixed bottom-6 right-6 w-14 h-14 rounded-full flex items-center justify-center text-white shadow-lg z-40 transition-all duration-300 hover:scale-110';
    chatButton.innerHTML = `
        <svg xmlns="http://www.w3.org/2000/svg" class="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
        </svg>
    `;
    document.body.appendChild(chatButton);

    // Trending backdrops rotation (URLs provided via bootstrap)
    let currentBackdropIndex = 0;
    const heroBackdrop = document.getElementById('dynamic-hero-backdrop');
    const backdropUrls = (BOOT.backdropUrls || []).filter(
        url => url && url !== "None" && url !== "");

    function rotateBackdropImage() {
        if (backdropUrls.length > 1) {
            currentBackdropIndex = (currentBackdropIndex + 1) % backdropUrls.length;
            heroBackdrop.style.backgroundImage = `url('${backdropUrls[currentBackdropIndex]}')`;
        }
    }

    if (backdropUrls.length > 1) {
        setInterval(rotateBackdropImage, 8000);
    }

    // Autocomplete
    const searchInput = document.getElementById('search-input');
    const autocompleteItems = document.getElementById('autocomplete-items');
    let autocompleteTimer = null;

    function positionDropdown() {
        const rect = searchInput.closest('form').getBoundingClientRect();
        autocompleteItems.style.top = (rect.bottom + 8) + 'px';
        autocompleteItems.style.left = rect.left + 'px';
        autocompleteItems.style.width = rect.width + 'px';
    }

    window.addEventListener('resize', () => {
        if (!autocompleteItems.classList.contains('hidden')) positionDropdown();
    });

    searchInput.addEventListener('input', function() {
        clearTimeout(autocompleteTimer);
        autocompleteTimer = setTimeout(async () => {
        const query = this.value.trim();
        if (query.length < 2) {
            autocompleteItems.classList.add('hidden');
            return;
        }

        try {
            const response = await fetch(`/autocomplete?query=${encodeURIComponent(query)}`);
            const data = await response.json();

            if (data.movies.length === 0 && data.shows.length === 0 && data.people.length === 0) {
                autocompleteItems.classList.add('hidden');
                return;
            }

            autocompleteItems.innerHTML = '';

            function buildACItem(posterPath, title, subtitle, href) {
                const item = document.createElement('div');
                item.className = 'p-2 flex items-center gap-2 hover:bg-indigo-900/50 cursor-pointer';
                const img = document.createElement('img');
                img.src = posterPath ? `https://image.tmdb.org/t/p/w92${posterPath}` : '/static/images/no-poster.svg';
                img.alt = title || '';
                img.className = 'w-10 h-14 object-cover rounded';
                const info = document.createElement('div');
                info.className = 'min-w-0 flex-1 overflow-hidden';
                const titleEl = document.createElement('p');
                titleEl.className = 'text-[#F5F6F5] text-sm truncate';
                titleEl.textContent = title || '';
                const subEl = document.createElement('p');
                subEl.className = 'text-[#A9B8B5] text-xs truncate';
                subEl.textContent = subtitle || 'N/A';
                info.appendChild(titleEl);
                info.appendChild(subEl);
                item.appendChild(img);
                item.appendChild(info);
                item.addEventListener('click', () => { window.location.href = href; });
                return item;
            }

            // Movies
            data.movies.forEach(movie => {
                autocompleteItems.appendChild(
                    buildACItem(movie.poster_path, movie.title, movie.release_date, `/movie/${movie.id}`)
                );
            });

            // TV Shows
            data.shows.forEach(show => {
                autocompleteItems.appendChild(
                    buildACItem(show.poster_path, show.name, show.first_air_date, `/tv/${show.id}`)
                );
            });

            // People
            data.people.forEach(person => {
                autocompleteItems.appendChild(
                    buildACItem(person.profile_path, person.name, person.known_for, `/actor/${person.id}`)
                );
            });

            if (autocompleteItems.children.length > 0) {
                positionDropdown();
                autocompleteItems.classList.remove('hidden');
            } else {
                autocompleteItems.classList.add('hidden');
            }
        } catch (error) {
            autocompleteItems.classList.add('hidden');
        }
        }, 300);
    });

    // Hide autocomplete when clicking outside
    document.addEventListener('click', (e) => {
        if (!searchInput.contains(e.target) && !autocompleteItems.contains(e.target)) {
            autocompleteItems.classList.add('hidden');
        }
    });
});
