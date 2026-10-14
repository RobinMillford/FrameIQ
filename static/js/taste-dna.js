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

    /**
     * F14 actor chip. Identical visual weight to every other Taste DNA chip;
     * the ONLY difference is that a resolvable actor becomes a real link to
     * the existing /actor/<id> page. An entry without a url renders as plain
     * text on purpose — never a fake or inert link.
     */
    function actorChip(item) {
        var li = document.createElement('li');
        li.className =
            'inline-flex items-center gap-1 bg-black/40 rounded-full px-3 py-1';

        if (item.url) {
            var link = document.createElement('a');
            link.href = item.url;
            link.className =
                'text-xs text-[var(--accent-hi)] hover:text-[var(--accent)] ' +
                'hover:underline focus:outline-none ' +
                'focus-visible:ring-2 focus-visible:ring-[var(--accent)] ' +
                'focus-visible:ring-offset-2 focus-visible:rounded';
            link.textContent = item.name;
            link.setAttribute('data-taste-actor-link', item.name);
            li.appendChild(link);
        } else {
            var label = document.createElement('span');
            label.className = 'text-xs text-[var(--text-hi)]';
            label.textContent = item.name;
            li.appendChild(label);
        }

        if (item.strength && item.strength !== 'high') {
            var tag = document.createElement('span');
            tag.className =
                'font-slate text-[10px] uppercase text-[var(--text-low)]';
            tag.textContent = item.strength;
            li.appendChild(tag);
        }
        return li;
    }

    /**
     * Empty-state copy, keyed by the API's actors_status.
     *
     * 'no_cast' — the user HAS watched titles but the dimension holds no cast
     * evidence, i.e. persisted cast metadata is still unenriched or too thin.
     * This is a coverage gap on FrameIQ's side, not a statement about taste.
     *
     * 'no_qualifying_actors' — the dimension ran and produced cast evidence,
     * but nothing scored positive (e.g. only negative cast signals). The
     * metadata EXISTS; there is simply no clear actor preference yet. Showing
     * the coverage message here would wrongly tell the user their cast data
     * is missing when it is present.
     */
    var ACTOR_EMPTY_TEXT = {
        no_cast: 'Actor insights aren\u2019t available yet because cast ' +
            'metadata is still limited.',
        no_qualifying_actors: 'No actors stand out yet \u2014 the titles ' +
            'you\u2019ve watched so far haven\u2019t produced a clear ' +
            'actor signal.'
    };

    function fillActors(items, status) {
        var block = el('taste-dna-actors-block');
        var empty = el('taste-dna-actors-empty-block');
        // Always start from the hidden state so a re-render can never leave a
        // stale section behind.
        if (block) {
            block.classList.add('hidden');
        }
        if (empty) {
            empty.classList.add('hidden');
        }

        if (items && items.length) {
            var list = el('taste-dna-actors');
            if (list) {
                items.forEach(function (item) {
                    if (item && item.name) {
                        list.appendChild(actorChip(item));
                    }
                });
                show('taste-dna-actors-block');
            }
            return;
        }
        // An empty list means two genuinely different things, so the copy
        // must differ. Neither message ever claims the user lacks actor
        // preferences — one is about metadata coverage, the other about a
        // real result that simply has nothing positive yet. Any unknown
        // status stays silent rather than guessing.
        var message = ACTOR_EMPTY_TEXT[status];
        if (!message) {
            return;
        }
        var node = el('taste-dna-actors-empty');
        if (node) {
            node.textContent = message;
            show('taste-dna-actors-empty-block');
        }
    }

    /**
     * Feature F18 — empty-state copy for the director section.
     *
     * Deliberately qualified. An empty director dimension means no persisted
     * director rows were found; it does NOT mean the user has no director
     * preferences, and it does NOT mean credits were successfully persisted
     * (the historical enrichment defect could mark titles enriched while
     * writing nothing). So the wording names the coverage limitation and stops
     * there — it makes no claim about taste, and no promise about what will
     * appear later.
     */
    var DIRECTOR_EMPTY_TEXT = {
        // No director evidence at all. We genuinely cannot tell WHY — the
        // titles may be uncredited, never enriched, or unreadable — so the
        // copy states only the observable fact and stops there.
        no_director_data: 'Director credits aren\u2019t available for enough ' +
            'of the titles you\u2019ve watched, so director insights can\u2019t ' +
            'be calculated yet.',
        // Director evidence EXISTS but nothing scored positive. Saying credits
        // were missing here would be false — they were read.
        no_positive_directors: 'No directors stand out yet \u2014 the titles ' +
            'you\u2019ve watched so far haven\u2019t produced a clear director ' +
            'signal.'
    };

    /**
     * Feature F18 — render directors, or the honest reason there are none.
     *
     * The chips themselves are unchanged from Phase 10; this only adds the
     * empty state and guarantees a re-render cannot leave a stale block
     * behind. Both states reset to hidden first.
     */
    /**
     * Feature F19 — director chip.
     *
     * Visually identical to every other Taste DNA chip; the only difference is
     * that a director which resolved to exactly one local record becomes a real
     * link to their page. Without a url the chip stays plain text on purpose —
     * never a fake or inert link.
     */
    function directorChip(item) {
        var li = document.createElement('li');
        li.className =
            'inline-flex items-center gap-1 bg-black/40 rounded-full px-3 py-1';

        if (item.url) {
            var link = document.createElement('a');
            link.href = item.url;
            link.className =
                'text-xs text-[var(--accent-hi)] hover:text-[var(--accent)] ' +
                'hover:underline focus:outline-none ' +
                'focus-visible:ring-2 focus-visible:ring-[var(--accent)] ' +
                'focus-visible:ring-offset-2 focus-visible:rounded';
            link.textContent = item.name;
            link.setAttribute('data-taste-director-link', item.name);
            li.appendChild(link);
        } else {
            var label = document.createElement('span');
            label.className = 'text-xs text-[var(--text-hi)]';
            label.textContent = item.name;
            li.appendChild(label);
        }

        if (item.strength && item.strength !== 'high') {
            var tag = document.createElement('span');
            tag.className =
                'font-slate text-[10px] uppercase text-[var(--text-low)]';
            tag.textContent = item.strength;
            li.appendChild(tag);
        }
        return li;
    }

    function fillDirectors(items, status) {
        var block = el('taste-dna-directors-block');
        var empty = el('taste-dna-directors-empty-block');
        if (block) {
            block.classList.add('hidden');
        }
        if (empty) {
            empty.classList.add('hidden');
        }

        if (items && items.length) {
            var list = el('taste-dna-directors');
            if (list) {
                items.forEach(function (item) {
                    if (item && item.name) {
                        list.appendChild(directorChip(item));
                    }
                });
                show('taste-dna-directors-block');
            }
            return;
        }
        var message = DIRECTOR_EMPTY_TEXT[status];
        if (!message) {
            return;  // unknown/absent status stays silent rather than guessing
        }
        var node = el('taste-dna-directors-empty');
        if (node) {
            node.textContent = message;
            show('taste-dna-directors-empty-block');
        }
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
        fillDirectors(data.top_directors, data.directors_status);
        fillList('taste-dna-eras', 'taste-dna-eras-block', data.eras);
        fillActors(data.top_actors, data.actors_status);

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
