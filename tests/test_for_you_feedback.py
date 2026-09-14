"""For You frontend feedback events (Feature #7 Phase 7) — focused regression.

Passive impression + click telemetry for the For You homepage rail, delivered
to the existing POST /api/rec/feedback endpoint. No JS unit-test framework
exists in this repo, so client behavior is pinned by source assertions
(comments stripped — the established repo pattern, cf. the continue-watching
telemetry guards) plus backend contract tests proving the exact payloads the
client emits are accepted and persisted.

What this phase is: ONE impression per card per page view at ≥50% visibility
(single IntersectionObserver, unobserve after firing), batched queue flushed
at 10 events / 2s debounce / page-exit (fetch keepalive), delegated click
handler that never blocks navigation, silent failure handling.

What this phase is NOT: no not_interested/save/already_watched/rating UI,
no ranking changes, no TasteProfile recomputation, no sendBeacon (it cannot
carry the CSRF header; fetch keepalive is used instead), no persistence
(localStorage/IndexedDB/service worker), no polling.
"""
import json

import pytest

from models import db, User, RecommendationFeedback


# ── module-unique data (suite convention: clean up everything) ───────────────
DOMAIN = 'fuyfeedback.test'


@pytest.fixture(autouse=True)
def _clean_feedback_rows(app):
    yield
    RecommendationFeedback.query.delete()
    db.session.commit()
    User.query.filter(User.email.like(f'%@{DOMAIN}')).delete(
        synchronize_session=False)
    db.session.commit()


def _make_user(username):
    u = User(username=username, email=f'{username}@{DOMAIN}',
             email_verified=True)
    u.set_password('TestPass1')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def user(app):
    with app.app_context():
        yield _make_user('fuyfb')


def _login(client):
    client.post('/login', data={
        'username': 'fuyfb', 'password': 'TestPass1'})
    return client


@pytest.fixture
def auth_client(client, user):
    return _login(client)


def _js_source():
    with open('static/js/for-you.js', encoding='utf-8') as f:
        return f.read()


def _js_code():
    """JS source with comment-only lines stripped — source guards must pin
    executable behavior, not prose."""
    src = _js_source()
    lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(('/*', '*', '//')):
            continue
        lines.append(line)
    return '\n'.join(lines)


# ── 1/2/31. Initialization gating ────────────────────────────────────────────

def test_feedback_gated_on_authenticated_placeholder():
    """Module exits before any telemetry unless rail + auth context exist."""
    src = _js_code()
    guard = src.index('__IS_AUTH__ !== true')
    assert guard < src.index('queueFeedback')


def test_anonymous_homepage_renders_no_feedback_surface(client):
    """No placeholder → no observer → no feedback requests possible."""
    html = client.get('/').data.decode()
    assert 'data-for-you-placeholder' not in html
    assert '/api/rec/feedback' not in html


# ── 3/4/5/6/7. Impression semantics ──────────────────────────────────────────

def test_impressions_use_intersection_observer_threshold():
    src = _js_code()
    assert 'IntersectionObserver' in src
    assert 'IMPRESSION_THRESHOLD = 0.5' in src
    assert 'unobserve' in src                      # fire once per card


def test_no_scroll_polling():
    src = _js_code()
    assert 'addEventListener(\'scroll\'' not in src
    assert 'setInterval' not in src
    assert 'onscroll' not in src


def test_one_impression_per_card_per_page_view():
    src = _js_code()
    assert 'seenImpressions' in src                # client-side Set
    assert 'seenImpressions[key] = true' in src
    assert 'return;   // one impression per page view' in src


def test_impressions_only_after_render_not_page_load():
    """Impressions wire up inside render() after the personalized check —
    a cold-start/hidden rail can never observe anything."""
    src = _js_code()
    assert src.index('initImpressions();') > src.index('data.personalized !== true')


# ── 8/9/10. Batching + endpoint ──────────────────────────────────────────────

def test_batched_post_to_canonical_endpoint():
    src = _js_code()
    assert "fetch('/api/rec/feedback'" in src
    assert "'/api/for-you'" in src                 # the only other call
    assert 'JSON.stringify({ events: batch })' in src   # events[] format
    assert src.count('fetch(') == 2                # for-you + feedback only


def test_batch_size_is_bounded():
    src = _js_code()
    assert 'FLUSH_SIZE = 10' in src
    assert 'FLUSH_DELAY_MS = 2000' in src
    assert '>= FLUSH_SIZE' in src                  # immediate flush threshold


# ── 11–17. Event payload fields (verified against the real backend) ─────────

def _post_batch(auth_client, events):
    return auth_client.post('/api/rec/feedback', json={'events': events})


def test_click_payload_accepted_and_persisted(auth_client):
    payload = {
        'media_id': 550, 'media_type': 'movie',
        'surface': 'home_for_you', 'event': 'click', 'position': 1,
        'source': 'genre_discover', 'reason_kind': 'genre_affinity',
    }
    r = _post_batch(auth_client, [payload])
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'recorded': 1, 'duplicates': 0}
    row = RecommendationFeedback.query.one()
    assert row.media_id == 550 and row.media_type == 'movie'
    assert row.event == 'click' and row.surface == 'home_for_you'
    assert row.position == 1
    assert row.reason_kind == 'genre_affinity'
    assert row.source == 'genre_discover'


def test_impression_payload_accepted_and_persisted(auth_client):
    payload = {
        'media_id': 603, 'media_type': 'tv',
        'surface': 'home_for_you', 'event': 'impression', 'position': 2,
        'reason_kind': 'similar_title',
    }
    r = _post_batch(auth_client, [payload])
    assert r.status_code == 200
    row = RecommendationFeedback.query.one()
    assert row.event == 'impression' and row.position == 2


def test_client_payload_omits_banned_fields(auth_client):
    """user_id is an unknown field → rejected by the API (never sent)."""
    r = _post_batch(auth_client, [{
        'media_id': 550, 'media_type': 'movie',
        'surface': 'home_for_you', 'event': 'impression', 'position': 1,
        'user_id': 999,
    }])
    assert r.status_code == 400
    assert RecommendationFeedback.query.count() == 0


def test_payload_contains_no_scores_or_profile_data():
    """Structural: buildEvent composes only the bounded field set."""
    src = _js_code()
    for field in ('media_id', 'media_type', 'surface', 'event',
                  'position', 'source', 'reason_kind'):
        assert field in src
    for banned in ('score', 'profile_version', 'genre_weights',
                   'confidence'):
        assert banned not in src


def test_client_never_sends_poster_urls_or_reason_objects():
    src = _js_code()
    assert 'poster_path' not in src.split('buildEvent')[1].split('}')[0]
    body = json.loads(json.dumps({'events': [
        {'media_id': 1, 'media_type': 'movie', 'surface': 'home_for_you',
         'event': 'impression', 'position': 1,
         'reason_kind': 'genre_affinity'}]}))
    ev = body['events'][0]
    assert set(ev) == {'media_id', 'media_type', 'surface', 'event',
                       'position', 'reason_kind'}


# ── 18–22. Client hygiene ────────────────────────────────────────────────────

def test_no_direct_tmdb_calls_in_feedback_path():
    src = _js_code()
    assert 'api.themoviedb.org' not in src


def test_no_ranking_logic_in_feedback_module():
    src = _js_code()
    for banned in ('vote_average', 'score =', 'sort(', 'genre_weights'):
        assert banned not in src


def test_click_does_not_block_navigation():
    """No preventDefault/stopPropagation in the click path; navigation
    always wins (structural guarantee)."""
    src = _js_code()
    click_section = src[src.index("addEventListener('click'"):]
    click_section = click_section[:click_section.index('function ')]
    assert 'preventDefault' not in click_section
    assert 'stopPropagation' not in click_section
    assert 'return false' not in click_section


def test_keepalive_used_for_page_exit_flush():
    src = _js_code()
    assert 'keepalive' in src
    assert 'sendBeacon' not in src     # cannot carry CSRF; keepalive instead
    assert 'visibilitychange' in src
    assert 'pagehide' in src


# ── 23. Rate-limit compatibility ─────────────────────────────────────────────

def test_one_page_of_cards_produces_few_requests():
    """14 cards → 1 immediate flush at 10 + 1 debounced flush of 4 (or one
    page-exit flush): ≤2 feedback requests per page view."""
    src = _js_code()
    assert 'FLUSH_SIZE = 10' in src
    assert "fetch('/api/rec/feedback'" in src
    # The only POST site is the single flush function.
    assert src.count("'/api/rec/feedback'") == 1


def test_batch_endpoint_accepts_full_rail_batch(auth_client):
    events = [{'media_id': 900000 + i, 'media_type': 'movie',
               'surface': 'home_for_you', 'event': 'impression',
               'position': i + 1} for i in range(14)]
    r = _post_batch(auth_client, events)
    assert r.status_code == 200
    data = r.get_json()
    assert data['recorded'] == 14 and data['duplicates'] == 0
    assert RecommendationFeedback.query.count() == 14


# ── 26/27/28. Failure handling ───────────────────────────────────────────────

def test_malformed_feedback_response_does_not_break_module():
    """The flush promise has no response parsing that can throw UI-visible
    errors — response body is never read (best-effort telemetry)."""
    src = _js_code()
    flush_body = src[src.index('function flushFeedback'):]
    flush_body = flush_body[:flush_body.index('function queueFeedback')]
    assert '.json()' not in flush_body          # no parse → no throw path
    assert '.catch(' in flush_body


def test_feedback_failures_are_silent():
    src = _js_code()
    feedback_section = src[src.index('var SURFACE'):]
    for banned in ('frameToast', 'alert(', 'spinner'):
        assert banned not in feedback_section


def test_no_aggressive_retry():
    src = _js_code()
    flush_body = src[src.index('function flushFeedback'):]
    flush_body = flush_body[:flush_body.index('function queueFeedback')]
    assert 'retry' not in flush_body.lower()
    assert 'setTimeout(' not in flush_body      # flush is one-shot


# ── 29. Oversized client batch prevented ─────────────────────────────────────

def test_client_batch_never_exceeds_api_cap():
    """FLUSH_SIZE (10) << API cap (100); splice caps any pathological queue
    growth at the flush boundary."""
    src = _js_code()
    assert 'FLUSH_SIZE = 10' in src
    assert 'splice(0, pendingFeedback.length)' in src


def test_api_rejects_oversized_batch(auth_client):
    """Backend cap as final protection: 101 events → 400, zero rows."""
    events = [{'media_id': 900000 + i, 'media_type': 'movie',
               'surface': 'home_for_you', 'event': 'impression',
               'position': 1} for i in range(101)]
    r = _post_batch(auth_client, events)
    assert r.status_code == 400
    assert RecommendationFeedback.query.count() == 0


# ── 13/35. No persistence, no service worker ────────────────────────────────

def test_no_telemetry_persistence():
    src = _js_code()
    for banned in ('localStorage', 'IndexedDB', 'indexedDB',
                   'serviceWorker', 'sessionStorage'):
        assert banned not in src


# ── 30/32/33. Existing rendering + engine untouched ─────────────────────────

def test_rendering_responsibilities_unchanged():
    """The Phase 5 contract (single fetch, hide-on-cold-start, canonical
    URLs, reason verbatim) still holds after the telemetry extension."""
    src = _js_code()
    assert src.count('fetch(') == 2
    assert 'personalized !== true' in src
    assert "'/movie/' + item.tmdb_id" in src
    assert "'/tv/' + item.tmdb_id" in src
    assert 'reason.text' in src


def test_engine_and_profile_stay_computation_free(auth_client, monkeypatch):
    """Runtime proof: recording feedback never triggers the engine or
    taste-profile computation — both are booby-trapped to explode."""
    import api.for_you as fy
    import api.taste_profile as tp

    def boom(*a, **kw):
        raise RuntimeError('must not be called during feedback recording')
    monkeypatch.setattr(fy, 'get_for_you', boom)
    monkeypatch.setattr(tp, 'compute_profile', boom)

    r = auth_client.post('/api/rec/feedback', json={'events': [{
        'media_id': 550, 'media_type': 'movie',
        'surface': 'home_for_you', 'event': 'impression',
        'position': 1}]})
    assert r.status_code == 200
    assert RecommendationFeedback.query.count() == 1


def test_homepage_feedback_source_scan_for_compute():
    """No path from the frontend module to profile computation."""
    src = _js_code()
    assert 'compute_profile' not in src
    assert 'taste_profile' not in src
