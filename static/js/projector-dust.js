/**
 * Projector dust — micro-specks drifting in the light beam.
 * Rendered once on load, pure CSS animation after that.
 * Deliberately sparse (~15 specks) — you should never consciously see one.
 */
(function () {
    'use strict';

    function init() {
        const container = document.createElement('div');
        container.className = 'projector-dust';
        container.setAttribute('aria-hidden', 'true');

        for (let i = 0; i < 15; i++) {
            const speck = document.createElement('div');
            speck.className = 'dust-speck';

            const x = Math.random() * 100;
            const y = Math.random() * 100;
            const dx = (Math.random() - 0.5) * 60;
            const dy = -(20 + Math.random() * 80);
            const duration = 12 + Math.random() * 20;
            const delay = Math.random() * duration;
            const size = 0.5 + Math.random() * 1;
            const opacity = 0.05 + Math.random() * 0.1;

            speck.style.left = x + '%';
            speck.style.top = y + '%';
            speck.style.width = size + 'px';
            speck.style.height = size + 'px';
            speck.style.opacity = opacity;
            speck.style.animationDuration = duration + 's';
            speck.style.animationDelay = '-' + delay + 's';
            speck.style.setProperty('--dx', dx + 'px');
            speck.style.setProperty('--dy', dy + 'px');

            container.appendChild(speck);
        }

        document.body.appendChild(container);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
