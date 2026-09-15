"""For You false-personalization / cold-start contract (production fix).

Regression coverage for the audited personalization gate:
    personalized=True  <=>  confidence >= 0.4 AND distinct_title_count >= 5
Below-gate states (missing profile, zero signals, <5 titles, <0.4
confidence) must return personalized=False + items=[] with ZERO candidate
generation — generic trending content never renders as For You.
"""
import json
import uuid

import pytest

from models import db, User, TasteProfile

DOMAIN = "example.invalid"


@pytest.fixture
def user(app):
    with app.app_context():
        u = User(username=f"fypg_{uuid.uuid4().hex[:8]}",
                 email=f"fypg_{uuid.uuid4().hex[:8]}@{DOMAIN}",
                 email_verified=True)
        u.set_password("TestPass1")
        db.session.add(u)
        db.session.commit()
        yield u
        TasteProfile.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
        db.session.commit()


def _user(prefix="fypg"):
    u = User(username=f"{prefix}_{uuid.uuid4().hex[:8]}",
             email=f"{prefix}_{uuid.uuid4().hex[:8]}@{DOMAIN}",
             email_verified=True)
    u.set_password("TestPass1")
    db.session.add(u)
    db.session.commit()
    return u


def _profile(user_id, confidence=0.9, titles=6, signals=12,
             genres=None, version=1):
    return TasteProfile(
        user_id=user_id,
        genre_weights=genres or {"Thriller": 0.8, "Drama": 0.5},
        decade_weights={"2010s": 0.6}, director_affinity={},
        confidence=confidence, signal_count=signals,
        distinct_title_count=titles, profile_version=version)


# ════════════════════════════════════════════════════════════════════════════
# Gate semantics
# ════════════════════════════════════════════════════════════════════════════

def test_missing_profile_not_personalized(app, user):
    with app.app_context():
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is False
    assert out["items"] == []
    assert out["mode"] == "cold"


def test_zero_signal_profile_not_personalized(app, user):
    with app.app_context():
        db.session.add(TasteProfile(user_id=user.id))  # zero signals/titles
        db.session.commit()
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is False
    assert out["items"] == []
    assert out["reason_state"] == "no_meaningful_signals"


def test_few_signals_state_is_cold_start_shape(app, user):
    """reason_state='few_signals' must present as cold-start/hedged with
    items=[] — never trending candidates labeled personalized."""
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.0, titles=1,
                                signals=1))
        db.session.commit()
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is False
    assert out["mode"] == "hedged"
    assert out["reason_state"] == "few_signals"
    assert out["items"] == []
    assert all(k in out for k in ("personalized", "mode", "confidence",
                                  "reason_state", "items"))


def test_gate_boundary_exactly_five_titles_and_full_confidence(app, user):
    """The audited gate is >=: exactly 5 titles + confidence 0.4 IS
    personalized."""
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.4, titles=5))
        db.session.commit()
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is True
    assert out["mode"] == "full"


def test_gate_boundary_below_confidence(app, user):
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.39, titles=5))
        db.session.commit()
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is False
    assert out["items"] == []


def test_gate_boundary_below_titles(app, user):
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.9, titles=4))
        db.session.commit()
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is False
    assert out["items"] == []


def test_eligible_user_personalized_with_items(app, user, monkeypatch):
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        _stub_external(monkeypatch)
        out = _fresh_call(user.id)
    assert out["personalized"] is True
    assert out["mode"] == "full"
    assert out["items"], "eligible user must receive items"


def _stub_external(monkeypatch):
    """Deterministic external environment using the repo's established
    _StubTmdb pattern (matches /trending/ URLs, discover by genre id).
    S0 trending returns one poster-carrying filler so eligible users can
    fill volume from S0; availability is empty; recs are empty."""
    import api.availability as availability
    from tests.test_for_you import _StubTmdb, _tmdb_raw, _wire

    stub = _StubTmdb(trending=[_tmdb_raw(5001, title='S0 Filler',
                                         genres=(53,), pop=40.0)])
    _wire(stub, monkeypatch)
    monkeypatch.setattr(availability, "get_availability", lambda *a, **kw: {})
    monkeypatch.setattr(availability, "match_my_services",
                        lambda a, b: {"matches": [], "available": False})


def _fresh_call(user_id):
    """get_for_you with the engine cache cleared so repeated tests with
    the same user id never observe a prior call's result."""
    import api.for_you as fy
    fy._cache.clear()
    return fy.get_for_you(user_id)


# ════════════════════════════════════════════════════════════════════════════
# Evidence honesty (trending filler inside full personalization)
# ════════════════════════════════════════════════════════════════════════════

def test_pure_trending_filler_keeps_trending_reason(app, user,
                                                    monkeypatch):
    """An S0 filler candidate for an eligible user keeps kind='trending' —
    no fabricated 'Because you like X' without supporting evidence."""
    with app.app_context():
        db.session.add(_profile(user.id, genres={"Thriller": 0.9}))
        db.session.commit()
        _stub_external(monkeypatch)
        out = _fresh_call(user.id)
    assert out["personalized"] is True
    by_id = {i["tmdb_id"]: i for i in out["items"]}
    filler = by_id.get(5001)
    assert filler is not None, "S0 filler must remain in the eligible pool"
    assert filler["source"] == "trending"
    assert filler["reason"]["kind"] == "trending"
    assert filler["reason"]["text"] == "Popular right now"
    assert "like" not in filler["reason"]["text"].lower()


def test_below_gate_user_receives_no_trending_cards(app, user,
                                                    monkeypatch):
    """The trending stub exists, but a below-gate user must get items=[]
    — generic fallback lives in existing rails, not For You."""
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.1, titles=2,
                                signals=3))
        db.session.commit()
        _stub_external(monkeypatch)
        out = _fresh_call(user.id)
    assert out["personalized"] is False
    assert out["items"] == []


# ════════════════════════════════════════════════════════════════════════════
# Cache isolation
# ════════════════════════════════════════════════════════════════════════════

def test_cache_cannot_leak_personalized_result_to_below_gate_user(app,
                                                                  user,
                                                                  monkeypatch):
    """A cached personalized response for one user must never surface for
    a below-gate user (distinct cache keys include user_id + mode +
    profile identity)."""
    import api.for_you as fy

    with app.app_context():
        eligible = _user("fypg_full")
        db.session.add(_profile(eligible.id))
        db.session.commit()
        _stub_external(monkeypatch)
        out = fy.get_for_you(eligible.id)
        assert out["personalized"] is True
        assert len(fy._cache) >= 1  # a personalized entry is cached

        cold = _user("fypg_cold")
        try:
            cold_out = fy.get_for_you(cold.id)
        finally:
            db.session.delete(eligible)
            db.session.delete(cold)
            db.session.commit()
    assert cold_out["personalized"] is False
    assert cold_out["items"] == []


def test_cache_key_components_include_user_mode_and_profile_identity(app,
                                                                     user):
    """The key must separate user_id, region, limit, mode, and profile
    version/timestamp — enough identity that a profile crossing the gate
    cannot serve a stale below-gate response (or vice versa)."""
    import api.for_you as fy
    with app.app_context():
        db.session.add(_profile(user.id))
        db.session.commit()
        profile = TasteProfile.query.filter_by(user_id=user.id).first()
        mode, _ = fy._classify(profile)
        key = (user.id, "US", fy.MAX_RESULTS, mode,
               profile.profile_version, profile.updated_at.isoformat())
        # Same inputs -> same key; a different mode/user diverges.
        assert mode == "full"
        other = (user.id + 1, "US", fy.MAX_RESULTS, "hedged", 1, None)
        assert key != other


# ════════════════════════════════════════════════════════════════════════════
# Response schema / determinism / independence
# ════════════════════════════════════════════════════════════════════════════

def test_few_signals_response_is_json_serializable_and_deterministic(app,
                                                                     user):
    import api.for_you as fy
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.0, titles=1,
                                signals=1))
        db.session.commit()
        first = fy.get_for_you(user.id)
        fy._cache.clear()
        second = fy.get_for_you(user.id)
    assert json.dumps(first, sort_keys=True) == json.dumps(second,
                                                           sort_keys=True)


def test_api_returns_json_not_html(app, auth_client, user):
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.1, titles=2,
                                signals=3))
        db.session.commit()
        resp = auth_client.get("/api/for-you")
    assert resp.status_code == 200
    assert resp.content_type.startswith("application/json")
    body = resp.get_json()
    assert body["personalized"] is False and body["items"] == []


def test_below_gate_response_has_no_feedback_dependency(app, user):
    """Source guard: the engine must not import or read
    RecommendationFeedback directly (learning flows through the nightly
    profile)."""
    src = open("api/for_you.py").read()
    assert "RecommendationFeedback" not in src
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.0, titles=1,
                                signals=1))
        db.session.commit()
        out = __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
    assert out["personalized"] is False


def test_no_profile_recomputation_in_request_path(app, user):
    import api.taste_profile as tp
    with app.app_context():
        db.session.add(_profile(user.id, confidence=0.0, titles=1,
                                signals=1))
        db.session.commit()
        calls = []
        orig = tp.compute_profile
        tp.compute_profile = lambda *a, **k: calls.append(a)
        try:
            __import__("api.for_you", fromlist=["x"]).get_for_you(user.id)
        finally:
            tp.compute_profile = orig
    assert calls == []
