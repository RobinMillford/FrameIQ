/**
 * Week 2 Lists Enhancement Manager
 * Handles drag-and-drop reordering, cover images, and shareable links
 */
class ListEnhancementsManager {
    constructor(listId) {
        this.listId = listId;
        this.isDragging = false;
        this.draggedElement = null;
        this.originalOrder = [];
        
        this.init();
    }
    
    init() {
        this.setupDragAndDrop();
        this.setupShareableLink();
    }
    
    // ============================================================================
    // Drag and Drop Reordering
    // ============================================================================
    
    setupDragAndDrop() {
        const listItems = document.querySelectorAll('.list-item[draggable="true"]');
        
        listItems.forEach((item, index) => {
            item.dataset.position = index;
            
            item.addEventListener('dragstart', (e) => this.handleDragStart(e));
            item.addEventListener('dragend', (e) => this.handleDragEnd(e));
            item.addEventListener('dragover', (e) => this.handleDragOver(e));
            item.addEventListener('drop', (e) => this.handleDrop(e));
            item.addEventListener('dragenter', (e) => this.handleDragEnter(e));
            item.addEventListener('dragleave', (e) => this.handleDragLeave(e));
        });
        
        // Store original order
        this.originalOrder = Array.from(listItems).map(item => item.dataset.itemId);
    }
    
    handleDragStart(e) {
        this.isDragging = true;
        this.draggedElement = e.target;
        e.target.classList.add('opacity-50');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/html', e.target.innerHTML);
    }
    
    handleDragEnd(e) {
        this.isDragging = false;
        e.target.classList.remove('opacity-50');
        
        // Remove all drag over indicators
        document.querySelectorAll('.list-item').forEach(item => {
            item.classList.remove('border-t-4', 'border-indigo-500');
        });
        
        // Check if order changed
        const currentOrder = Array.from(document.querySelectorAll('.list-item'))
            .map(item => item.dataset.itemId);
        
        if (JSON.stringify(currentOrder) !== JSON.stringify(this.originalOrder)) {
            this.saveNewOrder(currentOrder);
        }
    }
    
    handleDragOver(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        return false;
    }
    
    handleDragEnter(e) {
        if (e.target.classList.contains('list-item')) {
            e.target.classList.add('border-t-4', 'border-indigo-500');
        }
    }
    
    handleDragLeave(e) {
        if (e.target.classList.contains('list-item')) {
            e.target.classList.remove('border-t-4', 'border-indigo-500');
        }
    }
    
    handleDrop(e) {
        e.preventDefault();
        e.stopPropagation();
        
        if (this.draggedElement !== e.target && e.target.classList.contains('list-item')) {
            const container = e.target.parentNode;
            const draggedIndex = Array.from(container.children).indexOf(this.draggedElement);
            const targetIndex = Array.from(container.children).indexOf(e.target);
            
            if (draggedIndex < targetIndex) {
                container.insertBefore(this.draggedElement, e.target.nextSibling);
            } else {
                container.insertBefore(this.draggedElement, e.target);
            }
        }
        
        return false;
    }
    
    async saveNewOrder(itemOrder) {
        try {
            // Lists V2: numeric ids + CSRF header (global CSRFProtect covers
            // this PUT; base.html's fetch patch is not loaded on this page).
            const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
            const response = await fetch(`/api/lists/${this.listId}/reorder`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrf
                },
                body: JSON.stringify({ item_order: itemOrder.map(Number) })
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage('List reordered successfully', 'success');
                this.originalOrder = itemOrder;
            } else {
                this.showMessage(data.error || 'Failed to reorder list', 'error');
            }
        } catch (error) {
            this.showMessage('Failed to reorder list', 'error');
        }
    }
    
    // ============================================================================
    // Cover Artwork (Feature F48)
    // ============================================================================
    //
    // The "Cover" upload button and its data-URI writer are GONE, deliberately.
    //
    // That flow read a chosen file with FileReader.readAsDataURL and PUT the
    // resulting `data:` URL to /api/lists/<id>/cover. It could never have
    // worked: a data URL for any real image is far longer than
    // UserList.cover_image's 500-character column, and since F39
    // normalize_cover_image refuses `data:` outright, so every attempt ended in
    // a 400. The UI kept offering it anyway, which is worse than not offering
    // it -- users picked a file and watched nothing happen.
    //
    // A list cover is now derived from the list's own first ten items and drawn
    // by static/js/list-cover.js. Nothing is uploaded and nothing is stored.
    //
    // The server endpoint is intentionally left in place. It is not dead code:
    // it validates and stores an external https cover URL, and a list whose
    // owner already saved one through it keeps rendering that cover. What was
    // removed is the client interaction that could only ever fail.
    
    // ============================================================================
    // Shareable Links
    // ============================================================================
    
    setupShareableLink() {
        const shareBtn = document.getElementById('share-list-btn');
        const copyBtn = document.getElementById('copy-link-btn');
        
        if (shareBtn) {
            shareBtn.addEventListener('click', () => this.showShareModal());
        }
        
        if (copyBtn) {
            copyBtn.addEventListener('click', () => this.copyShareableLink());
        }
    }
    
    showShareModal() {
        const modal = document.getElementById('share-modal');
        if (modal) {
            modal.classList.remove('hidden');
            
            // Populate shareable link
            const listSlug = document.getElementById('list-slug')?.value;
            const shareUrl = `${window.location.origin}/lists/l/${listSlug}`;
            document.getElementById('share-url').value = shareUrl;
        }
    }
    
    async copyShareableLink() {
        const shareUrl = document.getElementById('share-url').value;
        
        try {
            await navigator.clipboard.writeText(shareUrl);
            this.showMessage('Link copied to clipboard!', 'success');
            
            // Update button text temporarily
            const copyBtn = document.getElementById('copy-link-btn');
            const originalText = copyBtn.innerHTML;
            copyBtn.innerHTML = '<i class="fas fa-check"></i> Copied!';
            
            setTimeout(() => {
                copyBtn.innerHTML = originalText;
            }, 2000);
            
        } catch (error) {
            this.showMessage('Failed to copy link', 'error');
        }
    }
    
    // ============================================================================
    // UI Helpers
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
        
        // Animate in
        setTimeout(() => alert.classList.add('scale-100'), 10);
        
        // Remove after 3 seconds
        setTimeout(() => {
            alert.classList.add('opacity-0');
            setTimeout(() => alert.remove(), 300);
        }, 3000);
    }
}

// Auto-initialize if list ID is present
document.addEventListener('DOMContentLoaded', () => {
    const listIdElement = document.querySelector('[data-list-id]');
    if (listIdElement) {
        const listId = listIdElement.dataset.listId;
        window.listEnhancementsManager = new ListEnhancementsManager(listId);
    }
});
