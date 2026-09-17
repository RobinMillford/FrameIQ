"""Year-in-Review experience (Feature #8, Phase 8) — focused suite.

Covers the private recap stack end to end, on top of the Phase 4
canonical transformation (tests/test_year_in_review.py):

Builder (api.year_in_review):
- additive passthroughs (people/season_quality/daily_activity) are
  verbatim canonical values inside the SAME single get_statistics() call
- empty model unchanged; no tv_completion; actors stay []

API (GET /api/year-in-review):
- private/session-scoped adapter; invalid years → clean 400 BEFORE the
  builder runs; Cache-Control: private, no-store; exactly one
  build_year_in_review() call and exactly one get_statistics() call per
  request; no direct DiaryEntry/MediaItem access in the route; user
  isolation; deterministic response

UI (templates/year_in_review.html + static/js/year-in-review.js):
- one fetch on load, one per explicit year change, no polling, no
  /api/statistics calls; canonical wording verbatim; safe DOM insertion;
  accessibility labels; no completion UI; no ID exposure
"""
import json
import re
import socket
import uuid
from datetime import date
from unittest.mock import patch

import pytest


# ── Fixtures (statistics-suite conventions) ─────────────────────────────────

def _make_user(username):
    from models import User, db as _db
    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    return u


@pytest.fixture
def user(app):
    with app.app_context():
        yield _make_user('p8a' + uuid.uuid4().hex[:6])


@pytest.fixture
def other_user(app):
    with app.app_context():
        yield _make_user('p8b' + uuid.uuid4().hex[:6])


def _media(title, runtime=90, genres=None, media_type='movie'):
    from models import MediaItem, db as _db
    m = MediaItem(tmdb_id=9_800_000 + uuid.uuid4().int % 900_000,
                  media_type=media_type, title=title, runtime=runtime,
                  genres=genres)
    _db.session.add(m)
    _db.session.commit()
    return m


def _diary(user, media, watched_date, rating=None, is_rewatch=False):
    from models import DiaryEntry, db as _db
    e = DiaryEntry(user_id=user.id, media_id=media.id,
                   media_type=media.media_type,
                   watched_date=watched_date, rating=rating,
                   is_rewatch=is_rewatch)
    _db.session.add(e)
    _db.session.commit()
    return e


@pytest.fixture
def auth_client(client, user):
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1'})
    return client


TEMPLATE = 'templates/year_in_review.html'
JS = 'static/js/year-in-review.js'


def _read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _js_code():
    """JS source with block comments stripped (prose mentions of banned
    APIs must not trip the guards — only real code matches)."""
    return re.sub(r'/\*.*?\*/', '', _read(JS), flags=re.S)


# ════════════════════════════════════════════════════════════════════════════
# Builder: additive canonical passthroughs (§42)
# ════════════════════════════════════════════════════════════════════════════

def test_builder_passthrough_fields_verbatim(user, app):
    from api.year_in_review import build_year_in_review
    import api.statistics as service
    with app.app_context():
        m = _media('PT', runtime=120, genres='Drama')
        _diary(user, m, date(2026, 3, 3), rating=4.0)
        stats = service.get_statistics(user.id, year=2026)
        recap = build_year_in_review(user.id, 2026)
    # The story model carries the SAME single-call canonical values.
    assert recap['people']['directors'] == stats['directors']
    assert recap['people']['actors'] == stats['actors']
    assert recap['season_quality'] == stats['season_quality']
    assert recap['daily_activity'] == stats['daily_activity']


def test_builder_still_one_statistics_call(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('CallCount', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        with patch('api.year_in_review.get_statistics',
                   wraps=__import__(
                       'api.year_in_review',
                       fromlist=['get_statistics']).get_statistics) as spy:
            build_year_in_review(user.id, 2026)
    assert spy.call_count == 1


def test_builder_empty_model_unchanged(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        recap = build_year_in_review(user.id, 2026)
    assert recap == {'year': 2026, 'available': False, 'state': 'empty'}


def test_builder_actors_remain_unavailable(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('Actors', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        recap = build_year_in_review(user.id, 2026)
    assert recap['people']['actors'] == []


def test_builder_no_tv_completion_field(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('NoComp', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        recap = build_year_in_review(user.id, 2026)
    assert 'tv_completion' not in recap


def test_builder_no_ids_exposed(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('P8Ids', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        media_id = m.id
        recap = build_year_in_review(user.id, 2026)

        def _walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    assert not (key.lower().endswith('_id')
                                or key.lower() == 'id'), key
                    yield from _walk(value)
            elif isinstance(node, list):
                for value in node:
                    yield from _walk(value)
            else:
                yield node

        assert media_id not in list(_walk(recap))
        assert user.id not in list(_walk(recap))


def test_builder_private_identity_not_exposed(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('P8Privacy', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        recap = build_year_in_review(user.id, 2026)
    blob = json.dumps(recap)
    assert user.username not in blob
    assert user.email not in blob


def test_builder_no_network(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('P8Net', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        with patch.object(socket.socket, '__init__',
                          side_effect=AssertionError('network attempted')), \
             patch.object(socket.socket, 'connect',
                          side_effect=AssertionError('network attempted')), \
             patch.object(socket.socket, 'connect_ex',
                          side_effect=AssertionError('network attempted')):
            recap = build_year_in_review(user.id, 2026)
    assert recap['available'] is True


def test_builder_deterministic(user, app):
    from api.year_in_review import build_year_in_review
    with app.app_context():
        m = _media('P8Det', runtime=90, genres='Drama')
        _diary(user, m, date(2026, 3, 3), rating=4.0)
        first = build_year_in_review(user.id, 2026)
        second = build_year_in_review(user.id, 2026)
    assert json.dumps(first, sort_keys=True) == \
        json.dumps(second, sort_keys=True)


# ════════════════════════════════════════════════════════════════════════════
# API: GET /api/year-in-review (§43)
# ════════════════════════════════════════════════════════════════════════════

def test_api_anonymous_rejected(client):
    assert client.get('/api/year-in-review?year=2026').status_code \
        in (302, 401)


def test_api_authenticated_success(auth_client):
    r = auth_client.get('/api/year-in-review?year=2026')
    assert r.status_code == 200
    assert r.content_type.startswith('application/json')
    assert r.get_json()['state'] == 'empty'


def test_api_empty_year_canonical_shape(auth_client):
    body = auth_client.get('/api/year-in-review?year=2026').get_json()
    assert body == {'year': 2026, 'available': False, 'state': 'empty'}


def test_api_populated_year(auth_client, user, app):
    with app.app_context():
        m = _media('Pop', runtime=90)
        _diary(user, m, date(2026, 3, 3), rating=4.0)
    body = auth_client.get('/api/year-in-review?year=2026').get_json()
    assert body['available'] is True
    assert body['summary']['total_watch_events'] == 1
    assert body['runtime']['complete'] is True


def test_api_missing_year_uses_current_year_default(auth_client):
    from datetime import datetime
    body = auth_client.get('/api/year-in-review').get_json()
    assert body['year'] == datetime.now().year


def test_api_malformed_year_clean_400(auth_client):
    for bad in ('abcd', '20.5', '%3Cscript%3E'):
        r = auth_client.get(f'/api/year-in-review?year={bad}')
        assert r.status_code == 400, bad
        assert 'error' in r.get_json()
        assert b'Traceback' not in r.data


def test_api_future_year_clean_400(auth_client):
    future = date.today().year + 1
    r = auth_client.get(f'/api/year-in-review?year={future}')
    assert r.status_code == 400


def test_api_invalid_year_never_reaches_statistics(auth_client):
    # §5: invalid years are rejected before any data work — the canonical
    # statistics service itself must never be invoked.
    import api.year_in_review as builder_module
    with patch.object(builder_module, 'get_statistics') as spy:
        r = auth_client.get('/api/year-in-review?year=1899')
    assert r.status_code == 400
    spy.assert_not_called()


def test_api_builder_called_exactly_once(auth_client, user, app):
    with app.app_context():
        m = _media('Once', runtime=90)
        _diary(user, m, date(2026, 3, 3))
    with patch('routes.statistics.year_in_review_service'
               '.build_year_in_review',
               wraps=__import__('routes.statistics', fromlist=['x'])
               .year_in_review_service.build_year_in_review) as spy:
        r = auth_client.get('/api/year-in-review?year=2026')
    assert r.status_code == 200
    assert spy.call_count == 1


def test_api_one_get_statistics_call_per_request(auth_client, user, app):
    import api.year_in_review as builder_module
    with app.app_context():
        m = _media('OneStat', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        calls = []
        original = builder_module.get_statistics

        def _spy(user_id, *args, **kwargs):
            calls.append(user_id)
            return original(user_id, *args, **kwargs)

        with patch.object(builder_module, 'get_statistics', _spy):
            r = auth_client.get('/api/year-in-review?year=2026')
    assert r.status_code == 200
    assert len(calls) == 1


def test_api_route_has_no_direct_model_queries(auth_client):
    # §9: the route is a pure adapter — the route module must not import
    # or reference the models/tables it must never touch.
    import inspect
    import routes.statistics as route
    source = inspect.getsource(route)
    assert 'DiaryEntry' not in source
    assert 'MediaItem' not in source
    assert 'tmdb' not in source.lower()
    assert 'for_you' not in source
    assert 'taste_profile' not in source
    assert 'RecommendationFeedback' not in source


def test_api_private_cache_headers(auth_client):
    r = auth_client.get('/api/year-in-review?year=2026')
    assert r.headers.get('Cache-Control') == 'private, no-store'


def test_api_user_isolation(auth_client, other_user, app):
    with app.app_context():
        m = _media('BOnly', runtime=90)
        _diary(other_user, m, date(2026, 3, 3))
        try:
            body = auth_client.get(
                '/api/year-in-review?year=2026').get_json()
            assert body['state'] == 'empty'
        finally:
            from models import DiaryEntry, db as _db
            DiaryEntry.query.filter_by(user_id=other_user.id).delete()
            _db.session.commit()


def test_api_deterministic_repeated_response(auth_client, user, app):
    with app.app_context():
        m = _media('Rep', runtime=90, genres='Drama')
        _diary(user, m, date(2026, 3, 3), rating=4.0)
    first = auth_client.get('/api/year-in-review?year=2026').get_json()
    second = auth_client.get('/api/year-in-review?year=2026').get_json()
    assert json.dumps(first, sort_keys=True) == \
        json.dumps(second, sort_keys=True)


def test_api_no_network(auth_client, user, app):
    with app.app_context():
        m = _media('RouteNet', runtime=90)
        _diary(user, m, date(2026, 3, 3))
        with patch.object(socket.socket, '__init__',
                          side_effect=AssertionError('network attempted')), \
             patch.object(socket.socket, 'connect',
                          side_effect=AssertionError('network attempted')), \
             patch.object(socket.socket, 'connect_ex',
                          side_effect=AssertionError('network attempted')):
            r = auth_client.get('/api/year-in-review?year=2026')
    assert r.status_code == 200


def test_api_error_response_leaks_no_internals(auth_client, monkeypatch):
    import routes.statistics as route

    def _boom(*args, **kwargs):
        raise RuntimeError('SECRET INTERNAL DETAIL')

    monkeypatch.setattr(
        route.year_in_review_service, 'build_year_in_review', _boom)
    r = auth_client.get('/api/year-in-review?year=2026')
    assert r.status_code == 500
    assert b'SECRET INTERNAL DETAIL' not in r.data


# ════════════════════════════════════════════════════════════════════════════
# UI: recap page source + interlock (§44)
# ════════════════════════════════════════════════════════════════════════════

def test_ui_template_shell_present():
    template = _read(TEMPLATE)
    for target in ('yir-year', 'yir-year-select', 'yir-loading',
                   'yir-error', 'yir-empty', 'yir-content',
                   'yir-watch-events', 'yir-distinct-titles', 'yir-hours',
                   'yir-average-rating', 'yir-highlights', 'yir-monthly',
                   'yir-media', 'yir-heatmap', 'yir-genres',
                   'yir-directors', 'yir-seasons', 'yir-runtime'):
        assert f'id="{target}"' in template, target


def test_ui_js_included_exactly_once():
    assert _read(TEMPLATE).count('js/year-in-review.js') == 1


def test_ui_one_initial_fetch_and_one_per_change():
    js = _js_code()
    assert js.count('fetch(') == 1
    assert 'setInterval' not in js
    assert 'setTimeout' not in js
    # Only the recap endpoint is the data source — never /api/statistics.
    assert '/api/year-in-review' in js
    assert '/api/statistics' not in js


def test_ui_no_tmdb_or_recommendation_calls():
    js = _js_code().lower()
    assert 'tmdb' not in js
    assert 'recommendation' not in js
    assert 'taste' not in js


def test_ui_no_storage_caching():
    js = _js_code()
    assert 'localStorage' not in js
    assert 'sessionStorage' not in js
    assert 'indexedDB' not in js


def test_ui_no_user_id_sent():
    js = _js_code()
    assert 'user_id' not in js


def test_ui_xss_safe_dynamic_insertion():
    js = _js_code()
    assert 'innerHTML' not in js
    assert 'insertAdjacentHTML' not in js
    assert 'document.write' not in js
    assert 'textContent' in js
    assert 'createElement' in js


def test_ui_no_independent_statistics_calculations():
    js = _js_code()
    assert 'reduce(' not in js
    assert '/ 60' not in js and '/60' not in js
    assert 'rewatch_rate' not in js
    assert 'top_genres' not in js   # genres come from the recap model
    assert 'getMonth' not in js and 'getFullYear' not in js
    assert 'Date.now' not in js


def test_ui_renders_canonical_wording_verbatim():
    # Highlight/runtime sentences must be displayed exactly as the
    # server provides them — never recomputed client-side.
    js = _js_code()
    assert 'item.text' in js        # highlight sentence passthrough
    assert 'runtime.text' in js     # canonical runtime wording


def test_ui_accessibility_labels_present():
    js = _js_code()
    assert 'aria-label' in js
    assert "role', 'listitem'" in js or "role\", \"listitem\"" in js \
        or "listitem" in js


def test_ui_no_completion_ui():
    blob = _js_code().lower() + _read(TEMPLATE).lower()
    assert 'tv-completion' not in blob
    assert 'completion-rate' not in blob
    assert 'completed episodes' not in blob


def test_ui_interlock_targets_exist():
    js = _js_code()
    template = _read(TEMPLATE)
    targets = set(re.findall(r"(?:el|setText|show|hide|clearList)\(\s*'"
                             r"([a-z0-9-]+)'", js)) - {'yir-year-select'}
    assert targets, 'renderer targets not found'
    missing = [t for t in targets if f'id="{t}"' not in template]
    assert not missing, f'JS writes to missing DOM ids: {missing}'


def test_ui_renderer_reads_only_recap_model_fields():
    import api.year_in_review as yir
    with open(yir.__file__, encoding='utf-8') as fh:
        module = fh.read()
    # Every top-level `data.<field>` the renderer reads must exist in
    # the recap model's construction (an API shape change fails here).
    js = _js_code()
    read_fields = set(re.findall(r'data\.([a-z_]+)', js))
    for field in read_fields:
        assert f'"{field}"' in module or f"'{field}'" in module, field


def test_ui_no_ids_in_renderer():
    js = _js_code()
    for banned in ('media_id', 'show_id', 'tmdb_id', 'person_id'):
        assert banned not in js, banned


def test_ui_page_not_publicly_cacheable_template_has_no_private_values():
    # §25-style shell check: the recap template must not server-render
    # any statistics values — they arrive via authenticated fetch.
    template = _read(TEMPLATE)
    assert '{{ year|tojson }}' in template
    assert 'total_watch_events' not in template
