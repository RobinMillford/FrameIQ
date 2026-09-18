"""Explicit For You feedback controls (Feature #7 Phase 16) — focused suite.

Per-card "⋯" menu on For You rail cards with two actions:

- Not interested → event=not_interested via the existing POST /api/rec/feedback
  batch path, flushed promptly; the card is dismissed from the rail (UI only —
  no database deletion, no watchlist/diary/TasteProfile mutation, no
  replacement recommendation fetch).
- Save → the canonical /add_to_watchlist persistence is invoked FIRST; the
  saved learning event is recorded ONLY after it succeeds. No new save
  endpoint, no parallel persistence path.

No JS unit-test framework exists in this repo, so client behavior is pinned by
the established comment-stripped source-guard pattern (see
tests/test_for_you_feedback.py) plus backend contract tests proving the exact
payloads the controls emit are accepted and persisted.

What this phase is NOT: no rating/already-watched controls, no new feedback
events, no ranking changes, no synchronous TasteProfile recomputation, no new
API endpoints, no localStorage/IndexedDB/service-worker persistence, no
polling, no second feedback client.
"""
import pytest
from sqlalchemy import select

from api.taste_profile import FEEDBACK_EVENT_WEIGHTS
from models import db, User, RecommendationFeedback, user_watchlist
from models.recommendation_feedback import EVENTS


# ── module-unique data (suite convention: clean up everything) ───────────────
DOMAIN = 'fuyexplicit.test'


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
        yield _make_user('fuyexp')


def _login(client):
    client.post('/login', data={
        'username': 'fuyexp', 'password': 'TestPass1'})
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


# ── 1/2. Menu rendering + cold-start gating ──────────────────────────────────

def test_action_menu_attached_to_personalized_cards():
    """buildActionMenu is invoked from card(), which only runs after the
    personalized check inside render()."""
    src = _js_code()
    card_idx = src.index('function card(')
    menu_idx = src.index('buildActionMenu(item, position));')
    assert card_idx < menu_idx < src.index('function render(')


def test_controls_initialized_only_after_personalized_render():
    """Cold-start (personalized !== true → hide()) can never wire the
    controls: initialization happens inside render() only."""
    src = _js_code()
    assert src.index('data.personalized !== true') \
        < src.index('initActionBehavior();') \
        < src.index("fetch('/api/for-you'")


def test_anonymous_homepage_has_no_controls(client):
    """No placeholder rendered → no cards → no action menu possible."""
    html = client.get('/').data.decode()
    assert 'data-for-you-placeholder' not in html
    assert 'data-rec-actions' not in html
    assert '/api/rec/feedback' not in html


# ── 4–13. Event payloads (verified against the real backend) ────────────────

def _post_event(auth_client, event, **extra):
    payload = {
        'media_id': 550, 'media_type': 'movie',
        'surface': 'home_for_you', 'event': event, 'position': 3,
    }
    payload.update(extra)
    return auth_client.post('/api/rec/feedback', json={'events': [payload]})


def test_not_interested_payload_persisted(auth_client):
    r = _post_event(auth_client, 'not_interested',
                    reason_kind='genre_affinity', source='genre_discover')
    assert r.status_code == 200
    row = RecommendationFeedback.query.one()
    assert row.event == 'not_interested'
    assert row.media_id == 550 and row.media_type == 'movie'
    assert row.surface == 'home_for_you' and row.position == 3
    assert row.reason_kind == 'genre_affinity' and row.source == 'genre_discover'


def test_saved_payload_persisted(auth_client):
    r = _post_event(auth_client, 'saved',
                    reason_kind='genre_affinity', source='genre_discover')
    assert r.status_code == 200
    row = RecommendationFeedback.query.one()
    assert row.event == 'saved'
    assert row.media_id == 550 and row.media_type == 'movie'
    assert row.surface == 'home_for_you' and row.position == 3


def test_duplicate_not_interested_is_idempotent_same_day(auth_client):
    payload = {'events': [{
        'media_id': 550, 'media_type': 'movie',
        'surface': 'home_for_you', 'event': 'not_interested',
        'position': 1}]}
    assert auth_client.post('/api/rec/feedback', json=payload).status_code \
        == 200
    second = auth_client.post('/api/rec/feedback', json=payload)
    assert second.status_code == 200
    assert second.get_json()['duplicates'] == 1
    assert RecommendationFeedback.query.count() == 1


def test_client_emits_only_minimum_fields():
    """Structural: the control events reuse buildEvent — the bounded field
    set with no user_id, scores, or profile data."""
    src = _js_code()
    assert "buildEvent(item, 'not_interested', position)" in src
    assert "buildEvent(item, 'saved', position)" in src
    for banned in ('user_id', 'score', 'profile_version',
                   'genre_weights', 'confidence'):
        assert banned not in src


def test_reason_kind_and_source_forwarded_when_present():
    """buildEvent forwards source / reason.kind only when the card carries
    them (set at render time from the API response)."""
    src = _js_code()
    body = src[src.index('function buildEvent'):]
    assert 'if (item.source) ev.source = item.source;' in body
    assert "if (item.reason && item.reason.kind)" in body


# ── 14/5. Card removal semantics ─────────────────────────────────────────────

def test_not_interested_flushes_then_removes_card():
    """The dismissal event is flushed immediately and the card removed from
    the rail (UI dismissal — no refill, no section reload)."""
    src = _js_code()
    body = src[src.index('function notInterested'):src.index(
        'function saveCard')]
    assert body.index('flushFeedback(false);') \
        < body.index("removeChild(card);")


def test_save_keeps_card_visible():
    """Saved recommendations remain in the rail — markSaved never removes
    or hides the section."""
    src = _js_code()
    body = src[src.index('function markSaved'):src.index(
        'function buildActionMenu')]
    assert 'removeChild' not in body
    assert 'hide()' not in body


# ── 16/17/18. Canonical save ordering ────────────────────────────────────────

def test_save_uses_canonical_watchlist_endpoint():
    src = _js_code()
    assert "SAVE_URL_BASE = '/add_to_watchlist/'" in src
    assert 'SAVE_URL_BASE + item.tmdb_id' in src
    for banned in ('/api/for-you/save', '/api/for-you/not-interested',
                   '/api/save', '/api/watchlist/add'):
        assert banned not in src


def test_saved_feedback_only_after_canonical_save_succeeds():
    """Within saveCard: the success check (res.ok) precedes the feedback
    enqueue, and the failure branch records nothing."""
    src = _js_code()
    body = src[src.index('function saveCard'):src.index(
        'function markSaved')]
    ok_idx = body.index('res.ok')
    qf_idx = body.index('queueFeedback')
    catch_idx = body.index('.catch(function () {')
    assert ok_idx < qf_idx < catch_idx
    tail = body[catch_idx:]
    assert 'queueFeedback' not in tail
    assert 'buildEvent' not in tail


# ── 6/19. Double-submission protection ───────────────────────────────────────

def test_one_active_action_per_card():
    src = _js_code()
    body = src[src.index('function markBusy'):src.index(
        'function releaseBusy')]
    assert 'data-rec-action-busy' in body
    assert 'return false' in body


# ── 20/21. Request budget ────────────────────────────────────────────────────

def test_no_second_for_you_fetch_or_refill():
    src = _js_code()
    assert src.count("fetch('/api/for-you'") == 1


def test_no_replacement_recommendation_call():
    src = _js_code()
    assert src.count('fetch(') == 3   # for-you + feedback + canonical save
    assert '/api/for-you/not-interested' not in src
    assert '/api/for-you/save' not in src


def test_single_feedback_endpoint_usage():
    src = _js_code()
    assert src.count("'/api/rec/feedback'") == 1
    assert src.count('/api/rec/') == 1


# ── 22/12. No recomputation, no cross-user data ──────────────────────────────

def test_no_compute_profile_reference_in_client():
    src = _js_code()
    assert 'compute_profile' not in src
    assert 'taste_profile' not in src


def test_feedback_events_never_recompute_or_retrieve(auth_client,
                                                     monkeypatch):
    """Runtime proof: recording explicit events never triggers the engine,
    taste-profile computation, or any recommendation retrieval."""
    import api.for_you as fy
    import api.taste_profile as tp

    def boom(*a, **kw):
        raise RuntimeError('must not be called during feedback recording')
    monkeypatch.setattr(fy, 'get_for_you', boom)
    monkeypatch.setattr(tp, 'compute_profile', boom)

    for event in ('not_interested', 'saved'):
        r = _post_event(auth_client, event)
        assert r.status_code == 200
    assert RecommendationFeedback.query.count() == 2


# ── 23/29/40. Canonical state stays orthogonal to feedback ───────────────────

def test_not_interested_does_not_touch_watchlist(auth_client, user):
    """The negative preference lives only in RecommendationFeedback — the
    canonical watchlist stays untouched. Scoped to the fixture user:
    other suites legitimately write watchlist rows on the shared test DB,
    so a whole-table assertion would be order-dependent."""
    _post_event(auth_client, 'not_interested')
    rows = db.session.execute(
        select(user_watchlist.c.user_id).where(
            user_watchlist.c.user_id == user.id)).fetchall()
    assert rows == []
    assert RecommendationFeedback.query.count() >= 1


def test_feedback_api_still_validates_events(auth_client):
    r = _post_event(auth_client, 'bogus_event')
    assert r.status_code == 400
    assert RecommendationFeedback.query.count() == 0


def test_feedback_api_rejects_anonymous(client):
    r = _post_event(client, 'not_interested')
    assert r.status_code in (401, 302)
    assert RecommendationFeedback.query.count() == 0


# ── 24/25/26/27. Passive telemetry + batching intact ─────────────────────────

def test_existing_impression_mechanics_intact():
    src = _js_code()
    assert 'IMPRESSION_THRESHOLD = 0.5' in src
    assert 'IntersectionObserver' in src and 'unobserve' in src
    assert 'seenImpressions[key] = true' in src


def test_existing_click_mechanics_intact():
    src = _js_code()
    clicks = src[src.index('function initClicks'):src.index(
        'function itemFromCard')]
    assert "'click'" in clicks
    assert 'flushOnPageExit();' in clicks
    assert 'preventDefault' not in clicks


def test_batching_infrastructure_unchanged():
    src = _js_code()
    assert 'FLUSH_SIZE = 10' in src and 'FLUSH_DELAY_MS = 2000' in src
    assert 'pendingFeedback' in src
    assert 'visibilitychange' in src and 'pagehide' in src
    assert 'keepalive' in src and 'sendBeacon' not in src


def test_explicit_actions_flush_promptly():
    """not_interested flushes immediately; save flushes inside the success
    continuation — neither waits for the 2s debounce."""
    src = _js_code()
    ni = src[src.index('function notInterested'):src.index(
        'function saveCard')]
    assert 'flushFeedback(false);' in ni
    sv = src[src.index('function saveCard'):src.index(
        'function markSaved')]
    assert 'flushFeedback(false);' in sv


# ── 28. Failure handling ─────────────────────────────────────────────────────

def test_feedback_failure_is_silent_and_non_blocking():
    """The batch POST stays fire-and-forget: a .catch no-op, no retry, no
    user-visible error surface."""
    src = _js_code()
    flush = src[src.index('function flushFeedback'):src.index(
        'function queueFeedback')]
    assert '.catch(function () {' in flush
    assert 'retry' not in flush.lower()


def test_save_failure_keeps_card_and_allows_retry():
    src = _js_code()
    body = src[src.index('function saveCard'):src.index(
        'function markSaved')]
    catch_tail = body[body.index('.catch(function () {'):]
    assert 'releaseBusy(card);' in catch_tail


# ── 30–35. Accessibility + interaction isolation ─────────────────────────────

def test_menu_accessibility_labels():
    src = _js_code()
    assert "'aria-label', 'More options for ' + item.title" in src
    assert 'aria-haspopup' in src
    assert 'aria-expanded' in src
    assert "'role', 'menu'" in src
    assert "'role', 'menuitem'" in src


def test_keyboard_escape_closes_menu():
    src = _js_code()
    assert "e.key === 'Escape'" in src
    assert 'closeOpenMenus();' in src
    assert 'trigger.focus();' in src


def test_outside_click_closes_menu():
    src = _js_code()
    assert "if (!e.target.closest('[data-rec-actions]')) closeOpenMenus();" \
        in src


def test_mobile_usable_no_hover_gate():
    src = _js_code()
    assert 'h-7 w-7' in src                          # ~28px touch target
    assert "trigger.addEventListener('click'" in src  # opens on tap
    assert "addEventListener('mouseover'" not in src


def test_action_menu_click_does_not_navigate():
    """Two isolations: the delegated card-click handler ignores activations
    inside [data-rec-actions], and the trigger stops propagation."""
    src = _js_code()
    clicks = src[src.index('function initClicks'):src.index(
        'function itemFromCard')]
    assert "e.target.closest('[data-rec-actions]')" in clicks
    assert 'return;' in clicks
    assert 'preventDefault' not in clicks
    trigger = src[src.index("trigger.addEventListener('click'"):]
    assert 'e.stopPropagation();' in trigger


def test_card_links_keep_canonical_navigation():
    src = _js_code()
    assert "data-rec-link', '1'" in src
    assert "'/movie/' + item.tmdb_id" in src
    assert "'/tv/' + item.tmdb_id" in src


def test_card_removal_moves_focus_to_next_card():
    src = _js_code()
    body = src[src.index('function focusAfterRemoval'):src.index(
        'function notInterested')]
    assert 'link.focus();' in body
    assert 'container.focus();' in body


# ── 36–39. Client hygiene ────────────────────────────────────────────────────

def test_no_direct_tmdb_calls():
    src = _js_code()
    assert 'api.themoviedb.org' not in src


def test_no_client_persistence():
    src = _js_code()
    for banned in ('localStorage', 'sessionStorage', 'IndexedDB',
                   'indexedDB', 'serviceWorker'):
        assert banned not in src


def test_no_polling():
    src = _js_code()
    assert 'setInterval' not in src
    assert "'scroll'" not in src


def test_no_ranking_logic_in_client():
    src = _js_code()
    for banned in ('vote_average', 'score =', 'sort(', 'candidate',
                   'ranking'):
        assert banned not in src


# ── 41/42/43. Backend/wider-system invariants ────────────────────────────────

def test_profile_event_weights_cover_the_new_events():
    """The nightly pipeline already consumes not_interested/saved — the
    weight map must still cover every event (unchanged contract)."""
    assert set(FEEDBACK_EVENT_WEIGHTS) == set(EVENTS)
    assert FEEDBACK_EVENT_WEIGHTS['not_interested'] < 0
    assert FEEDBACK_EVENT_WEIGHTS['saved'] > 0


def test_nightly_workflow_untouched():
    with open('.github/workflows/taste-profile-nightly.yml',
              encoding='utf-8') as f:
        yml = f.read()
    assert 'enrich_directors.py' in yml
    assert 'compute_taste_profiles.py' in yml
    assert 'not_interested' not in yml


def test_no_legacy_taste_references():
    src = _js_code()
    for banned in ('user_taste_profile', 'user_similarity'):
        assert banned not in src


def test_not_interested_never_queries_recommendation_surfaces():
    """The module only POSTs feedback — no GET/query surface exists."""
    src = _js_code()
    assert "method: 'POST'" in src
    assert "method: 'GET'" not in src
