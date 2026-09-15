"""Taste DNA profile-page UI (Feature #6, Phase 14).

The profile page hosts the Taste DNA section; a small dedicated JS module
(static/js/taste-dna.js) renders it from the private presentation API.
Server-rendered HTML keeps only the section skeleton and a coarse
initial-state gate — never taste values (the JS is the only renderer and
the API is its only data source).

JS behavior is covered with source assertions (the repository has no JS
test framework); comments are stripped before forbidden-token checks so
documentation cannot create false positives.
"""
import re
import uuid

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(username):
    from models import User, db as _db

    u = User(username=username, email=f'{username}@example.com',
             email_verified=True)
    u.set_password('TestPass1')
    _db.session.add(u)
    _db.session.commit()
    return u


def _login(client, user):
    client.post('/login', data={
        'username': user.username, 'password': 'TestPass1'})
    return client


def _add_profile(uid, *, genres=None, confidence=0.7, titles=10):
    from models import TasteProfile, db as _db

    p = TasteProfile(user_id=uid)
    p.genre_weights = genres or {}
    p.confidence = confidence
    p.distinct_title_count = titles
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.fixture
def ui_user(app):
    with app.app_context():
        yield _make_user('ui' + uuid.uuid4().hex[:6])


@pytest.fixture
def ui_auth_client(client, ui_user):
    return _login(client, ui_user)


def _js_source():
    with open('static/js/taste-dna.js', encoding='utf-8') as fh:
        return fh.read()


def _js_code():
    """JS source with comments stripped (guard-safe)."""
    src = _js_source()
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
    src = re.sub(r'^\s*//.*$', '', src, flags=re.M)
    return src


# ── Section presence / privacy (spec §1, §2) ─────────────────────────────────

def test_authenticated_profile_has_taste_dna_section(ui_auth_client,
                                                     ui_user):
    html = ui_auth_client.get('/profile').get_data(as_text=True)
    assert 'id="taste-dna-section"' in html
    assert 'Taste DNA' in html


def test_anonymous_profile_redirects_no_private_data(client):
    r = client.get('/profile')
    assert r.status_code == 302
    assert b'Taste DNA' not in r.data


def test_cold_start_marker_for_user_without_profile(ui_auth_client,
                                                    ui_user):
    html = ui_auth_client.get('/profile').get_data(as_text=True)
    assert 'data-initial-state="cold_start"' in html


def test_available_marker_for_profile_user(ui_auth_client, ui_user):
    _add_profile(ui_user.id, genres={"Thriller": 0.9})
    html = ui_auth_client.get('/profile').get_data(as_text=True)
    assert 'data-initial-state="available"' in html


def test_no_raw_scores_in_html(ui_auth_client, ui_user):
    """The template renders the skeleton only — never taste values."""
    _add_profile(ui_user.id, genres={"Thriller": 0.9123, "Drama": 0.4})
    html = ui_auth_client.get('/profile').get_data(as_text=True)
    assert '0.9123' not in html
    assert 'genre_weights' not in html
    assert 'director_affinity' not in html


def test_no_internal_ids_in_html(ui_auth_client, ui_user):
    _add_profile(ui_user.id, genres={"Thriller": 0.9})
    html = ui_auth_client.get('/profile').get_data(as_text=True)
    assert 'profile_version' not in html
    assert 'signal_count' not in html
    assert 'user_id=' not in html.split('taste-dna-section')[1].split(
        '</div>', 1)[0]


# ── JS contract (spec §8–§11, source guards) ─────────────────────────────────

def test_js_fetches_only_the_private_api():
    code = _js_code()
    assert code.count("fetch(") == 1
    assert '/api/taste-profile' in code


def test_js_never_requests_with_user_id():
    code = _js_code()
    assert 'user_id' not in code
    assert 'username=' not in code


def test_js_no_polling():
    code = _js_code()
    assert 'setInterval' not in code
    assert 'setTimeout' not in code


def test_js_no_tmdb_calls():
    code = _js_code()
    assert 'tmdb' not in code.lower()
    assert 'themoviedb' not in code.lower()


def test_js_no_feedback_or_profile_json_access():
    code = _js_code()
    assert 'feedback' not in code.lower()
    assert 'genre_weights' not in code
    assert 'director_affinity' not in code
    assert 'signal_count' not in code


def test_js_no_strength_or_confidence_math():
    """Strengths/confidence are server-computed; JS only renders labels."""
    code = _js_code()
    assert 'confidence' not in code.lower()
    assert 'Math.round' not in code
    assert 'Math.max' not in code


def test_js_renders_server_reason_only():
    """The renderer prints names/strengths as given — no construction of
    taste claims from parsed values."""
    code = _js_code()
    assert 'textContent' in code      # rendered as text, never innerHTML
    assert 'innerHTML' not in code    # no HTML injection of API data


def test_js_hides_section_on_failure():
    code = _js_code()
    assert 'hideSection' in code
    assert '.catch(' in code


def test_js_cold_start_state():
    code = _js_code()
    assert 'renderColdStart' in code
    assert "available" in code


# ── Existing profile page remains intact (spec §12) ──────────────────────────

def test_existing_profile_sections_intact(ui_auth_client, ui_user):
    html = ui_auth_client.get('/profile').get_data(as_text=True)
    assert 'DIARY' in html          # quick stats strip
    assert 'WATCHLIST' in html
    assert 'profile-page.js' in html


def test_profile_route_no_longer_has_duplicate_genre_counter():
    """The old duplicated Counter-based Taste DNA logic is replaced by the
    canonical presentation model (comments stripped so documentation
    cannot false-positive)."""
    with open('routes/auth.py', encoding='utf-8') as fh:
        src = fh.read()
    profile_block = src.split("def profile()")[1].split("\ndef ")[0]
    code = re.sub(r'#.*$', '', profile_block, flags=re.M)
    assert 'Counter' not in code
    assert 'taste_genres' not in code
    assert 'taste_dna' in code


def test_template_loads_taste_dna_script_once():
    with open('templates/profile.html', encoding='utf-8') as fh:
        html = fh.read()
    assert html.count('js/taste-dna.js') == 1
