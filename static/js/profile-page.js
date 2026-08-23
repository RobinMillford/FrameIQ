/**
 * Profile page behaviors.
 * Requires window.__PROFILE_BOOTSTRAP__ = { userId } rendered by the template.
 */
const BOOT = window.__PROFILE_BOOTSTRAP__ || {};
        document.addEventListener('DOMContentLoaded', function() {
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
            
            // Load recommendations preview
            loadRecommendationsPreview();

            // Tab switching logic
            const tabOverview = document.getElementById('tab-overview');
            const tabReviews = document.getElementById('tab-reviews');
            const tabInsights = document.getElementById('tab-insights');
            const overviewContent = document.getElementById('overview-content');
            const reviewsContent = document.getElementById('reviews-content');
            const insightsContent = document.getElementById('insights-content');
            
            function switchTab(activeTab, activeContent) {
                [tabOverview, tabReviews, tabInsights].forEach(tab => {
                    if (tab) {
                        tab.classList.remove('border-indigo-500', 'text-indigo-400');
                        tab.classList.add('border-transparent', 'text-gray-400');
                    }
                });
                [overviewContent, reviewsContent, insightsContent].forEach(content => {
                    if (content) content.classList.add('hidden');
                });
                
                if (activeTab) {
                    activeTab.classList.add('border-indigo-500', 'text-indigo-400');
                    activeTab.classList.remove('border-transparent', 'text-gray-400');
                }
                if (activeContent) activeContent.classList.remove('hidden');
            }
            
            if (tabOverview) {
                tabOverview.addEventListener('click', () => switchTab(tabOverview, overviewContent));
            }
            
            if (tabReviews) {
                tabReviews.addEventListener('click', () => switchTab(tabReviews, reviewsContent));
            }
                
            if (tabInsights) {
                tabInsights.addEventListener('click', () => {
                    switchTab(tabInsights, insightsContent);
                    // Initialize dashboard if not already done
                    if (!window.statsDashboard) {
                        window.statsDashboard = new StatsDashboard(BOOT.userId);
                    }
                });
            }
        });
        
        function loadRecommendationsPreview() {
            fetch('/profile/recommendations-preview')
                .then(response => response.json())
                .then(data => {
                    const container = document.getElementById('recommendations-preview');
                    container.innerHTML = '';
                    if (data.recommendations && data.recommendations.length > 0) {
                        const grid = document.createElement('div');
                        grid.className = 'grid grid-cols-2 sm:grid-cols-3 gap-4';
                        data.recommendations.forEach(rec => {
                            const mediaPath = rec.media_type === 'movie' ? 'movie' : 'tv';
                            const a = document.createElement('a');
                            a.href = `/${mediaPath}/${rec.id}`;
                            a.className = 'recommendation-card block';
                            const img = document.createElement('img');
                            img.src = rec.poster || '';
                            img.alt = rec.title || '';
                            img.className = 'w-full rounded mb-2';
                            img.style.cssText = 'aspect-ratio:2/3;object-fit:cover';
                            img.onerror = function() { this.src = '/static/images/no-poster.svg'; };
                            const titleEl = document.createElement('div');
                            titleEl.className = 'text-xs font-semibold truncate';
                            titleEl.textContent = rec.title || '';
                            const dateEl = document.createElement('div');
                            dateEl.className = 'text-xs text-gray-400';
                            dateEl.textContent = rec.release_date || '';
                            const basedEl = document.createElement('div');
                            basedEl.className = 'text-xs text-indigo-300 truncate';
                            basedEl.title = `Based on: ${rec.based_on || ''}`;
                            basedEl.textContent = `Based on: ${rec.based_on || ''}`;
                            a.appendChild(img);
                            a.appendChild(titleEl);
                            a.appendChild(dateEl);
                            a.appendChild(basedEl);
                            grid.appendChild(a);
                        });
                        container.appendChild(grid);
                    } else {
                        const p = document.createElement('p');
                        p.className = 'text-gray-400';
                        p.textContent = 'Add items to your lists to get personalized recommendations.';
                        container.appendChild(p);
                    }
                })
                .catch(() => {
                    const container = document.getElementById('recommendations-preview');
                    if (container) {
                        container.innerHTML = '';
                        const p = document.createElement('p');
                        p.className = 'text-gray-400';
                        p.textContent = 'Unable to load recommendations at this time.';
                        container.appendChild(p);
                    }
                });
        }
        
        // Load lists and diary counts
        async function loadListsAndDiaryCounts() {
            try {
                // Load lists count
                const listsResponse = await fetch(`/api/users/BOOT.userId/lists`);
                const listsData = await listsResponse.json();
                document.getElementById('lists-count').textContent = `${listsData.count || 0} lists`;
                
                // Load diary count
                const diaryResponse = await fetch(`/api/diary?per_page=1`);
                const diaryData = await diaryResponse.json();
                document.getElementById('diary-count').textContent = `${diaryData.total || 0} entries`;
            } catch (error) {
                document.getElementById('lists-count').textContent = '0 lists';
                document.getElementById('diary-count').textContent = '0 entries';
            }
        }
        
        // Create List Modal Functions
        function openCreateListModal() {
            document.getElementById('create-list-modal').classList.remove('hidden');
        }
        
        function closeCreateListModal() {
            document.getElementById('create-list-modal').classList.add('hidden');
            document.getElementById('list-title').value = '';
            document.getElementById('list-description').value = '';
            document.getElementById('list-public').checked = true;
        }
        
        async function createList() {
            const title = document.getElementById('list-title').value;
            const description = document.getElementById('list-description').value;
            const isPublic = document.getElementById('list-public').checked;
            
            if (!title.trim()) {
                alert('Please enter a list title');
                return;
            }
            
            try {
                const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
                const response = await fetch('/api/lists/create', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                    body: JSON.stringify({
                        title: title,
                        description: description,
                        is_public: isPublic
                    })
                });
                
                const data = await response.json();
                if (response.ok) {
                    alert('List created successfully!');
                    closeCreateListModal();
                    loadListsAndDiaryCounts(); // Refresh count
                } else {
                    alert(data.error || 'Failed to create list');
                }
            } catch (error) {
                alert('An error occurred');
            }
        }
        
        // Load counts on page load
        loadListsAndDiaryCounts();
