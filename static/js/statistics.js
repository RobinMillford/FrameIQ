/**
 * Personal statistics (Feature #8, Phase 3).
 *
 * Renders the profile-page Statistics section from the canonical private API:
 *
 *     GET /api/statistics[?year=YYYY|?lifetime=true]
 *
 * Responsibilities ONLY:
 *  - exactly ONE /api/statistics request per page load; ONE more per
 *    explicit period switch (no polling, no setInterval, no scroll refetch)
 *  - validate the minimal response shape
 *  - render SERVER-PROVIDED values verbatim — no statistics math happens
 *    here (no averages, rates, rankings, hours, or month fabrication),
 *    no TMDb calls, no storage caching
 *  - bounded skeleton while loading; graceful empty state; on any API
 *    failure a neutral message — the rest of the profile page is never
 *    broken
 *
 * XSS safety: every dynamic value is inserted with textContent; no
 * user-controlled string ever reaches innerHTML.
 */
(function () {
    'use strict';

    function el(id) {
        return document.getElementById(id);
    }

    function show(id) {
        var node = el(id);
        if (node) {
            node.classList.remove('hidden');
        }
    }

    function hide(id) {
        var node = el(id);
        if (node) {
            node.classList.add('hidden');
        }
    }

    function setText(id, text) {
        var node = el(id);
        if (node) {
            node.textContent = text;
        }
    }

    function setBar(listId, item, max) {
        // Simple server-ordered HTML/CSS bar; the width is a bounded
        // presentation scaling of the server count, and the textual value
        // is always rendered for screen readers (never color alone).
        var li = document.createElement('li');
        li.className = 'flex items-center gap-2';
        var label = document.createElement('span');
        label.className = 'text-xs text-[var(--text-hi)] w-28 shrink-0 truncate';
        label.textContent = item.label;
        label.title = item.label;
        var track = document.createElement('span');
        track.className = 'flex-1 h-2 bg-black/40 rounded overflow-hidden';
        var fill = document.createElement('span');
        fill.className = 'block h-full bg-[var(--accent)] rounded';
        var pct = max > 0 ? Math.round((item.value / max) * 100) : 0;
        fill.style.width = Math.max(pct, item.value > 0 ? 4 : 0) + '%';
        track.appendChild(fill);
        var value = document.createElement('span');
        value.className = 'font-slate text-[10px] text-[var(--text-low)] w-10 text-right shrink-0';
        value.textContent = String(item.value);
        li.appendChild(label);
        li.appendChild(track);
        li.appendChild(value);
        el(listId).appendChild(li);
    }

    function renderGenres(names) {
        var list = el('statistics-genres');
        list.textContent = '';
        names.forEach(function (genre) {
            var li = document.createElement('li');
            li.className =
                'inline-flex items-center bg-black/40 rounded-full px-3 py-1';
            var label = document.createElement('span');
            label.className = 'text-xs text-[var(--text-hi)]';
            label.textContent = genre.name;
            li.appendChild(label);
            var count = document.createElement('span');
            count.className =
                'font-slate text-[10px] uppercase text-[var(--text-low)] ml-1';
            count.textContent = String(genre.count);
            li.appendChild(count);
            list.appendChild(li);
        });
        show('statistics-genres-block');
    }

    function renderRatings(distribution, ratingCount) {
        var list = el('statistics-rating-distribution');
        list.textContent = '';
        var buckets = Object.keys(distribution); // server order, fixed 0.5–5.0
        var max = 0;
        buckets.forEach(function (bucket) {
            if (distribution[bucket] > max) {
                max = distribution[bucket];
            }
        });
        buckets.forEach(function (bucket) {
            setBar('statistics-rating-distribution',
                { label: bucket + ' stars', value: distribution[bucket] },
                max);
        });
        setText('statistics-rating-count',
            ratingCount + (ratingCount === 1 ? ' rated event' : ' rated events'));
        show('statistics-ratings-block');
    }

    function renderMonthly(months) {
        var list = el('statistics-monthly');
        list.textContent = '';
        var max = 0;
        months.forEach(function (row) {
            if (row.count > max) {
                max = row.count;
            }
        });
        months.forEach(function (row) {
            setBar('statistics-monthly',
                { label: row.month, value: row.count }, max);
        });
        show('statistics-monthly-block');
    }

    function renderPeople(listId, blockId, people) {
        // Neutral, descriptive rows only: "N watches \u00b7 M titles".
        // Names are untrusted display text — textContent everywhere;
        // every row's full fact is its accessible content (§23).
        var list = el(listId);
        list.textContent = '';
        people.forEach(function (person) {
            var li = document.createElement('li');
            li.className = 'flex items-baseline justify-between gap-2';
            var name = document.createElement('span');
            name.className = 'text-sm text-[var(--text-hi)] truncate';
            name.textContent = person.name;
            var counts = document.createElement('span');
            counts.className =
                'font-slate text-[10px] text-[var(--text-low)] shrink-0';
            var watches = person.watch_event_count;
            var titles = person.distinct_title_count;
            counts.textContent = watches +
                (watches === 1 ? ' watch \u00b7 ' : ' watches \u00b7 ') +
                titles + (titles === 1 ? ' title' : ' titles');
            li.setAttribute('aria-label', person.name + ': ' +
                counts.textContent);
            li.appendChild(name);
            li.appendChild(counts);
            list.appendChild(li);
        });
        show(blockId);
    }

    function renderHeatmap(dailyActivity, activeDays, maxEvents) {
        var grid = el('statistics-heatmap');
        grid.textContent = '';
        dailyActivity.forEach(function (row) {
            // One semantic list item per active day. Understandable
            // without color: the count is always rendered as text and
            // the accessible name carries the full fact (§14).
            var cell = document.createElement('span');
            cell.setAttribute('role', 'listitem');
            var intensity = maxEvents > 0
                ? Math.min(4, Math.ceil(row.count / maxEvents * 4))
                : 1;
            cell.className = 'heatmap-cell heatmap-l' + intensity +
                ' w-3 h-3 rounded-sm shrink-0';
            cell.textContent = String(row.count);
            cell.title = row.date + ': ' + row.count +
                (row.count === 1 ? ' watch' : ' watches');
            cell.setAttribute('aria-label',
                row.date + ': ' + row.count +
                (row.count === 1 ? ' watch' : ' watches'));
            grid.appendChild(cell);
        });
        setText('statistics-heatmap-summary',
            activeDays + (activeDays === 1 ? ' active day' : ' active days') +
            ' \u00b7 busiest day: ' + maxEvents +
            (maxEvents === 1 ? ' watch' : ' watches'));
        show('statistics-heatmap-block');
    }

    function renderSeasonQuality(seasons) {
        // Phase 7: bounded neutral rows — "Show S1 · avg 4.3 (8 rated)".
        // Descriptive display ordering only (§11): never framed as a
        // quality judgment or preference. Server ordering, server
        // values, textContent everywhere; accessible via the aria-label
        // on each row.
        var list = el('statistics-season-quality');
        var block = el('statistics-season-quality-block');
        if (!list || !block) {
            return;
        }
        while (list.firstChild) {
            list.removeChild(list.firstChild);
        }
        seasons.forEach(function (season) {
            var li = document.createElement('li');
            li.className = 'flex items-center gap-2 text-xs';
            var name = document.createElement('span');
            name.className = 'text-[var(--text-hi)] w-28 shrink-0 truncate';
            name.textContent = season.show_name;
            name.title = season.show_name;
            var meta = document.createElement('span');
            meta.className = 'font-slate text-[10px] text-[var(--text-low)]';
            meta.textContent = 'S' + season.season_number +
                ' \u00b7 avg ' + season.average_rating +
                ' (' + season.rating_count + ' rated)';
            li.setAttribute('aria-label',
                season.show_name + ' season ' + season.season_number +
                ': average rating ' + season.average_rating +
                ' from ' + season.rating_count + ' rated episodes');
            li.appendChild(name);
            li.appendChild(meta);
            list.appendChild(li);
        });
        block.classList.remove('hidden');
    }

    function render(data) {
        var summary = data;
        setText('statistics-watch-events', String(summary.total_watch_events));
        setText('statistics-distinct-titles', String(summary.distinct_titles));
        setText('statistics-hours', String(summary.total_hours_watched));
        setText('statistics-average-rating',
            summary.average_rating === null || summary.average_rating === undefined
                ? '—'
                : String(summary.average_rating));
        setText('statistics-rewatches', String(summary.rewatch_count));

        if (summary.top_genres && summary.top_genres.length) {
            renderGenres(summary.top_genres);
        }

        if (summary.rating_distribution &&
                Object.keys(summary.rating_distribution).length) {
            renderRatings(summary.rating_distribution, summary.rating_count);
        }

        if (summary.monthly_watch_counts && summary.monthly_watch_counts.length) {
            renderMonthly(summary.monthly_watch_counts);
        }

        if (Array.isArray(summary.daily_activity) &&
                summary.daily_activity.length) {
            renderHeatmap(summary.daily_activity,
                summary.active_watch_days, summary.max_daily_watch_events);
        }

        if (Array.isArray(summary.directors) && summary.directors.length) {
            renderPeople('statistics-directors',
                'statistics-directors-block', summary.directors);
        }

        if (Array.isArray(summary.actors) && summary.actors.length) {
            renderPeople('statistics-actors',
                'statistics-actors-block', summary.actors);
        } else {
            // §24: no actor persistence exists yet — neutral note, no
            // fabricated people and no placeholder actors wording.
            show('statistics-actors-empty');
            show('statistics-actors-block');
        }

        // Phase 7: season quality — the server provides the show name,
        // season number, rating count, average, and the full ten-bucket
        // distribution. All values are rendered verbatim (no averaging,
        // ranking, or rating math here). Hidden when the list is empty.
        if (Array.isArray(summary.season_quality) &&
                summary.season_quality.length) {
            renderSeasonQuality(summary.season_quality);
        }

        var media = summary.media_type_distribution || {};
        setText('statistics-media',
            (media.movie || 0) + ' movie watch events \u00b7 ' +
            (media.tv || 0) + ' TV watch events');
        show('statistics-media-block');

        if (typeof summary.runtime_missing_events === 'number') {
            setText('statistics-runtime',
                summary.runtime_covered_events +
                ' events with known runtime \u00b7 ' +
                summary.runtime_missing_events + ' without');
            show('statistics-runtime-block');
        }

        hide('statistics-loading');
        hide('statistics-error');
        hide('statistics-empty');
        show('statistics-content');
        show('statistics-section');
    }

    function renderEmpty() {
        hide('statistics-loading');
        hide('statistics-error');
        hide('statistics-content');
        show('statistics-empty');
        show('statistics-section');
    }

    function renderError() {
        hide('statistics-loading');
        hide('statistics-content');
        hide('statistics-empty');
        show('statistics-error');
        show('statistics-section');
    }

    function isValidShape(data) {
        return !!(data && typeof data === 'object' &&
            typeof data.total_watch_events === 'number' &&
            typeof data.distinct_titles === 'number' &&
            data.rating_distribution && data.media_type_distribution &&
            Array.isArray(data.monthly_watch_counts));
    }

    function fetchStatistics(period) {
        var url = period === 'lifetime'
            ? '/api/statistics?lifetime=true'
            : '/api/statistics';
        show('statistics-loading');
        hide('statistics-content');
        hide('statistics-error');
        hide('statistics-empty');
        fetch(url, {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        })
            .then(function (res) {
                if (!res.ok) {
                    throw new Error('statistics HTTP ' + res.status);
                }
                return res.json();
            })
            .then(function (data) {
                if (!isValidShape(data)) {
                    throw new Error('statistics malformed response');
                }
                if (data.total_watch_events === 0 &&
                        data.distinct_titles === 0) {
                    renderEmpty();
                    return;
                }
                render(data);
            })
            .catch(function () {
                renderError(); // never break the profile page on failure
            });
    }

    function init() {
        var section = el('statistics-section');
        if (!section) {
            return; // not the profile page — nothing to do
        }
        var selector = el('statistics-period');
        if (selector) {
            selector.addEventListener('change', function () {
                fetchStatistics(selector.value);
            });
        }
        fetchStatistics('current'); // exactly one initial request
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
