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
        this.setupCoverImageUpload();
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
            const response = await fetch(`/api/lists/${this.listId}/reorder`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ item_order: itemOrder })
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage('List reordered successfully', 'success');
                this.originalOrder = itemOrder;
            } else {
                this.showMessage(data.error || 'Failed to reorder list', 'error');
            }
        } catch (error) {
            console.error('Error reordering list:', error);
            this.showMessage('Failed to reorder list', 'error');
        }
    }
    
    // ============================================================================
    // Cover Image Upload
    // ============================================================================
    
    setupCoverImageUpload() {
        const uploadBtn = document.getElementById('upload-cover-btn');
        const coverInput = document.getElementById('cover-image-input');
        
        if (uploadBtn && coverInput) {
            uploadBtn.addEventListener('click', () => coverInput.click());
            coverInput.addEventListener('change', (e) => this.handleCoverImageUpload(e));
        }
    }
    
    async handleCoverImageUpload(e) {
        const file = e.target.files[0];
        if (!file) return;
        
        // Validate file type
        if (!file.type.startsWith('image/')) {
            this.showMessage('Please select an image file', 'error');
            return;
        }
        
        // Validate file size (max 5MB)
        if (file.size > 5 * 1024 * 1024) {
            this.showMessage('Image size must be less than 5MB', 'error');
            return;
        }
        
        try {
            // In a real app, upload to cloud storage (S3, Cloudinary, etc.)
            // For now, we'll use a data URL (not recommended for production)
            const reader = new FileReader();
            reader.onload = async (event) => {
                const imageUrl = event.target.result;
                await this.updateCoverImage(imageUrl);
            };
            reader.readAsDataURL(file);
            
        } catch (error) {
            console.error('Error uploading cover image:', error);
            this.showMessage('Failed to upload cover image', 'error');
        }
    }
    
    async updateCoverImage(imageUrl) {
        try {
            const response = await fetch(`/api/lists/${this.listId}/cover`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ cover_image: imageUrl })
            });
            
            const data = await response.json();
            
            if (response.ok) {
                this.showMessage('Cover image updated successfully', 'success');
                
                // Update the cover image in the UI
                const coverElement = document.getElementById('list-cover');
                if (coverElement) {
                    coverElement.src = imageUrl;
                }
            } else {
                this.showMessage(data.error || 'Failed to update cover image', 'error');
            }
        } catch (error) {
            console.error('Error updating cover image:', error);
            this.showMessage('Failed to update cover image', 'error');
        }
    }
    
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
            console.error('Error copying to clipboard:', error);
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
