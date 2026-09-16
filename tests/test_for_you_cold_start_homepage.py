"""For You cold-start / homepage behavior (production audit).

Two production defects are pinned here:

1. PERMANENT SKELETONS — for-you.js is loaded at the end of the content
   block, but window.__IS_AUTH__ was only defined near the end of <body>.
   The module's auth gate (`__IS_AUTH__ !== true`) evaluated against an
   undefined flag, so the module exited BEFORE fetching and the server
   skeleton placeholder stayed visible forever. The flag now ships in
   <head>; these tests pin the document-order contract.

2. HTTP-LAYER CACHE ISOLATION — /api/for-you is a per-user personalized
   payload and must be Cache-Control: no-store so a browser/CDN can never
   replay an old personalized response to a now-ineligible user.

Also covers the PART 11 title ladder (1–4 titles never personalize even
with above-gate confidence) and the PART 12 render/hide source contract.
"""
from tests.test_for_you_personalization_gate import (
    _fresh_call,
    _profile,
    _stub_external,
)

import uuid

import pytest

from models import TasteProfile, db

DOMAIN = "example.invalid"


@pytest.fixture
def user(app):
    """Module-local user (the gate suite's fixture is module-scoped)."""
    with app.app_context():
        u = _user()
        yield u
        TasteProfile.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
        db.session.commit()


def _user(prefix="fycs"):
    from models import User
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:8]}@{DOMAIN}",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


# ════════════════════════════════════════════════════════════════════════════
# PART 11 — cold-start title ladder (gate: titles >= 5 AND confidence >= 0.4)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("titles", [1, 2, 3, 4])
def test_one_to_four_titles_never_personalized(app, user, monkeypatch,
                                               titles):
    """Above-gate confidence with <5 distinct titles is still ineligible —
    and the wired trending stub proves no generic candidates leak out."""
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.8, titles=titles,
                                signals=titles * 2))
        db.session.commit()
        _stub_external(monkeypatch)
        out = _fresh_call(user.id)
    assert out["personalized"] is False
    assert out["items"] == []
    assert out["mode"] == "hedged"


def test_confidence_just_below_gate_not_personalized(app, user):
    """6 titles but confidence 0.39 → ineligible (threshold unchanged)."""
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.39, titles=6,
                                signals=20))
        db.session.commit()
        out = _fresh_call(user.id)
    assert out["personalized"] is False
    assert out["items"] == []


def test_empty_positive_evidence_follows_gate(app, user, monkeypatch):
    """A profile with empty genre weights but eligible counts stays governed
    by the canonical gate (titles+confidence) and must not crash."""
    with app.app_context():
        db.session.add(TasteProfile(
            user_id=user.id, genre_weights={}, decade_weights={},
            director_affinity={}, confidence=0.9, signal_count=20,
            distinct_title_count=6, profile_version=1))
        db.session.commit()
        _stub_external(monkeypatch)
        out = _fresh_call(user.id)
    assert out["personalized"] is True
    assert isinstance(out["items"], list)


# ════════════════════════════════════════════════════════════════════════════
# PART 5/6/7 — homepage: script/flag ordering (permanent-skeleton fix)
# ════════════════════════════════════════════════════════════════════════════

def test_auth_flag_is_defined_before_content_block():
    """base.html must define window.__IS_AUTH__ before {% block content %}
    so content-block scripts (for-you.js) see it at execution time."""
    with open("templates/base.html", encoding="utf-8") as f:
        src = f.read()
    assert src.index("window.__IS_AUTH__") < src.index("{% block content %}")


def test_for_you_js_runs_after_auth_flag_in_rendered_homepage(auth_client):
    """Rendered homepage contract: the flag appears in the document before
    js/for-you.js — the exact ordering whose absence left skeletons up."""
    html = auth_client.get("/").data.decode()
    assert "window.__IS_AUTH__" in html
    assert "js/for-you.js" in html
    assert html.index("window.__IS_AUTH__") < html.index("js/for-you.js")


def test_js_auth_gate_uses_strict_comparison():
    """The guard must be `__IS_AUTH__ !== true` (strict) so a defined-but-
    false flag and an undefined flag are both treated as anonymous — never
    as an accidental pass-through."""
    with open("static/js/for-you.js", encoding="utf-8") as f:
        src = f.read()
    assert "window.__IS_AUTH__ !== true" in src


# ════════════════════════════════════════════════════════════════════════════
# PART 4 — HTTP-layer cache isolation
# ════════════════════════════════════════════════════════════════════════════

def test_personalized_api_response_is_no_store(auth_client, monkeypatch):
    monkeypatch.setattr("api.for_you.get_for_you", lambda *a, **kw: {
        "personalized": True, "mode": "full", "confidence": 0.9,
        "reason_state": "learned_taste",
        "items": [{"tmdb_id": 1, "media_type": "movie", "title": "T",
                   "poster_path": None, "reason": {"kind": "trending",
                                                   "text": "Popular"}}]})
    r = auth_client.get("/api/for-you")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"


def test_cold_start_api_response_is_no_store(auth_client, monkeypatch):
    """A stale personalized payload must never be replayable against a
    now-cold user — BOTH shapes are uncacheable at the HTTP layer."""
    monkeypatch.setattr("api.for_you.get_for_you", lambda *a, **kw: {
        "personalized": False, "mode": "hedged", "confidence": 0.0,
        "reason_state": "few_signals", "items": []})
    r = auth_client.get("/api/for-you")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "no-store"
    assert r.get_json()["personalized"] is False


# ════════════════════════════════════════════════════════════════════════════
# PART 6/7/12 — hide/render contract (whole section, never skeletons+cards)
# ════════════════════════════════════════════════════════════════════════════

def _js_source():
    with open("static/js/for-you.js", encoding="utf-8") as f:
        return f.read()


def test_hide_removes_whole_section_not_just_cards():
    """personalized=false must remove the outer <section> — heading,
    subtitle, arrows, and skeletons all live inside it."""
    src = _js_source()
    assert "closest('section')" in src
    assert "removeChild(section)" in src


def test_render_clears_skeletons_before_appending_cards():
    """Loading skeletons are replaced in place — cards and skeletons can
    never coexist, and a rendered rail never keeps the placeholder state."""
    src = _js_source()
    assert "container.textContent = ''" in src
    assert "container.appendChild(frag)" in src


def test_render_hides_on_malformed_response():
    """Malformed payloads (missing flag/items, wrong types) → hide, via the
    same branch as cold start — never a partial render."""
    src = _js_source()
    assert "personalized !== true" in src
    assert "!Array.isArray(data.items)" in src
    assert "data.items.length === 0" in src


def test_any_fetch_failure_hides_section():
    """HTTP error, parse error, network failure → section removed (PART 7
    outcome C) — the placeholder can never outlive the request."""
    src = _js_source()
    assert ".catch(hide)" in src


def test_homepage_placeholder_wrapped_in_section(auth_client):
    """The skeleton placeholder's closest section must be the rails-loop
    <section> carrying the For You heading — i.e. hide() removes the whole
    block the screenshot showed."""
    html = auth_client.get("/").data.decode()
    marker = html.index("data-for-you-placeholder")
    section_open = html.rindex("<section", 0, marker)
    section_close = html.index("</section>", marker)
    between = html[section_open:section_close]
    assert "picked for your taste" in between
    assert "rail-arrow" in between
    # Skeletons live INSIDE the placeholder rail (after the attribute):
    assert 'class="loading' in html[marker:section_close]
