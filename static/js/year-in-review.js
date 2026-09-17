/**
 * Year in Review (Feature #8, Phase 8).
 *
 * Renders the private recap page from the canonical story API:
 *
 *     GET /api/year-in-review?year=YYYY
 *
 * Responsibilities ONLY:
 *  - exactly ONE /api/year-in-review request per page load; ONE more per
 *    explicit year selection (no polling, no prefetching other years,
 *    no /api/statistics calls — the recap model is the sole data source)
 *  - validate the minimal response shape
 *  - render SERVER-PROVIDED values and sentences verbatim — every
 *    highlight text, the runtime wording, all counts and averages come
 *    from the canonical api.year_in_review model; no statistics math
 *    happens here (no averaging, ranking, rates, hours, or month
 *    fabrication), no TMDb calls, no storage caching
 *  - bounded skeleton while loading; canonical empty state for an
 *    unwatched year; neutral error state that never shows internals
 *
 * XSS safety: every dynamic value is inserted with textContent /
 * createElement / setAttribute; no API content ever reaches innerHTML.
 * Person names, genre names, and show names are untrusted display text.
 */
(function () {
    'use strict';

    // Static month-name table for displaying the canonical 'YYYY-MM'
    // labels (a string mapping, not date arithmetic).
    var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May',
        'June', 'July', 'August', 'September', 'October', 'November',
        'December'];

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

    function clearList(id) {
        var node = el(id);
        if (node) {
            node.textContent = '';
        }
    }

    function monthLabel(canonicalLabel) {
        // '2026-01' → 'January' (static table; the canonical label is
        // also kept in the accessible name below).
        var month = parseInt(canonicalLabel.split('-')[1], 10);
        return MONTH_NAMES[month - 1] || canonicalLabel;
    }

    function setBar(listId, label, value, max, accessibleName) {
        // Server-ordered HTML/CSS bar; the width is a bounded
        // presentation scaling of the server count, and the full fact is
        // always rendered as text for screen readers (never color alone).
        var li = document.createElement('li');
        li.className = 'flex items-center gap-2';
        var name = document.createElement('span');
        name.className = 'text-xs text-[var(--text-hi)] w-24 shrink-0 truncate';
        name.textContent = label;
        name.title = label;
        var track = document.createElement('span');
        track.className = 'flex-1 h-2 bg-black/40 rounded overflow-hidden';
        var fill = document.createElement('span');
        fill.className = 'block h-full bg-[var(--accent)] rounded yir-bar-fill';
        var pct = max > 0 ? Math.round((value / max) * 100) : 0;
        fill.style.width = Math.max(pct, value > 0 ? 4 : 0) + '%';
        track.appendChild(fill);
        var count = document.createElement('span');
        count.className =
            'font-slate text-[10px] text-[var(--text-low)] w-10 text-right shrink-0';
        count.textContent = String(value);
        li.setAttribute('aria-label', accessibleName);
        li.appendChild(name);
        li.appendChild(track);
        li.appendChild(count);
        el(listId).appendChild(li);
    }

    function renderHighlights(highlights) {
        // Each canonical highlight already carries its own descriptive,
        // bounded sentence — rendered verbatim, in server order.
        clearList('yir-highlights');
        var order = ['top_genre', 'busiest_month', 'ratings', 'rewatches',
            'media_split'];
        var count = 0;
        order.forEach(function (key) {
            var item = highlights[key];
            if (!item || !item.text) {
                return; // unsupported facts are omitted, never invented
            }
            var li = document.createElement('li');
            li.className = 'text-sm text-[var(--text-hi)]';
            li.textContent = item.text;
            el('yir-highlights').appendChild(li);
            count += 1;
        });
        if (count === 0) {
            hide('yir-highlights-heading');
        }
    }

    function renderMonthly(monthly) {
        var list = el('yir-monthly');
        clearList('yir-monthly');
        var max = 0;
        monthly.forEach(function (row) {
            if (row.count > max) {
                max = row.count;
            }
        });
        monthly.forEach(function (row) {
            var label = monthLabel(row.month);
            var name = label + ': ' + row.count +
                (row.count === 1 ? ' watch' : ' watches');
            setBar('yir-monthly', label, row.count, max, name);
        });
        show('yir-monthly-block');
    }

    function renderMedia(mediaType, mediaSplitText) {
        var movie = mediaType.movie || 0;
        var tv = mediaType.tv || 0;
        var text = movie + (movie === 1 ? ' movie watch event' :
            ' movie watch events') + ' \u00b7 ' +
            tv + (tv === 1 ? ' TV watch event' : ' TV watch events');
        if (mediaSplitText) {
            text += ' \u2014 ' + mediaSplitText;
        }
        setText('yir-media', text);
        show('yir-media-block');
    }

    function renderHeatmap(dailyActivity) {
        var grid = el('yir-heatmap');
        clearList('yir-heatmap');
        if (!dailyActivity.length) {
            hide('yir-heatmap-block');
            return;
        }
        var max = 0;
        dailyActivity.forEach(function (row) {
            if (row.count > max) {
                max = row.count;
            }
        });
        dailyActivity.forEach(function (row) {
            // One semantic list item per active day; the count is always
            // rendered as text so the heatmap works without color.
            var cell = document.createElement('span');
            cell.setAttribute('role', 'listitem');
            var intensity = max > 0
                ? Math.min(4, Math.ceil(row.count / max * 4))
                : 1;
            cell.className = 'heatmap-cell heatmap-l' + intensity +
                ' w-3 h-3 rounded-sm shrink-0';
            cell.textContent = String(row.count);
            var fact = row.date + ': ' + row.count +
                (row.count === 1 ? ' watch' : ' watches');
            cell.title = fact;
            cell.setAttribute('aria-label', fact);
            grid.appendChild(cell);
        });
        show('yir-heatmap-block');
    }

    function renderGenres(genres) {
        var list = el('yir-genres');
        clearList('yir-genres');
        genres.forEach(function (genre) {
            var li = document.createElement('li');
            li.className =
                'inline-flex items-center bg-black/40 rounded-full px-3 py-1';
            var name = document.createElement('span');
            name.className = 'text-xs text-[var(--text-hi)]';
            name.textContent = genre.name;
            li.appendChild(name);
            var count = document.createElement('span');
            count.className =
                'font-slate text-[10px] uppercase text-[var(--text-low)] ml-1';
            count.textContent = String(genre.count);
            li.appendChild(count);
            list.appendChild(li);
        });
        show('yir-genres-block');
    }

    function renderDirectors(directors) {
        // Neutral rows in canonical count ordering — "N watches · M
        // titles". Names are untrusted display text: textContent only.
        var list = el('yir-directors');
        clearList('yir-directors');
        if (!directors.length) {
            show('yir-directors-empty');
            show('yir-directors-block');
            return;
        }
        directors.forEach(function (person) {
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
            li.setAttribute('aria-label',
                person.name + ': ' + counts.textContent);
            li.appendChild(name);
            li.appendChild(counts);
            list.appendChild(li);
        });
        show('yir-directors-block');
    }

    function renderSeasons(seasons) {
        // Canonical aggregate ratings only — display ordering, never a
        // quality judgment. Hidden entirely when there is no data.
        var list = el('yir-seasons');
        var block = el('yir-seasons-block');
        clearList('yir-seasons');
        if (!seasons.length) {
            hide('yir-seasons-block');
            return;
        }
        seasons.forEach(function (season) {
            var li = document.createElement('li');
            li.className = 'flex items-center gap-2 text-xs';
            var name = document.createElement('span');
            name.className = 'text-[var(--text-hi)] w-32 shrink-0 truncate';
            name.textContent = season.show_name;
            name.title = season.show_name;
            var meta = document.createElement('span');
            meta.className =
                'font-slate text-[10px] text-[var(--text-low)]';
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
        show('yir-seasons-block');
    }

    function renderRuntime(runtime) {
        // Canonical wording only: exact hours when coverage is complete,
        // "At least X hours" when it is not. Never reworded or recomputed.
        setText('yir-runtime', runtime.text);
        show('yir-runtime-block');
    }

    function render(data) {
        setText('yir-year', String(data.year));
        var summary = data.summary;
        setText('yir-watch-events', String(summary.total_watch_events));
        setText('yir-distinct-titles', String(summary.distinct_titles));
        setText('yir-hours', String(summary.total_hours_watched));
        setText('yir-average-rating',
            summary.average_rating === null ||
                summary.average_rating === undefined
                ? '—'
                : String(summary.average_rating));

        renderHighlights(data.highlights || {});
        renderMonthly(data.monthly);
        renderMedia(data.media_type,
            (data.highlights.media_split || {}).text);
        renderHeatmap(data.daily_activity || []);
        renderGenres(data.genres || []);
        renderDirectors((data.people && data.people.directors) || []);
        renderSeasons(data.season_quality || []);
        renderRuntime(data.runtime);

        hide('yir-loading');
        hide('yir-error');
        hide('yir-empty');
        show('yir-content');
    }

    function renderEmpty(data) {
        setText('yir-year', String(data.year));
        hide('yir-loading');
        hide('yir-error');
        hide('yir-content');
        show('yir-empty');
    }

    function renderError() {
        hide('yir-loading');
        hide('yir-content');
        hide('yir-empty');
        show('yir-error');
    }

    function isValidShape(data) {
        return !!(data && typeof data === 'object' &&
            typeof data.year === 'number' &&
            typeof data.available === 'boolean' &&
            typeof data.state === 'string');
    }

    function fetchRecap(year) {
        var url = '/api/year-in-review?year=' + encodeURIComponent(year);
        show('yir-loading');
        hide('yir-content');
        hide('yir-error');
        hide('yir-empty');
        fetch(url, {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        })
            .then(function (res) {
                if (!res.ok) {
                    throw new Error('recap HTTP ' + res.status);
                }
                return res.json();
            })
            .then(function (data) {
                if (!isValidShape(data)) {
                    throw new Error('recap malformed response');
                }
                if (data.state === 'empty') {
                    renderEmpty(data);
                    return;
                }
                render(data);
            })
            .catch(function () {
                renderError(); // never surface internals to the user
            });
    }

    function populateYearSelector(selectedYear) {
        // Simple bounded selector: the requested year plus the nine
        // before it. Options are period labels only — they do not imply
        // that data exists for any year; an unwatched year shows the
        // canonical empty state.
        var selector = el('yir-year-select');
        if (!selector) {
            return;
        }
        for (var offset = 0; offset < 10; offset += 1) {
            var year = selectedYear - offset;
            var option = document.createElement('option');
            option.value = String(year);
            option.textContent = String(year);
            if (year === selectedYear) {
                option.selected = true;
            }
            selector.appendChild(option);
        }
        selector.addEventListener('change', function () {
            fetchRecap(selector.value); // one request per explicit change
        });
    }

    function init() {
        if (!el('yir-content')) {
            return; // not the recap page — nothing to do
        }
        var injectedYear = window.__YIR_YEAR__;
        var year = typeof injectedYear === 'number' ? injectedYear : null;
        if (year === null) {
            renderError();
            return;
        }
        populateYearSelector(year);
        fetchRecap(year); // exactly one initial request
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
