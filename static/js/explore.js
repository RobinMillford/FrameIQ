/* F11 — discovery results, chips and saved presets.
 *
 * Bounded network work: ONE proxy request per results page. No card ever
 * triggers its own TMDb call — posters come from the same payload.
 *
 * Parameters are NOT invented here. The server canonicalises the query string
 * (api/discovery) and hands this file the exact set to replay, so the browser
 * cannot widen or contradict what was validated server-side.
 */
(function () {
  'use strict';

  var cfgEl = document.getElementById('explore-config');
  if (!cfgEl) return;

  var CFG;
  try { CFG = JSON.parse(cfgEl.textContent); }
  catch (e) { return; }

  var PROXY = '/api/tmdb/proxy';
  var grid = document.getElementById('result-grid');
  var status = document.getElementById('result-status');
  var empty = document.getElementById('empty-state');
  var pager = document.getElementById('pager');
  var prevBtn = document.getElementById('prev-page');
  var nextBtn = document.getElementById('next-page');
  var pageLabel = document.getElementById('page-label');
  var form = document.getElementById('facet-form');
  var ratingOut = document.getElementById('rating-out');

  var state = {
    page: CFG.page || 1,
    totalPages: 1,
    loading: false,
    lastFocus: null
  };

  function isTv() { return CFG.media_type === 'tv'; }

  function detailUrl(m) {
    return isTv() ? '/tv/' + m.id : '/movie/' + m.id;
  }

  function posterUrl(p) {
    return p ? 'https://image.tmdb.org/t/p/w342' + p : '/static/images/no-poster.svg';
  }

  function titleOf(m) { return isTv() ? m.name : m.title; }
  function dateOf(m) { return isTv() ? m.first_air_date : m.release_date; }

  /* ── Cards ─────────────────────────────────────────────────── */
  function buildCard(m) {
    var a = document.createElement('a');
    a.href = detailUrl(m);
    a.className = 'group rounded-xl overflow-hidden border border-[var(--line)] ' +
      'bg-[var(--surface)] hover:border-[var(--accent)] transition-colors ' +
      'focus-visible:outline focus-visible:ring-2 focus-visible:ring-[var(--accent)]';

    var img = document.createElement('img');
    img.src = posterUrl(m.poster_path);
    img.alt = '';
    img.loading = 'lazy';
    img.className = 'w-full aspect-[2/3] object-cover';
    a.appendChild(img);

    var body = document.createElement('div');
    body.className = 'p-2.5';

    var t = document.createElement('p');
    t.className = 'text-sm font-medium text-white line-clamp-2';
    t.textContent = titleOf(m) || 'Untitled';
    body.appendChild(t);

    var meta = document.createElement('p');
    meta.className = 'text-xs mt-1';
    meta.style.color = 'var(--text-low)';
    var bits = [];
    var d = dateOf(m);
    if (d) bits.push(String(d).slice(0, 4));
    if (typeof m.vote_average === 'number' && m.vote_count > 0) {
      bits.push('★ ' + m.vote_average.toFixed(1));
    }
    meta.textContent = bits.join(' · ');
    body.appendChild(meta);

    a.appendChild(body);
    return a;
  }

  function renderCards(results) {
    grid.textContent = '';
    if (!results.length) {
      empty.hidden = false;
      grid.hidden = true;
      return;
    }
    empty.hidden = true;
    grid.hidden = false;
    var frag = document.createDocumentFragment();
    results.forEach(function (m) { frag.appendChild(buildCard(m)); });
    grid.appendChild(frag);
  }

  /* ── Fetch ─────────────────────────────────────────────────── */
  function load(page) {
    if (state.loading) return;
    state.loading = true;
    state.page = page;
    status.textContent = 'Loading titles…';

    var params = new URLSearchParams();
    params.set('path', CFG.path);
    Object.keys(CFG.params).forEach(function (k) {
      if (k === 'page') return;
      params.set(k, CFG.params[k]);
    });
    params.set('page', String(page));

    fetch(PROXY + '?' + params.toString(), {
      headers: { 'Accept': 'application/json' }
    })
      .then(function (r) {
        if (!r.ok) throw new Error('upstream ' + r.status);
        return r.json();
      })
      .then(function (data) {
        var results = data.results || [];
        var total = typeof data.total_results === 'number' ? data.total_results : 0;
        var perPage = 20;
        state.totalPages = Math.max(1, Math.min(Math.ceil(total / perPage), 500));
        renderCards(results);
        pageLabel.textContent = 'Page ' + page + ' of ' + state.totalPages;
        prevBtn.disabled = page <= 1;
        nextBtn.disabled = page >= state.totalPages || results.length === 0;
        status.textContent = results.length
          ? results.length + ' title' + (results.length === 1 ? '' : 's') +
            ' on this page'
          : 'No titles match these filters.';
      })
      .catch(function () {
        grid.textContent = '';
        grid.hidden = true;
        empty.hidden = false;
        status.textContent = 'Could not load titles right now. Please try again.';
      })
      .finally(function () { state.loading = false; });
  }

  /* ── Pager ─────────────────────────────────────────────────── */
  if (prevBtn) prevBtn.addEventListener('click', function () {
    if (state.page > 1) load(state.page - 1);
  });
  if (nextBtn) nextBtn.addEventListener('click', function () {
    if (state.page < state.totalPages) load(state.page + 1);
  });

  /* ── Chips: remove one facet, keep the rest ─────────────────── */
  document.addEventListener('click', function (e) {
    var link = e.target.closest('.remove-chip');
    if (!link) return;
    e.preventDefault();
    state.lastFocus = link;
    var key = link.getAttribute('data-key');
    var value = link.getAttribute('data-value') || '';

    if (key === 'genres') {
      var box = form.querySelector(
        'input[name="genres"][value="' + (window.CSS && CSS.escape
          ? CSS.escape(value) : value) + '"]');
      if (box) box.checked = false;
    } else if (key === 'year_from+year_to') {
      form.querySelector('[name="year_from"]').value = '';
      form.querySelector('[name="year_to"]').value = '';
    } else if (key === 'min_rating') {
      var range = form.querySelector('[name="min_rating"]');
      if (range) range.value = 0;
      if (ratingOut) ratingOut.textContent = 'Any';
    } else if (key === 'sort') {
      form.querySelector('[name="sort"]').value = 'popularity.desc';
    } else {
      form.querySelector('[name="' + key + '"]').value = '';
    }
    form.submit();
  });

  /* ── Rating output ─────────────────────────────────────────── */
  var ratingInput = form && form.querySelector('[name="min_rating"]');
  if (ratingInput && ratingOut) {
    ratingInput.addEventListener('input', function () {
      var v = parseFloat(ratingInput.value);
      ratingOut.textContent = (v > 0 ? v.toFixed(1) : 'Any');
    });
  }

  /* ── Saved presets ─────────────────────────────────────────── */
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.delete-preset');
    if (!btn) return;
    e.preventDefault();
    var id = btn.getAttribute('data-id');
    if (!window.confirm('Delete this saved discovery filter?')) return;
    fetch('/explore/presets/' + encodeURIComponent(id), {
      method: 'DELETE',
      headers: { 'X-CSRFToken': csrfToken() }
    }).then(function () { window.location.reload(); });
  });

  var saveBtn = document.getElementById('save-preset');
  if (saveBtn) {
    saveBtn.addEventListener('click', function () {
      var nameField = document.getElementById('preset-name');
      var name = nameField && nameField.value.trim();
      if (!name) {
        if (nameField) nameField.focus();
        return;
      }
      var query = CFG.query || {};
      var config = {
        media_type: query.type || 'movie',
        genres: query.genres ? query.genres.split(',') : [],
        sort: query.sort || 'popularity.desc'
      };
      if (query.year_from) config.year_from = query.year_from;
      if (query.year_to) config.year_to = query.year_to;
      if (query.min_rating) config.min_rating = query.min_rating;

      fetch('/explore/presets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   'X-CSRFToken': csrfToken() },
        body: JSON.stringify({ name: name, config: config })
      }).then(function () { window.location.reload(); });
    });
  }

  load(state.page);
})();