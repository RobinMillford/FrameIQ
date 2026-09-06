/**
 * Shared detail-page init (movie & TV detail).
 *
 * Note: ReviewManager is NOT initialized here — review-manager.js already
 * auto-initializes itself on DOMContentLoaded using body[data-media-id],
 * and having both would create two instances (duplicate
 * /api/media/<id>/reviews requests and duplicate event listeners).
 */

// Priority dropdown toggle function
function togglePriorityDropdown(dropdownId) {
    const dropdown = document.getElementById(dropdownId);
    dropdown.classList.toggle('hidden');

    // Close dropdown when clicking outside
    document.addEventListener('click', function closeDropdown(e) {
        if (!e.target.closest(`#${dropdownId}`) && !e.target.closest('button[onclick*="' + dropdownId + '"]')) {
            dropdown.classList.add('hidden');
            document.removeEventListener('click', closeDropdown);
        }
    });
}
