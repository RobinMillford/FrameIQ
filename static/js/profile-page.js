/**
 * Profile page behaviors (redesign).
 * Requires window.__PROFILE_BOOTSTRAP__ = { userId } rendered by the template.
 *
 * Responsibilities: tab switching (aria-selected, keyboard arrows),
 * recommendations preview (one fetch, safe DOM rendering), lists/diary
 * counts, and the Create List modal. Dropdown behavior lives in the
 * markup + shared chrome; no duplicate wiring here.
 */
const BOOT = window.__PROFILE_BOOTSTRAP__ || {};

document.addEventListener('DOMContentLoaded', function () {
    // Load recommendations preview
    loadRecommendationsPreview();

    // Tab switching logic (aria-selected drives styling via CSS)
    const tabOverview = document.getElementById('tab-overview');
    const tabReviews = document.getElementById('tab-reviews');
    const tabInsights = document.getElementById('tab-insights');
    const overviewContent = document.getElementById('overview-content');
    const reviewsContent = document.getElementById('reviews-content');
    const insightsContent = document.getElementById('insights-content');
    const tabs = [tabOverview, tabReviews, tabInsights].filter(Boolean);
    const contents = { tab_overview: overviewContent, tab_reviews: reviewsContent, tab_insights: insightsContent };

    function switchTab(activeTab) {
        tabs.forEach(tab => {
            const isActive = tab === activeTab;
            tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        tabs.forEach(tab => {
            const content = contents['tab_' + tab.id.replace('tab-', '')];
            if (content) content.classList.toggle('hidden', tab !== activeTab);
        });

        if (activeTab === tabInsights && !window.statsDashboard) {
            // Initialize dashboard on first open (lazy; Chart.js is loaded
            // by the template). Guarded so a failure never blocks the tab.
            try {
                window.statsDashboard = new StatsDashboard(BOOT.userId);
            } catch (err) {
                /* dashboard is optional presentation */
            }
        }
    }

    tabs.forEach((tab, i) => {
        tab.addEventListener('click', () => switchTab(tab));
        tab.addEventListener('keydown', (e) => {
            if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
            e.preventDefault();
            const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
            next.focus();
            switchTab(next);
        });
    });
});

function loadRecommendationsPreview() {
    fetch('/profile/recommendations-preview')
        .then(response => response.json())
        .then(data => {
            const container = document.getElementById('recommendations-preview');
            if (!container) return;
            container.innerHTML = '';
            if (data.recommendations && data.recommendations.length > 0) {
                const rail = document.createElement('div');
                rail.className = 'rec-rail';
                data.recommendations.forEach(rec => {
                    const mediaPath = rec.media_type === 'movie' ? 'movie' : 'tv';
                    const a = document.createElement('a');
                    a.href = `/${mediaPath}/${rec.id}`;
                    a.className = 'rec-card block focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent-hi)] rounded-lg';

                    const img = document.createElement('img');
                    img.src = rec.poster || '';
                    img.alt = rec.title || '';
                    img.className = 'rec-poster mb-2';
                    img.onerror = function () { this.src = '/static/images/no-poster.svg'; };

                    const titleEl = document.createElement('div');
                    titleEl.className = 'text-sm font-semibold text-[var(--text-hi)] truncate';
                    titleEl.textContent = rec.title || '';

                    const dateEl = document.createElement('div');
                    dateEl.className = 'text-xs text-[var(--text-mid)] mt-0.5';
                    dateEl.textContent = rec.release_date || '';

                    // Recommendation reason — readable, never tiny (§4-E)
                    const basedEl = document.createElement('div');
                    basedEl.className = 'rec-reason mt-1';
                    basedEl.title = rec.based_on ? `Because you watched ${rec.based_on}` : '';
                    basedEl.textContent = rec.based_on ? `Because you watched ${rec.based_on}` : '';

                    a.appendChild(img);
                    a.appendChild(titleEl);
                    a.appendChild(dateEl);
                    a.appendChild(basedEl);
                    rail.appendChild(a);
                });
                container.appendChild(rail);
            } else {
                const p = document.createElement('p');
                p.className = 'meta-text';
                p.textContent = 'Add items to your lists to get personalized recommendations.';
                container.appendChild(p);
            }
        })
        .catch(() => {
            const container = document.getElementById('recommendations-preview');
            if (container) {
                container.innerHTML = '';
                const p = document.createElement('p');
                p.className = 'meta-text';
                p.textContent = 'Unable to load recommendations at this time.';
                container.appendChild(p);
            }
        });
}

// Load lists and diary counts
async function loadListsAndDiaryCounts() {
    const listsCount = document.getElementById('lists-count');
    const diaryCount = document.getElementById('diary-count');
    const headerLists = document.getElementById('header-lists-count');
    try {
        // Load lists count
        const listsResponse = await fetch(`/api/users/${BOOT.userId}/lists`);
        const listsData = await listsResponse.json();
        const lists = `${listsData.count || 0} lists`;
        if (listsCount) listsCount.textContent = lists;
        if (headerLists) headerLists.textContent = String(listsData.count || 0);

        // Load diary count
        const diaryResponse = await fetch(`/api/diary?per_page=1`);
        const diaryData = await diaryResponse.json();
        if (diaryCount) diaryCount.textContent = `${diaryData.total || 0} entries`;
    } catch (error) {
        if (listsCount) listsCount.textContent = '0 lists';
        if (diaryCount) diaryCount.textContent = '0 entries';
    }
}

// Create List Modal Functions
function openCreateListModal() {
    document.getElementById('create-list-modal').classList.remove('hidden');
    document.getElementById('list-title').focus();
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
            window.frameToast?.('List created successfully', 'success');
            closeCreateListModal();
            loadListsAndDiaryCounts(); // Refresh count
        } else {
            window.frameToast?.(data.error || 'Failed to create list', 'error');
        }
    } catch (error) {
        window.frameToast?.('An error occurred', 'error');
    }
}

// Load counts on page load
loadListsAndDiaryCounts();
