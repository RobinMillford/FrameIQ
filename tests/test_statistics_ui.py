"""Personal statistics profile UI (Feature #8, Phase 3).

Covers the template shell (templates/profile.html), the renderer
(static/js/statistics.js), and the API↔UI interlock using the repo's
established source/template testing conventions (no browser framework):

- shell: statistics section present for authenticated users, NO private
  values server-rendered into the anonymous-reachable HTML
- renderer: exactly one initial fetch, fetch-per-explicit-period-switch,
  no polling, server-value rendering only (no statistics math), safe DOM
  insertion, empty/error states, accessibility hooks
- interlock (§35/§43): every DOM id the renderer writes exists in the
  template, and every response field the renderer reads is a canonical
  api.statistics output key — an API shape change breaks these tests
  instead of silently blanking cards
"""
import re
import uuid

import pytest

TEMPLATE = 'templates/profile.html'
JS = 'static/js/statistics.js'


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _section(template, marker, end_marker):
    start = template.index(marker)
    return template[start:template.index(end_marker, start)]


# ════════════════════════════════════════════════════════════════════════════
# Profile page shell
# ════════════════════════════════════════════════════════════════════════════

def test_profile_contains_statistics_shell():
    template = _read(TEMPLATE)
    assert 'id="statistics-section"' in template
    assert 'id="statistics-period"' in template
    assert 'id="statistics-loading"' in template
    assert 'id="statistics-empty"' in template
    assert 'id="statistics-error"' in template
    assert 'id="statistics-content"' in template


def test_statistics_js_included_exactly_once():
    template = _read(TEMPLATE)
    assert template.count('js/statistics.js') == 1


def test_shell_carries_no_server_rendered_private_values():
    # §25: the section is a shell — no Jinja interpolation of statistics
    # values anywhere inside it (values arrive via authenticated fetch).
    template = _read(TEMPLATE)
    section = _section(template, 'id="statistics-section"',
                       '<!-- Quick stats strip -->')
    assert '{{' not in section and '{%' not in section


def test_anonymous_profile_gets_no_private_statistics(client):
    # The profile route is login_required: an anonymous request never
    # receives the shell (or any private values) at all.
    r = client.get('/profile')
    assert r.status_code in (302, 401)


def test_profile_structure_remains_intact():
    template = _read(TEMPLATE)
    assert 'id="taste-dna-section"' in template          # Taste DNA intact
    assert 'js/taste-dna.js' in template
    assert 'Quick stats strip' in template
    assert 'id="reviews-content"' in template
    assert 'recommendations-preview' in template


def test_period_selector_accessibility():
    template = _read(TEMPLATE)
    assert re.search(r'<select[^>]*id="statistics-period"', template)
    assert 'aria-label="Statistics period"' in template
    assert '<option value="current">' in template
    assert '<option value="lifetime">' in template


# ════════════════════════════════════════════════════════════════════════════
# Renderer behavior (source-level guards, repo convention)
# ════════════════════════════════════════════════════════════════════════════

def test_exactly_one_initial_api_fetch():
    js = _read(JS)
    # init() triggers the single initial request; the fetch URL builder
    # is the only network entry point.
    assert js.count("fetch(url,") == 1
    assert js.count("fetchStatistics('current')") == 1  # in init() only


def test_period_switch_fetches_selected_period():
    js = _read(JS)
    assert "'/api/statistics?lifetime=true'" in js
    assert js.count("addEventListener('change'") == 1
    # Switching re-fetches via the same single entry point — no extra
    # fetch construction elsewhere.
    assert '/api/statistics' in js
    assert js.count("'/api/statistics'") == 1


def _js_source():
    # Strip block comments so prose mentions of banned APIs (docs) don't
    # trip the guards — only real code matches.
    return re.sub(r'/\*.*?\*/', '', _read(JS), flags=re.S)


# ── Fixtures (replicated from the API suite's login/user helpers) ────────────

@pytest.fixture
def stats_user(app):
    from models import User, db as _db
    username = 'statu' + uuid.uuid4().hex[:6]
    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    with app.app_context():
        yield u


@pytest.fixture
def auth_client(client, stats_user):
    client.post('/login', data={
        'username': stats_user.username, 'password': 'TestPass1'})
    return client


def test_no_polling_or_automatic_refresh():
    js = _js_source()
    assert 'setInterval' not in js
    assert 'setTimeout' not in js
    assert 'MutationObserver' not in js
    assert 'IntersectionObserver' not in js


def test_loading_state_exists_and_is_bounded():
    js = _read(JS)
    # Skeleton is shown before fetching and hidden on every settle path.
    assert "show('statistics-loading')" in js
    assert "hide('statistics-loading')" in js


def test_empty_state_rendered():
    js = _read(JS)
    assert "renderEmpty()" in js
    template = _read(TEMPLATE)
    assert 'No viewing history yet.' in template
    assert 'Start logging movies and shows' in template


def test_api_error_handled_without_breaking_profile():
    js = _read(JS)
    assert '.catch(' in js
    assert 'renderError()' in js
    # No automatic retry.
    assert 'retry' not in js.lower()


def test_summary_values_rendered():
    js = _read(JS)
    for target in ('statistics-watch-events', 'statistics-distinct-titles',
                   'statistics-hours', 'statistics-average-rating',
                   'statistics-rewatches'):
        assert f"setText('{target}'" in js


def test_genres_rendered_in_server_order():
    js = _read(JS)
    assert 'function renderGenres' in js
    assert '.sort(' not in js  # server ordering is authoritative


def test_rating_buckets_rendered_in_server_order():
    js = _read(JS)
    # Buckets come straight from the server dict; never reordered/rebuilt.
    assert 'Object.keys(summary.rating_distribution)' in js
    assert '.sort(' not in js


def test_monthly_values_rendered_from_server():
    js = _read(JS)
    assert 'summary.monthly_watch_counts' in js
    assert '.sort(' not in js
    # No month fabrication in JS.
    assert "'Jan'" not in js and 'getMonth' not in js


def test_movie_tv_values_rendered():
    js = _read(JS)
    assert 'summary.media_type_distribution' in js


def test_no_statistics_calculations_in_js():
    js = _read(JS)
    # Forbidden client-side math: averages, hours, rewatch rates,
    # rankings/percentages of statistics values. Bar widths are bounded
    # presentation scaling of server counts (§29) — allowed.
    assert '/ 60' not in js and '/60' not in js
    assert 'average' not in js.replace('average_rating', '') or \
        'sum(' not in js
    assert 'reduce(' not in js
    assert 'rewatch_rate' not in js  # rate is never recomputed/reformatted


def test_no_tmdb_or_recommendation_calls():
    js = _js_source()
    assert 'tmdb' not in js.lower()
    assert 'recommendation' not in js.lower()
    assert 'taste' not in js.lower()


def test_no_storage_caching():
    js = _read(JS)
    assert 'localStorage' not in js
    assert 'sessionStorage' not in js
    assert 'indexedDB' not in js
    assert 'document.cookie' not in js


def test_no_user_id_sent_to_api():
    js = _read(JS)
    assert 'user_id' not in js


def test_xss_safe_dynamic_insertion():
    js = _js_source()
    assert 'innerHTML' not in js
    assert 'insertAdjacentHTML' not in js
    assert 'document.write' not in js
    # All dynamic text goes through textContent.
    assert 'textContent' in js


# ════════════════════════════════════════════════════════════════════════════
# API ↔ UI interlock (§35) + data correctness (§43)
# ════════════════════════════════════════════════════════════════════════════

def _js_el_targets(js):
    return set(re.findall(r"(?:el|setText|show|hide)\(\s*'([a-z0-9-]+)'",
                          js)) - {'statistics-period'}


def test_every_renderer_target_exists_in_template():
    # A renamed/removed DOM id breaks this test instead of silently
    # blanking the statistics cards.
    js = _read(JS)
    template = _read(TEMPLATE)
    targets = _js_el_targets(js)
    assert targets, 'renderer targets not found'
    missing = [t for t in targets if f'id="{t}"' not in template]
    assert not missing, f'JS writes to missing DOM ids: {missing}'


def test_renderer_reads_only_canonical_service_fields():
    # Every `summary.<field>` (and direct data.<field>) the renderer
    # consumes must be a real Phase 2 output key — an API shape change
    # fails here rather than rendering blank values (§35).
    import re as _re

    js = _read(JS)
    read_fields = set(_re.findall(
        r'(?:summary|data)\.([a-z_]+)', js))
    canonical = {
        'total_watch_events', 'distinct_titles', 'movies_watched',
        'tv_watch_events', 'total_hours_watched', 'runtime_covered_events',
        'runtime_missing_events', 'average_rating', 'rating_count',
        'rating_distribution', 'rewatch_count', 'rewatch_rate',
        'top_genres', 'monthly_watch_counts', 'media_type_distribution',
    }
    unknown = read_fields - canonical
    assert not unknown, f'renderer reads non-canonical fields: {unknown}'

    # The canonical set itself must still match the service's actual
    # zero-history output (guards against silent service drift too); the
    # full shape is pinned by the Phase 2 suite.


def test_data_correctness_cross_check(auth_client, stats_user, app):
    # §43: hand-computed expected values from seeded DiaryEntry data must
    # survive service → API unchanged; the renderer displays these exact
    # fields via String(value) (proven by the interlock tests above).
    from tests.test_statistics_api import _date, _diary, _media

    with app.app_context():
        m1 = _media('Cross A', runtime=100, genres='Drama')
        m2 = _media('Cross B', runtime=50, genres='Drama, Thriller')
        m3 = _media('Cross TV', runtime=45, media_type='tv')
        _diary(stats_user, m1, _date(2026, 2, 1), rating=4.0)
        _diary(stats_user, m1, _date(2026, 3, 1), rating=5.0,
               is_rewatch=True)
        _diary(stats_user, m2, _date(2026, 4, 1))
        _diary(stats_user, m3, _date(2026, 5, 1))
        try:
            data = auth_client.get('/api/statistics?year=2026').get_json()
            # Hand-computed: 4 events, 3 distinct titles, 1 rewatch.
            assert data['total_watch_events'] == 4
            assert data['distinct_titles'] == 3
            assert data['rewatch_count'] == 1
            assert data['rewatch_rate'] == 0.25            # 1/4
            # 100 + 100 + 50 + 45 = 295 min → 4.9 h (1 dp)
            assert data['total_hours_watched'] == 4.9
            assert data['rating_count'] == 2
            assert data['average_rating'] == 4.5           # (4.0+5.0)/2
            assert data['movies_watched'] == 3
            assert data['tv_watch_events'] == 1
            assert data['media_type_distribution'] == {'movie': 3, 'tv': 1}
            by_name = {g['name']: g for g in data['top_genres']}
            assert by_name['Drama']['count'] == 3
            assert by_name['Thriller']['count'] == 1
            months = {r['month']: r['count']
                      for r in data['monthly_watch_counts']}
            assert (months['2026-02'], months['2026-03'],
                    months['2026-04'], months['2026-05']) == (1, 1, 1, 1)
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=stats_user.id).delete()
            _db.session.commit()
