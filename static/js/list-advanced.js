/**
 * Week 2b: List Collaboration & Categories Manager
 * Handles collaborators, categories, and analytics
 */
class ListAdvancedManager {
    csrfHeader() {
        return {
            'Content-Type': 'application/json',
            'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
        };
    }

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
        const D = window.FrameIQDOM;
        const container = document.getElementById('collaborators-list');
        if (!container) return;

        try {
            const response = await fetch(`/api/lists/${this.listId}/collaborators`);
            const data = await response.json();
            
            if (response.ok && data.collaborators.length > 0) {
                D.clear(container);
                data.collaborators.forEach(collab => {
                    container.appendChild(this.createCollaboratorCard(collab));
                });
            } else if (response.ok) {
                D.clear(container);
                container.appendChild(D.el('p', {
                    class: 'text-gray-500 text-sm',
                    text: 'No collaborators yet',
                }));
            } else {
                // F23 (C-11): the collaborator roster is now authenticated, so
                // a 401/403 means "you may not see this", which is NOT the same
                // claim as "this list has no collaborators". Saying the latter
                // would misinform whoever can edit the list.
                D.clear(container);
                container.appendChild(D.el('p', {
                    class: 'text-gray-500 text-sm',
                    text: (response.status === 401 || response.status === 403)
                        ? 'Sign in and be a collaborator to view this list.'
                        : (data.error || 'Could not load collaborators.'),
                }));
            }
        } catch (error) {
            // Never leave the panel claiming an empty roster after a failure.
            const container = document.getElementById('collaborators-list');
            if (container) {
                D.clear(container);
                container.appendChild(D.el('p', {
                    class: 'text-gray-500 text-sm',
                    text: 'Could not load collaborators.',
                }));
            }
        }
    }
    
    createCollaboratorCard(collab) {
        const D = window.FrameIQDOM;
        const div = D.el('div', { class: 'flex items-center justify-between p-3 bg-gray-700 rounded-lg' });

        const roleColor = collab.role === 'editor' ? 'text-green-400' : 'text-blue-400';

        // F21 — the username is user-supplied free text (registration applies
        // no charset rule). It is placed with textContent and is NEVER
        // interpolated into an attribute or a handler string.
        const username = D.text(collab.user && collab.user.username);

        const left = D.el('div', { class: 'flex items-center gap-3' });
        left.appendChild(D.el('img', {
            src: D.safeImageUrl(
                collab.user && collab.user.profile_picture,
                '/static/images/default-avatar.png',
                '/static/'),
            alt: username,
            class: 'w-10 h-10 rounded-full',
        }));
        const meta = D.el('div');
        meta.appendChild(D.el('p', { class: 'font-semibold text-white', text: username }));
        const roleLine = D.el('p', { class: 'text-xs ' + roleColor });
        roleLine.appendChild(D.icon(collab.role === 'editor' ? 'pen' : 'eye'));
        roleLine.appendChild(document.createTextNode(' ' + D.text(collab.role)));
        meta.appendChild(roleLine);
        left.appendChild(meta);
        div.appendChild(left);

        const actions = D.el('div', { class: 'flex gap-2' });

        const userId = D.safeId(collab.user && collab.user.id);
        if (userId === null) {
            // Without a validated id we cannot address the API safely, so we
            // render no action buttons rather than guessing.
            return div;
        }

        const roleBtn = D.el('button', {
            class: 'text-sm text-indigo-400 hover:text-indigo-300',
            type: 'button',
            'aria-label': 'Change role for ' + username,
            title: 'Change role',
        });
        roleBtn.appendChild(D.icon('user-edit'));
        roleBtn.addEventListener('click', () => this.changeCollaboratorRole(userId));
        actions.appendChild(roleBtn);

        const removeBtn = D.el('button', {
            class: 'text-sm text-red-400 hover:text-red-300',
            type: 'button',
            'aria-label': 'Remove ' + username,
            title: 'Remove collaborator',
        });
        removeBtn.appendChild(D.icon('times'));
        removeBtn.addEventListener('click', () => this.removeCollaborator(userId, username));
        actions.appendChild(removeBtn);

        div.appendChild(actions);
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
                headers: this.csrfHeader(),
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
            this.showMessage('Failed to add collaborator', 'error');
        }
    }
    
    async removeCollaborator(userId, username) {
        if (!confirm(`Remove ${username} from collaborators?`)) return;
        
        try {
            const response = await fetch(`/api/lists/${this.listId}/collaborators/${userId}`, {
                method: 'DELETE',
                headers: this.csrfHeader()
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Removed ${username}`, 'success');
                this.loadCollaborators();
            } else {
                this.showMessage(data.error || 'Failed to remove collaborator', 'error');
            }
        } catch (error) {
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
                headers: this.csrfHeader(),
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
        const D = window.FrameIQDOM;
        const container = document.getElementById('list-categories');
        if (!container) return;

        try {
            const response = await fetch(`/api/lists/${this.listId}/categories`);
            const data = await response.json();
            
            if (response.ok && data.categories.length > 0) {
                D.clear(container);
                data.categories.forEach(cat => {
                    container.appendChild(this.createCategoryBadge(cat, true));
                });
            } else {
                D.clear(container);
                container.appendChild(D.el('span', {
                    class: 'text-gray-500 text-sm',
                    text: 'No categories',
                }));
            }
        } catch (error) {
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
        }
    }
    
    createCategoryBadge(category, removable = false) {
        const D = window.FrameIQDOM;
        const span = D.el('span', {
            class: 'inline-flex items-center gap-2 px-3 py-1 rounded-full text-sm font-semibold',
        });
        // The colour is a stored field — validated so it cannot inject extra
        // CSS declarations or a url() fetch.
        span.style.backgroundColor = D.safeCssColor(category.color, '#6366f1');
        span.style.color = '#ffffff';

        span.appendChild(D.icon(category.icon || 'tag'));
        // F21 — category name is stored text: textContent, never markup.
        span.appendChild(D.el('span', { text: category.name }));

        if (removable) {
            const categoryId = D.safeId(category.id);
            if (categoryId !== null) {
                const name = D.text(category.name);
                const btn = D.el('button', {
                    class: 'ml-1 hover:text-red-200',
                    type: 'button',
                    'aria-label': 'Remove category ' + name,
                    title: 'Remove category',
                });
                btn.appendChild(D.icon('times', 'text-xs'));
                btn.addEventListener('click', () => this.removeCategory(categoryId, name));
                span.appendChild(btn);
            }
        }

        return span;
    }
    
    openCategoryModal() {
        const D = window.FrameIQDOM;
        const modal = document.getElementById('category-modal');
        if (!modal) return;

        // Populate category grid
        const grid = document.getElementById('category-grid');
        if (grid && this.availableCategories) {
            D.clear(grid);
            this.availableCategories.forEach(cat => {
                const catId = D.safeId(cat.id);
                const btn = D.el('button', {
                    class: 'p-4 bg-gray-700 rounded-lg hover:bg-gray-600 transition-colors text-left',
                    type: 'button',
                });

                const head = D.el('div', { class: 'flex items-center gap-3 mb-2' });
                const glyph = D.icon(cat.icon || 'tag', 'text-2xl');
                glyph.style.color = D.safeCssColor(cat.color, '');
                head.appendChild(glyph);
                head.appendChild(D.el('span', { class: 'font-semibold text-white', text: cat.name }));
                btn.appendChild(head);

                btn.appendChild(D.el('p', {
                    class: 'text-xs text-gray-400',
                    text: cat.description || '',
                }));

                const usage = D.el('p', { class: 'text-xs text-gray-500 mt-2' });
                usage.appendChild(D.icon('list'));
                usage.appendChild(document.createTextNode(
                    ' ' + (D.safeId(cat.usage_count) ?? 0) + ' lists'));
                btn.appendChild(usage);

                if (catId !== null) {
                    const name = D.text(cat.name);
                    btn.addEventListener('click', () => this.addCategory(catId, name));
                } else {
                    btn.disabled = true;
                }

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
                headers: this.csrfHeader(),
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
            this.showMessage('Failed to add category', 'error');
        }
    }
    
    async removeCategory(categoryId, categoryName) {
        try {
            const response = await fetch(`/api/lists/${this.listId}/categories/${categoryId}`, {
                method: 'DELETE',
                headers: this.csrfHeader()
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage(`Removed category: ${categoryName}`, 'success');
                this.loadCategories();
            } else {
                this.showMessage(data.error || 'Failed to remove category', 'error');
            }
        } catch (error) {
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
        }
    }
    
    displayAnalytics(data) {
        const D = window.FrameIQDOM;
        const container = document.getElementById('analytics-panel');
        if (!container) return;

        const analytics = data.analytics || {};
        D.clear(container);

        const panel = D.el('div', {
            class: 'bg-gray-800 rounded-lg p-6 border border-gray-700',
        });

        const heading = D.el('h3', { class: 'text-xl font-bold text-indigo-400 mb-4' });
        heading.appendChild(D.icon('chart-line'));
        heading.appendChild(document.createTextNode(' List Analytics'));
        panel.appendChild(heading);

        const stat = (value, label) => {
            const cell = D.el('div', { class: 'text-center' });
            cell.appendChild(D.el('p', {
                class: 'text-3xl font-bold text-white',
                text: D.safeId(value) ?? 0,
            }));
            cell.appendChild(D.el('p', { class: 'text-sm text-gray-400', text: label }));
            return cell;
        };

        const grid = D.el('div', { class: 'grid grid-cols-2 md:grid-cols-4 gap-4 mb-6' });
        grid.appendChild(stat(analytics.view_count, 'Total Views'));
        grid.appendChild(stat(analytics.unique_viewers, 'Unique Viewers'));
        grid.appendChild(stat(analytics.share_count, 'Shares'));
        grid.appendChild(stat(analytics.fork_count, 'Forks'));
        panel.appendChild(grid);

        const viewers = Array.isArray(data.recent_viewers) ? data.recent_viewers : [];
        if (viewers.length > 0) {
            const wrap = D.el('div');
            wrap.appendChild(D.el('h4', {
                class: 'font-semibold text-gray-300 mb-3',
                text: 'Recent Viewers',
            }));
            const list = D.el('div', { class: 'space-y-2' });
            viewers.slice(0, 5).forEach(viewer => {
                const row = D.el('div', {
                    class: 'flex items-center justify-between text-sm',
                });
                // Viewer usernames are user-set: textContent only.
                row.appendChild(D.el('span', { class: 'text-gray-400', text: viewer.username }));
                row.appendChild(D.el('span', {
                    class: 'text-gray-500',
                    text: new Date(viewer.viewed_at).toLocaleString(),
                }));
                list.appendChild(row);
            });
            wrap.appendChild(list);
            panel.appendChild(wrap);
        }

        container.appendChild(panel);
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
        }
    }
    
    // ============================================================================
    // UI HELPERS
    // ============================================================================
    
    showMessage(message, type = 'info') {
        const D = window.FrameIQDOM;
        const alertClass = type === 'success' ? 'bg-green-500' :
                          type === 'error' ? 'bg-red-500' : 'bg-blue-500';

        // F21 — `message` embeds usernames and category names supplied by
        // other users. It is rendered as TEXT; the previous innerHTML build
        // was a stored-XSS sink reachable from this toast alone.
        const alert = D.el('div', {
            class: `fixed top-4 right-4 ${alertClass} text-white px-6 py-3 rounded-lg shadow-lg z-50 transition-all transform`,
            role: type === 'error' ? 'alert' : 'status',
        });
        const row = D.el('div', { class: 'flex items-center gap-3' });
        row.appendChild(D.icon(
            type === 'success' ? 'check-circle'
                : type === 'error' ? 'exclamation-circle' : 'info-circle'));
        row.appendChild(D.el('span', { text: message }));
        alert.appendChild(row);

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
