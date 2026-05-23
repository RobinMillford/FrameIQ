document.addEventListener('DOMContentLoaded', function() {
    const profileButton = document.getElementById('profile-button');
    const profileMenu = document.getElementById('profile-menu');

    if (profileButton && profileMenu) {
        profileButton.addEventListener('click', function(e) {
            e.stopPropagation();
            profileMenu.classList.toggle('hidden');
        });

        document.addEventListener('click', function(e) {
            if (!profileButton.contains(e.target) && !profileMenu.contains(e.target)) {
                profileMenu.classList.add('hidden');
            }
        });

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                profileMenu.classList.add('hidden');
            }
        });
    }

    const categoryButtons = document.querySelectorAll('.category-btn');
    const categorySections = document.querySelectorAll('.category-section');

    categoryButtons.forEach(button => {
        button.addEventListener('click', function() {
            const category = this.getAttribute('data-category');

            categoryButtons.forEach(btn => btn.classList.remove('active'));
            this.classList.add('active');

            categorySections.forEach(section => {
                const show = section.getAttribute('data-category') === category || category === 'all';
                section.classList.toggle('hidden', !show);
            });
        });
    });

    document.body.classList.add('watchlist-page');
});
