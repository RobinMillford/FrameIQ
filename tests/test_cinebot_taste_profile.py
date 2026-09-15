"""CineBot × canonical TasteProfile (Feature #6, Phase 13).

Covers the TasteProfile-driven taste context in src/api/agent_service.py:

- the deterministic [TASTE PROFILE] formatter (calibrated wording, bounded,
  cold-start safe, no IDs / raw JSON)
- _build_user_context() integration: exactly one get_profile() load,
  recent/current context preserved, no recomputation, no network
- source guards: no RecommendationFeedback reads, no Counter-based taste
  algorithm, no writes (profile snapshots cannot leak into chat memory)
- the graph-state flow still carries the enriched context, existing tools
  and the async streaming architecture remain untouched
"""
import ast
import socket
import uuid

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def ctx_user(app, db):
    """A unique user to keep DB state hermetic across the shared test DB."""
    from models import User, db as _db

    uid_box = []

    with app.app_context():
        u = User(username="cbuser_" + uuid.uuid4().hex[:8],
                 email="cb_%s@example.com" % uuid.uuid4().hex[:6],
                 password_hash="x")
        _db.session.add(u)
        _db.session.commit()
        uid_box.append(u.id)
    yield uid_box[0]

    # Tear down dependents first (FK order matters on SQLite).
    with app.app_context():
        from models import (Review, TVShowProgress, WatchProgress,
                            UserChatMemory, TasteProfile, user_watchlist,
                            User)
        uid = uid_box[0]
        _db.session.execute(user_watchlist.delete().where(
            user_watchlist.c.user_id == uid))
        Review.query.filter_by(user_id=uid).delete()
        TVShowProgress.query.filter_by(user_id=uid).delete()
        WatchProgress.query.filter_by(user_id=uid).delete()
        UserChatMemory.query.filter_by(user_id=uid).delete()
        TasteProfile.query.filter_by(user_id=uid).delete()
        u = _db.session.get(User, uid)
        if u:
            _db.session.delete(u)
        _db.session.commit()


def _add_profile(uid, *, genres=None, decades=None, directors=None,
                 runtime=None, media_pref=None, confidence=0.7,
                 titles=10, signals=20):
    from models import TasteProfile, db as _db

    p = TasteProfile(user_id=uid)
    p.genre_weights = genres or {}
    p.decade_weights = decades or {}
    p.director_affinity = directors or {}
    p.runtime_pref = runtime or {}
    p.media_type_pref = media_pref or {}
    p.confidence = confidence
    p.distinct_title_count = titles
    p.signal_count = signals
    _db.session.add(p)
    _db.session.commit()
    return p


@pytest.fixture
def ban_network(monkeypatch):
    """Any socket connect during a test fails loudly."""

    class _Blocked(socket.socket):
        def __init__(self, *a, **k):
            raise AssertionError("network access attempted during test")

    monkeypatch.setattr(socket, "socket", _Blocked)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("network access attempted")))


# ── Formatter: deterministic, bounded, calibrated ────────────────────────────

def _rich_summary_dict():
    return {
        "genre_weights": {"Thriller": 0.9, "Drama": 0.4, "Horror": -0.6},
        "decade_weights": {"2010s": 0.8, "1990s": 0.3},
        "director_affinity": {"Denis Villeneuve": 0.7, "Greta Gerwig": 0.4},
        "runtime_pref": {"p25": 100.0, "p75": 150.0, "sample_count": 8},
        "media_type_pref": {"movie": 0.8, "tv": 0.2},
        "confidence": 0.8,
        "distinct_title_count": 12,
        "signal_count": 30,
        "profile_version": 1,
    }


def test_cold_start_none_profile_renders_nothing():
    from src.api.agent_service import _format_taste_profile

    assert _format_taste_profile(None) is None


def test_empty_profile_renders_nothing(ctx_user):
    """A profile with no positive genres must produce no taste claims."""
    from src.api.agent_service import _format_taste_profile
    from api.taste_profile import get_profile

    _add_profile(ctx_user, genres={"Horror": -0.5}, confidence=0.0,
                 titles=0, signals=0)
    assert _format_taste_profile(get_profile(ctx_user)) is None


def test_formatter_renders_positive_genres():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "thriller" in block.lower() and "drama" in block.lower()


def test_formatter_renders_negative_genres_calibrated():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "horror" in block.lower()
    assert "steer away from" in block
    # Weak negative evidence must not sound absolute.
    assert "hates" not in block.lower()
    assert "never watches" not in block.lower()


def test_formatter_renders_directors():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "Denis Villeneuve" in block


def test_formatter_absent_directors_is_graceful():
    from src.api.agent_service import _format_taste_profile

    d = _rich_summary_dict()
    d["director_affinity"] = {}
    block = _format_taste_profile(d)
    assert "director" not in block.lower()


def test_formatter_renders_decades_runtime_media_pref():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "2010s" in block
    assert "100-150 min" in block
    assert "movie (80%)" in block and "tv (20%)" in block


def test_formatter_high_confidence_strong_wording():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "strong" in block
    assert "hints" not in block


def test_formatter_low_confidence_hedged():
    from src.api.agent_service import _format_taste_profile

    d = _rich_summary_dict()
    d["confidence"] = 0.3
    d["distinct_title_count"] = 3
    block = _format_taste_profile(d)
    assert "limited evidence" in block and "hints" in block
    assert "confidence: strong" not in block


def test_formatter_delimited_section():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert block.startswith("[TASTE PROFILE]")
    assert block.rstrip().endswith("[/TASTE PROFILE]")


def test_formatter_deterministic():
    from src.api.agent_service import _format_taste_profile

    assert (_format_taste_profile(_rich_summary_dict())
            == _format_taste_profile(_rich_summary_dict()))


def test_formatter_bounded():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert len(block) < 1200


def test_formatter_exposes_no_ids():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "user_id" not in block and "profile" not in block.replace(
        "[TASTE PROFILE]", "").replace("taste profile", "")
    assert "version" not in block  # profile_version is internal


def test_formatter_no_raw_json_dump():
    from src.api.agent_service import _format_taste_profile

    block = _format_taste_profile(_rich_summary_dict())
    assert "{" not in block and "}" not in block and '"' not in block


# ── Context integration ───────────────────────────────────────────────────────

def test_context_includes_persisted_profile(ctx_user):
    from src.api.agent_service import _build_user_context

    _add_profile(ctx_user, genres={"Thriller": 0.9}, confidence=0.8,
                 titles=12, signals=30)
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "[TASTE PROFILE]" in ctx and "[/TASTE PROFILE]" in ctx
    assert "thriller" in ctx.lower()


def test_missing_profile_does_not_crash(ctx_user):
    from src.api.agent_service import _build_user_context

    ctx = _build_user_context(f"user_{ctx_user}")
    assert "[TASTE PROFILE]" not in ctx


def test_profile_loaded_exactly_once(ctx_user, monkeypatch):
    import api.taste_profile as tp
    from src.api.agent_service import _build_user_context

    _add_profile(ctx_user, genres={"Thriller": 0.9}, confidence=0.8,
                 titles=12, signals=30)
    calls = []
    real = tp.get_profile

    def counting(uid, create=False):
        calls.append(uid)
        return real(uid, create=create)

    monkeypatch.setattr(tp, "get_profile", counting)
    _build_user_context(f"user_{ctx_user}")
    assert calls.count(ctx_user) == 1


def test_no_profile_query_for_anonymous(monkeypatch):
    import api.taste_profile as tp
    from src.api.agent_service import _build_user_context

    def boom(*a, **k):
        raise AssertionError("anonymous context must not query TasteProfile")

    monkeypatch.setattr(tp, "get_profile", boom)
    assert _build_user_context("session_anon_123") == ""
    assert _build_user_context("not-a-user-session") == ""


def test_recent_ratings_remain(ctx_user):
    from models import MediaItem, Review, db as _db
    from src.api.agent_service import _build_user_context

    m = MediaItem(tmdb_id=550, media_type="movie", title="Fight Club")
    _db.session.add(m)
    _db.session.flush()
    _db.session.add(Review(user_id=ctx_user, media_id=m.id,
                           media_type="movie", rating=4.5))
    _db.session.commit()
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "Recent ratings" in ctx and "Fight Club" in ctx


def test_tv_tracking_remains(ctx_user):
    from models import TVShowProgress, db as _db
    from src.api.agent_service import _build_user_context

    _db.session.add(TVShowProgress(user_id=ctx_user, show_id=1399,
                                   total_episodes=10, watched_episodes=4,
                                   status="watching"))
    _db.session.commit()
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "TV shows they are tracking" in ctx and "1399" in ctx


def test_chat_memory_remains(ctx_user):
    from models import UserChatMemory, db as _db
    from src.api.agent_service import _build_user_context

    _db.session.add(UserChatMemory(user_id=ctx_user,
                                   content="Loves slow-burn sci-fi"))
    _db.session.commit()
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "remembered from chat" in ctx.lower()
    assert "slow-burn sci-fi" in ctx


def test_watch_progress_remains(ctx_user):
    from models import WatchProgress, db as _db
    from src.api.agent_service import _build_user_context

    wp = WatchProgress(user_id=ctx_user, tmdb_id=155, media_type="movie",
                       title="The Dark Knight", current_time=3000,
                       duration=9000)
    _db.session.add(wp)
    _db.session.commit()
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "Recently streamed" in ctx and "The Dark Knight" in ctx


def test_watchlist_count_remains(ctx_user):
    from models import MediaItem, user_watchlist, db as _db
    from src.api.agent_service import _build_user_context

    m = MediaItem(tmdb_id=27205, media_type="movie", title="Inception")
    _db.session.add(m)
    _db.session.flush()
    _db.session.execute(user_watchlist.insert().values(
        user_id=ctx_user, media_id=m.id, media_type="movie"))
    _db.session.commit()
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "Watchlist: 1 titles saved." in ctx


def test_no_compute_profile_call(ctx_user, monkeypatch):
    """Request-path guarantee: profile is read, never recomputed."""
    import api.taste_profile as tp
    from src.api.agent_service import _build_user_context

    def boom(*a, **k):
        raise AssertionError("compute_profile must never run in a request")

    monkeypatch.setattr(tp, "compute_profile", boom)
    _add_profile(ctx_user, genres={"Thriller": 0.9})
    _build_user_context(f"user_{ctx_user}")          # profile present
    _build_user_context(f"user_{ctx_user}")          # and absent path too
    from models import TasteProfile, db as _db
    TasteProfile.query.filter_by(user_id=ctx_user).delete()
    _db.session.commit()
    _build_user_context(f"user_{ctx_user}")


def test_no_network_for_context(ctx_user, ban_network):
    from src.api.agent_service import _build_user_context

    _add_profile(ctx_user, genres={"Thriller": 0.9}, confidence=0.8,
                 titles=12, signals=30)
    ctx = _build_user_context(f"user_{ctx_user}")
    assert "[TASTE PROFILE]" in ctx


def test_context_never_touches_tmdb(ctx_user, monkeypatch):
    import api.tmdb.cache as cache
    from src.api.agent_service import _build_user_context

    def boom(*a, **k):
        raise AssertionError("TMDb called while building CineBot context")

    monkeypatch.setattr(cache, "cached_tmdb_request", boom)
    _add_profile(ctx_user, genres={"Thriller": 0.9})
    _build_user_context(f"user_{ctx_user}")


# ── Source guards ─────────────────────────────────────────────────────────────

def _source_tree():
    """Parsed AST of agent_service.py (guards ignore comments/docstrings)."""
    with open("src/api/agent_service.py", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _source():
    with open("src/api/agent_service.py", encoding="utf-8") as fh:
        return fh.read()


def test_source_guard_no_feedback_reads():
    """No RecommendationFeedback usage anywhere in the module (AST-level,
    so descriptive docstrings cannot false-positive)."""
    names = {n.id for n in ast.walk(_source_tree())
             if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(_source_tree())
             if isinstance(n, ast.Attribute)}
    assert "RecommendationFeedback" not in names | attrs


def test_source_guard_no_counter_taste_algorithm():
    """The Phase 13 duplicate long-term genre aggregation is gone."""
    calls = {n.func.id for n in ast.walk(_source_tree())
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "Counter" not in calls


def test_source_guard_no_profile_recomputation():
    src = _source()
    assert "compute_profile" not in src
    assert "create=True" not in src


def test_source_guard_module_writes_nothing():
    """No commits/writes in agent_service: taste data cannot be persisted
    into chat memory or anywhere else from this module."""
    src = _source()
    assert "session.commit" not in src
    assert "db.session.commit" not in src


def test_source_guard_no_async_sqlite_in_context_module():
    """Chat checkpointing stays in src/agents/graph.py, not here."""
    src = _source()
    assert "AsyncSqliteSaver" not in src
    assert "checkpoint" not in src.lower()


# ── State flow, tools, streaming architecture ─────────────────────────────────

def test_initial_state_carries_taste_context(ctx_user):
    from src.api.agent_service import _build_initial_state

    _add_profile(ctx_user, genres={"Thriller": 0.9}, confidence=0.8,
                 titles=12, signals=30)
    state = _build_initial_state("what should I watch?", f"user_{ctx_user}")
    assert "[TASTE PROFILE]" in state["user_context"]
    assert state["user_id"] == ctx_user


def test_initial_state_cold_start_has_no_taste_claims(ctx_user):
    from src.api.agent_service import _build_initial_state

    state = _build_initial_state("hello", f"user_{ctx_user}")
    assert "[TASTE PROFILE]" not in state["user_context"]


def test_existing_tools_unchanged():
    from src.agents.tools import RETRIEVER_TOOLS

    names = {t.name for t in RETRIEVER_TOOLS}
    assert {"discover_movies", "discover_tv", "get_similar_movies",
            "my_history"} <= names


def test_streaming_architecture_intact():
    """The async chat/streaming paths this phase must not disturb."""
    with open("routes/chat.py", encoding="utf-8") as fh:
        chat_src = fh.read()
    assert "astream_events" in chat_src
    with open("src/agents/graph.py", encoding="utf-8") as fh:
        graph_src = fh.read()
    assert "AsyncSqliteSaver" in graph_src


def test_nodes_still_inject_user_context():
    """The enriched context must still reach the system prompts."""
    with open("src/agents/nodes.py", encoding="utf-8") as fh:
        nodes_src = fh.read()
    assert 'state.get("user_context")' in nodes_src
