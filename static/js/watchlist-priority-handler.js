// Handle priority changes for watchlist and wishlist pages
document.addEventListener('DOMContentLoaded', function() {
    // Get all priority selects
    const prioritySelects = document.querySelectorAll('.priority-select');
    
    prioritySelects.forEach(select => {
        select.addEventListener('change', async function() {
            const mediaId = this.dataset.mediaId;
            const mediaType = this.dataset.mediaType;
            const newPriority = this.value;
            
            // Determine which list we're on
            const listType = document.body.classList.contains('watchlist-page') ? 'watchlist' : 'wishlist';
            
            // Show loading state
            this.disabled = true;
            this.style.opacity = '0.5';
            
            try {
                const response = await fetch(`/api/update_priority/${listType}/${mediaId}/${mediaType}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ priority: newPriority })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    // Update the card's data attribute
                    const card = this.closest('.media-card');
                    if (card) {
                        card.dataset.priority = newPriority;
                    }
                    
                    // Show success feedback
                    const emoji = {
                        'high': '🔥',
                        'medium': '📌',
                        'low': '💤'
                    }[newPriority];
                    
                    // Create temporary success message
                    const message = document.createElement('div');
                    message.className = 'fixed top-4 right-4 bg-green-600 text-white px-4 py-2 rounded-lg shadow-lg z-50 transition-opacity duration-300';
                    message.textContent = `${emoji} Priority updated!`;
                    document.body.appendChild(message);
                    
                    setTimeout(() => {
                        message.style.opacity = '0';
                        setTimeout(() => message.remove(), 300);
                    }, 2000);
                } else {
                    throw new Error(data.error || 'Failed to update priority');
                }
            } catch (error) {
                console.error('Error updating priority:', error);
                
                // Show error message
                const message = document.createElement('div');
                message.className = 'fixed top-4 right-4 bg-red-600 text-white px-4 py-2 rounded-lg shadow-lg z-50';
                message.textContent = '❌ Failed to update priority';
                document.body.appendChild(message);
                
                setTimeout(() => message.remove(), 3000);
                
                // Revert selection
                const card = this.closest('.media-card');
                if (card) {
                    this.value = card.dataset.priority || 'medium';
                }
            } finally {
                // Remove loading state
                this.disabled = false;
                this.style.opacity = '1';
            }
        });
    });
    
    // Priority filtering
    const priorityFilterButtons = document.querySelectorAll('.priority-filter-btn');
    
    priorityFilterButtons.forEach(button => {
        button.addEventListener('click', function() {
            const priority = this.dataset.priority;
            
            // Update active button
            priorityFilterButtons.forEach(btn => btn.classList.remove('active'));
            this.classList.add('active');
            
            // Filter cards
            const cards = document.querySelectorAll('.media-card');
            cards.forEach(card => {
                if (priority === 'all' || card.dataset.priority === priority) {
                    card.style.display = '';
                } else {
                    card.style.display = 'none';
                }
            });
        });
    });
    
    // Sorting
    const sortSelect = document.getElementById('watchlist-sort');
    if (sortSelect) {
        sortSelect.addEventListener('change', function() {
            const sortBy = this.value;
            const container = document.querySelector('.watchlist-grid');
            const cards = Array.from(container.querySelectorAll('.media-card'));
            
            if (sortBy === 'priority') {
                const priorityOrder = { 'high': 1, 'medium': 2, 'low': 3 };
                cards.sort((a, b) => {
                    const priorityA = priorityOrder[a.dataset.priority] || 4;
                    const priorityB = priorityOrder[b.dataset.priority] || 4;
                    return priorityA - priorityB;
                });
            } else {
                // Sort by date added (default order from server)
                cards.sort((a, b) => {
                    const indexA = parseInt(a.dataset.originalIndex) || 0;
                    const indexB = parseInt(b.dataset.originalIndex) || 0;
                    return indexA - indexB;
                });
            }
            
            // Re-append cards in new order
            cards.forEach(card => container.appendChild(card));
        });
    }
    
    // Store original order for date sorting
    const cards = document.querySelectorAll('.media-card');
    cards.forEach((card, index) => {
        card.dataset.originalIndex = index;
    });
});
