/**
 * Week 2b: List Collaboration & Categories Manager
 * Handles collaborators, categories, and analytics
 */
class ListAdvancedManager {
    constructor(listId) {
        this.listId = listId;
        this.init();
    }
    
    init() {
        this.setupCollaboratorUI();
        this.setupCategoryUI();
        this.setupAnalytics();
    }
    
    // ============================================================================
    // COLLABORATOR MANAGEMENT
    // ============================================================================
    
    setupCollaboratorUI() {
        const addCollabBtn = document.getElementById('add-collaborator-btn');
        if (addCollabBtn) {
            addCollabBtn.addEventListener('click', () => this.openAddCollaboratorModal());
        }
        
        this.loadCollaborators();
    }
    
    async loadCollaborators() {
        const container = document.getElementById('collaborators-list');
        if (!container) return;
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/collaborators`);
            const data = await response.json();
            
            if (response.ok && data.collaborators.length > 0) {
                container.innerHTML = '';
                data.collaborators.forEach(collab => {
                    container.appendChild(this.createCollaboratorCard(collab));
                });
            } else {
                container.innerHTML = '<p class="text-gray-500 text-sm">No collaborators yet</p>';
            }
        } catch (error) {
            console.error('Error loading collaborators:', error);
        }
    }
    
    createCollaboratorCard(collab) {
        const div = document.createElement('div');
        div.className = 'flex items-center justify-between p-3 bg-gray-700 rounded-lg';
        
        const roleIcon = collab.role === 'editor' ? 'fa-pen' : 'fa-eye';
        const roleColor = collab.role === 'editor' ? 'text-green-400' : 'text-blue-400';
        
        div.innerHTML = `
            <div class="flex items-center gap-3">
                <img src="${collab.user.profile_picture || '/static/images/default-avatar.png'}" 
                     alt="${collab.user.username}" 
                     class="w-10 h-10 rounded-full">
                <div>
                    <p class="font-semibold text-white">${collab.user.username}</p>
                    <p class="text-xs ${roleColor}">
                        <i class="fas ${roleIcon}"></i> ${collab.role}
                    </p>
                </div>
            </div>
            <div class="flex gap-2">
                <button onclick="listAdvancedManager.changeCollaboratorRole(${collab.user.id})" 
                        class="text-sm text-indigo-400 hover:text-indigo-300">
                    <i class="fas fa-user-edit"></i>
                </button>
                <button onclick="listAdvancedManager.removeCollaborator(${collab.user.id}, '${collab.user.username}')" 
                        class="text-sm text-red-400 hover:text-red-300">
                    <i class="fas fa-times"></i>
                </button>
            </div>
        `;
        
        return div;
    }
    
    openAddCollaboratorModal() {
        const modal = document.getElementById('add-collaborator-modal');
        if (modal) {
            modal.classList.remove('hidden');
        }
    }
    
    closeAddCollaboratorModal() {
        const modal = document.getElementById('add-collaborator-modal');
        if (modal) {
            modal.classList.add('hidden');
            document.getElementById('collaborator-username').value = '';
            document.getElementById('collaborator-role').value = 'editor';
        }
    }
    
    async addCollaborator() {
        const username = document.getElementById('collaborator-username').value.trim();
        const role = document.getElementById('collaborator-role').value;
        
        if (!username) {
            this.showMessage('Please enter a username', 'error');
            return;
        }
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/collaborators`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ username, role })
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Added ${username} as ${role}`, 'success');
                this.closeAddCollaboratorModal();
                this.loadCollaborators();
            } else {
                this.showMessage(data.error || 'Failed to add collaborator', 'error');
            }
        } catch (error) {
            console.error('Error adding collaborator:', error);
            this.showMessage('Failed to add collaborator', 'error');
        }
    }
    
    async removeCollaborator(userId, username) {
        if (!confirm(`Remove ${username} from collaborators?`)) return;
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/collaborators/${userId}`, {
                method: 'DELETE'
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Removed ${username}`, 'success');
                this.loadCollaborators();
            } else {
                this.showMessage(data.error || 'Failed to remove collaborator', 'error');
            }
        } catch (error) {
            console.error('Error removing collaborator:', error);
            this.showMessage('Failed to remove collaborator', 'error');
        }
    }
    
    async changeCollaboratorRole(userId) {
        const newRole = prompt('Enter new role (editor or viewer):');
        if (!newRole || !['editor', 'viewer'].includes(newRole.toLowerCase())) {
            this.showMessage('Invalid role. Must be "editor" or "viewer"', 'error');
            return;
        }
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/collaborators/${userId}/role`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ role: newRole.toLowerCase() })
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Updated role to ${newRole}`, 'success');
                this.loadCollaborators();
            } else {
                this.showMessage(data.error || 'Failed to update role', 'error');
            }
        } catch (error) {
            console.error('Error updating role:', error);
            this.showMessage('Failed to update role', 'error');
        }
    }
    
    // ============================================================================
    // CATEGORY MANAGEMENT
    // ============================================================================
    
    setupCategoryUI() {
        const addCategoryBtn = document.getElementById('add-category-btn');
        if (addCategoryBtn) {
            addCategoryBtn.addEventListener('click', () => this.openCategoryModal());
        }
        
        this.loadCategories();
        this.loadAvailableCategories();
    }
    
    async loadCategories() {
        const container = document.getElementById('list-categories');
        if (!container) return;
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/categories`);
            const data = await response.json();
            
            if (response.ok && data.categories.length > 0) {
                container.innerHTML = '';
                data.categories.forEach(cat => {
                    container.appendChild(this.createCategoryBadge(cat, true));
                });
            } else {
                container.innerHTML = '<span class="text-gray-500 text-sm">No categories</span>';
            }
        } catch (error) {
            console.error('Error loading categories:', error);
        }
    }
    
    async loadAvailableCategories() {
        try {
            const response = await fetch('/api/categories');
            const data = await response.json();
            
            if (response.ok) {
                this.availableCategories = data.categories;
            }
        } catch (error) {
            console.error('Error loading available categories:', error);
        }
    }
    
    createCategoryBadge(category, removable = false) {
        const span = document.createElement('span');
        span.className = 'inline-flex items-center gap-2 px-3 py-1 rounded-full text-sm font-semibold';
        span.style.backgroundColor = category.color || '#6366f1';
        span.style.color = '#ffffff';
        
        span.innerHTML = `
            <i class="fas ${category.icon || 'fa-tag'}"></i>
            <span>${category.name}</span>
            ${removable ? `
                <button onclick="listAdvancedManager.removeCategory(${category.id}, '${category.name}')" 
                        class="ml-1 hover:text-red-200">
                    <i class="fas fa-times text-xs"></i>
                </button>
            ` : ''}
        `;
        
        return span;
    }
    
    openCategoryModal() {
        const modal = document.getElementById('category-modal');
        if (!modal) return;
        
        // Populate category grid
        const grid = document.getElementById('category-grid');
        if (grid && this.availableCategories) {
            grid.innerHTML = '';
            this.availableCategories.forEach(cat => {
                const btn = document.createElement('button');
                btn.className = 'p-4 bg-gray-700 rounded-lg hover:bg-gray-600 transition-colors text-left';
                btn.onclick = () => this.addCategory(cat.id, cat.name);
                
                btn.innerHTML = `
                    <div class="flex items-center gap-3 mb-2">
                        <i class="fas ${cat.icon || 'fa-tag'} text-2xl" style="color: ${cat.color}"></i>
                        <span class="font-semibold text-white">${cat.name}</span>
                    </div>
                    <p class="text-xs text-gray-400">${cat.description || ''}</p>
                    <p class="text-xs text-gray-500 mt-2">
                        <i class="fas fa-list"></i> ${cat.usage_count} lists
                    </p>
                `;
                
                grid.appendChild(btn);
            });
        }
        
        modal.classList.remove('hidden');
    }
    
    closeCategoryModal() {
        const modal = document.getElementById('category-modal');
        if (modal) {
            modal.classList.add('hidden');
        }
    }
    
    async addCategory(categoryId, categoryName) {
        try {
            const response = await fetch(`/api/lists/${this.listId}/categories`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ category_id: categoryId })
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Added category: ${categoryName}`, 'success');
                this.closeCategoryModal();
                this.loadCategories();
            } else {
                this.showMessage(data.error || 'Failed to add category', 'error');
            }
        } catch (error) {
            console.error('Error adding category:', error);
            this.showMessage('Failed to add category', 'error');
        }
    }
    
    async removeCategory(categoryId, categoryName) {
        try {
            const response = await fetch(`/api/lists/${this.listId}/categories/${categoryId}`, {
                method: 'DELETE'
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Removed category: ${categoryName}`, 'success');
                this.loadCategories();
            } else {
                this.showMessage(data.error || 'Failed to remove category', 'error');
            }
        } catch (error) {
            console.error('Error removing category:', error);
            this.showMessage('Failed to remove category', 'error');
        }
    }
    
    // ============================================================================
    // ANALYTICS
    // ============================================================================
    
    setupAnalytics() {
        // Track view when page loads
        this.trackView();
        
        // Load analytics if user is owner/collaborator
        this.loadAnalytics();
    }
    
    async trackView() {
        try {
            await fetch(`/api/lists/${this.listId}/view`, {
                method: 'POST'
            });
        } catch (error) {
            console.error('Error tracking view:', error);
        }
    }
    
    async loadAnalytics() {
        const container = document.getElementById('analytics-panel');
        if (!container) return;
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/analytics`);
            
            if (response.ok) {
                const data = await response.json();
                this.displayAnalytics(data);
            }
        } catch (error) {
            // User probably doesn't have permission to view analytics
            console.log('Analytics not available');
        }
    }
    
    displayAnalytics(data) {
        const container = document.getElementById('analytics-panel');
        if (!container) return;
        
        const analytics = data.analytics;
        
        container.innerHTML = `
            <div class="bg-gray-800 rounded-lg p-6 border border-gray-700">
                <h3 class="text-xl font-bold text-indigo-400 mb-4">
                    <i class="fas fa-chart-line"></i> List Analytics
                </h3>
                
                <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
                    <div class="text-center">
                        <p class="text-3xl font-bold text-white">${analytics.view_count}</p>
                        <p class="text-sm text-gray-400">Total Views</p>
                    </div>
                    <div class="text-center">
                        <p class="text-3xl font-bold text-white">${analytics.unique_viewers}</p>
                        <p class="text-sm text-gray-400">Unique Viewers</p>
                    </div>
                    <div class="text-center">
                        <p class="text-3xl font-bold text-white">${analytics.share_count}</p>
                        <p class="text-sm text-gray-400">Shares</p>
                    </div>
                    <div class="text-center">
                        <p class="text-3xl font-bold text-white">${analytics.fork_count}</p>
                        <p class="text-sm text-gray-400">Forks</p>
                    </div>
                </div>
                
                ${data.recent_viewers && data.recent_viewers.length > 0 ? `
                    <div>
                        <h4 class="font-semibold text-gray-300 mb-3">Recent Viewers</h4>
                        <div class="space-y-2">
                            ${data.recent_viewers.slice(0, 5).map(viewer => `
                                <div class="flex items-center justify-between text-sm">
                                    <span class="text-gray-400">${viewer.username}</span>
                                    <span class="text-gray-500">${new Date(viewer.viewed_at).toLocaleString()}</span>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                ` : ''}
            </div>
        `;
    }
    
    async trackShare() {
        try {
            const response = await fetch(`/api/lists/${this.listId}/share`, {
                method: 'POST'
            });
            
            if (response.ok) {
                this.loadAnalytics(); // Refresh analytics
            }
        } catch (error) {
            console.error('Error tracking share:', error);
        }
    }
    
    // ============================================================================
    // UI HELPERS
    // ============================================================================
    
    showMessage(message, type = 'info') {
        const alertClass = type === 'success' ? 'bg-green-500' : 
                          type === 'error' ? 'bg-red-500' : 'bg-blue-500';
        
        const alert = document.createElement('div');
        alert.className = `fixed top-4 right-4 ${alertClass} text-white px-6 py-3 rounded-lg shadow-lg z-50 transition-all transform`;
        alert.innerHTML = `
            <div class="flex items-center gap-3">
                <i class="fas fa-${type === 'success' ? 'check-circle' : type === 'error' ? 'exclamation-circle' : 'info-circle'}"></i>
                <span>${message}</span>
            </div>
        `;
        
        document.body.appendChild(alert);
        
        setTimeout(() => alert.classList.add('scale-100'), 10);
        setTimeout(() => {
            alert.classList.add('opacity-0');
            setTimeout(() => alert.remove(), 300);
        }, 3000);
    }
}

// Auto-initialize
document.addEventListener('DOMContentLoaded', () => {
    const listIdElement = document.querySelector('[data-list-id]');
    if (listIdElement) {
        const listId = listIdElement.dataset.listId;
        window.listAdvancedManager = new ListAdvancedManager(listId);
    }
});
