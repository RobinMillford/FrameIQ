/**
 * Shared detail-page initialization (movie & TV detail).
 * Expects review-manager.js to be loaded first.
 */
document.addEventListener('DOMContentLoaded', function() {
    const reviewSection = document.querySelector('.media-reviews-section');
    if (reviewSection) {
        const mediaId = reviewSection.dataset.mediaId;
        const mediaType = reviewSection.dataset.mediaType;

        window.reviewManager = new ReviewManager({
            mediaId: mediaId,
            mediaType: mediaType
        });
    }
});

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
