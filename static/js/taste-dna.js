/**
 * Taste DNA (Feature #6, Phase 14).
 *
 * Renders the profile-page Taste DNA section from the canonical private API:
 *
 *     GET /api/taste-profile
 *
 * Responsibilities ONLY:
 *  - exactly ONE /api/taste-profile request per page load (no polling, no
 *    scroll refetch, no setInterval)
 *  - validate the minimal response shape
 *  - render strength labels / sections using the server presentation model
 *    (the server is authoritative — no strength, confidence or taste math
 *    happens here, no TMDb calls, no raw profile JSON is read)
 *  - hide the whole section on cold start, empty data, or any API failure so
 *    the rest of the profile page is never broken
 *
 * Privacy: the response contains only human-readable presentation values.
 * Nothing is written to DOM data attributes beyond rendered text.
 */
(function () {
    'use strict';

    function el(id) {
        return document.getElementById(id);
    }

    function hideSection() {
        var section = el('taste-dna-section');
        if (section) {
            section.classList.add('hidden');
        }
    }

    function show(id) {
        var node = el(id);
        if (node) {
            node.classList.remove('hidden');
        }
    }

    function setText(id, text) {
        var node = el(id);
        if (node) {
            node.textContent = text;
        }
    }

    /**
     * Strength chips: label text carries the meaning; visual weight only
     * reinforces it (strength is never conveyed by color alone — every chip
     * shows its label).
     */
    function chip(name, strength) {
        var li = document.createElement('li');
        li.className =
            'inline-flex items-center gap-1 bg-black/40 rounded-full px-3 py-1';
        var label = document.createElement('span');
        label.className = 'text-xs text-[var(--text-hi)]';
        label.textContent = name;
        li.appendChild(label);
        if (strength && strength !== 'high') {
            var tag = document.createElement('span');
            tag.className =
                'font-slate text-[10px] uppercase text-[var(--text-low)]';
            tag.textContent = strength;
            li.appendChild(tag);
        }
        return li;
    }

    function fillList(listId, blockId, items) {
        if (!items || !items.length) {
            return;
        }
        var list = el(listId);
        if (!list) {
            return;
        }
        items.forEach(function (item) {
            list.appendChild(chip(item.name, item.strength));
        });
        show(blockId);
    }

    function mediaSentence(mediaPreference) {
        if (!mediaPreference) {
            return null;
        }
        var movie = mediaPreference.movie;
        var tv = mediaPreference.tv;
        if (movie === 'high' && (!tv || tv === 'low')) {
            return 'Mostly movies';
        }
        if (tv === 'high' && (!movie || movie === 'low')) {
            return 'Mostly TV';
        }
        if (movie && tv && (movie === 'high' || movie === 'moderate') &&
                (tv === 'high' || tv === 'moderate')) {
            return 'Balanced between movies and TV';
        }
        if (movie) {
            return 'Mostly movies';
        }
        if (tv) {
            return 'Mostly TV';
        }
        return null;
    }

    var LEVEL_TEXT = {
        strong: 'Strong taste profile',
        developing: 'Developing taste profile',
        limited: 'Early taste signals'
    };

    function render(data) {
        if (!data || data.available !== true || !data.top_genres ||
                !data.top_genres.length) {
            renderColdStart();
            return;
        }

        setText('taste-dna-level', LEVEL_TEXT[data.level] ||
            LEVEL_TEXT.developing);

        fillList('taste-dna-genres', 'taste-dna-genres-block',
            data.top_genres);
        fillList('taste-dna-avoid', 'taste-dna-avoid-block',
            data.avoid_genres);
        fillList('taste-dna-directors', 'taste-dna-directors-block',
            data.top_directors);
        fillList('taste-dna-eras', 'taste-dna-eras-block', data.eras);

        var mediaText = mediaSentence(data.media_preference);
        if (mediaText) {
            setText('taste-dna-media', mediaText);
            show('taste-dna-media-block');
        }

        if (data.runtime && data.runtime.min && data.runtime.max) {
            setText('taste-dna-runtime',
                'About ' + data.runtime.min + '\u2013' + data.runtime.max +
                ' minutes');
            show('taste-dna-runtime-block');
        }

        if (data.titles_analyzed) {
            setText('taste-dna-basis',
                'Based on ' + data.titles_analyzed + ' titles');
        }

        show('taste-dna-content');
        show('taste-dna-section');
    }

    function renderColdStart() {
        show('taste-dna-coldstart');
        show('taste-dna-section');
    }

    function init() {
        var section = el('taste-dna-section');
        if (!section) {
            return; // not the profile page — nothing to do
        }
        fetch('/api/taste-profile', {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        })
            .then(function (res) {
                if (!res.ok) {
                    throw new Error('taste-profile HTTP ' + res.status);
                }
                return res.json();
            })
            .then(function (data) {
                if (!data || typeof data !== 'object') {
                    throw new Error('taste-profile malformed response');
                }
                if (data.available === false) {
                    renderColdStart();
                    return;
                }
                render(data);
            })
            .catch(function () {
                hideSection(); // never break the profile page on failure
            });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
