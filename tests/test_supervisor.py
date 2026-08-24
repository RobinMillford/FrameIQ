"""
P2 tests — LLM supervisor routing, entity extraction, fallback, my_history.

Supervisor LLM is mocked; no network or OpenAI key needed.
"""
import pytest
from unittest.mock import patch, MagicMock
from langchain_core.messages import HumanMessage

from src.agents.nodes import (
    supervisor_node, _heuristic_route, _RouteDecision,
)
from src.agents.tools import _query_user_history
from src.api.agent_service import _build_initial_state


def _decision(route="retriever", intent="recommend", titles=None,
              people=None, genres=None, years=None):
    m = MagicMock(spec=_RouteDecision)
    m.route = route
    m.intent = intent
    m.titles = titles or []
    m.people = people or []
    m.genres = genres or []
    m.years = years or []
    return m


def _state(text, **kwargs):
    base = {
        "messages": [HumanMessage(content=text)],
        "user_intent": None, "next_step": None, "entities": {},
        "user_id": 1, "retrieved_context": [],
        "final_response_metadata": {"movies": [], "tv_shows": []},
        "user_context": "",
    }
    base.update(kwargs)
    return base


class TestLLMSupervisor:
    def test_retriever_route_with_entities(self):
        decision = _decision(
            route="retriever", intent="recommend",
            genres=["horror"], years=["2024"],
        )
        with patch("src.agents.nodes._get_supervisor_chain") as g:
            g.return_value.invoke.return_value = decision
            out = supervisor_node(_state("suggest latest horror movies 2024"))
        assert out["next_step"] == "retriever"
        assert out["user_intent"] == "recommend"
        assert out["entities"]["genres"] == ["horror"]
        assert out["entities"]["years"] == ["2024"]

    def test_greeting_route(self):
        decision = _decision(route="greeting", intent="greeting")
        with patch("src.agents.nodes._get_supervisor_chain") as g:
            g.return_value.invoke.return_value = decision
            out = supervisor_node(_state("hey there, what's up"))
        assert out["next_step"] == "chat"
        assert out["user_intent"] == "greeting"

    def test_fast_path_greeting_no_llm(self):
        with patch("src.agents.nodes._get_supervisor_chain") as g:
            out = supervisor_node(_state("hi"))
            g.assert_not_called()
        assert out["next_step"] == "chat"
        assert out["user_intent"] == "greeting"

    def test_llm_failure_falls_back_to_heuristic(self):
        with patch("src.agents.nodes._get_supervisor_chain") as g:
            g.return_value.invoke.side_effect = RuntimeError("api down")
            out = supervisor_node(_state("suggest some horror movies"))
        # heuristic: not a greeting/starter -> retriever
        assert out["next_step"] == "retriever"
        assert out["entities"] == {}

    def test_heuristic_chat_starter_on_fallback(self):
        with patch("src.agents.nodes._get_supervisor_chain") as g:
            g.return_value.invoke.side_effect = RuntimeError("api down")
            out = supervisor_node(_state("what is method acting"))
        assert out["next_step"] == "chat"


class TestHeuristicRouter:
    @pytest.mark.parametrize("text,expected", [
        ("hi", ("greeting", "greeting")),
        ("thank you", ("greeting", "greeting")),
        ("what is method acting", ("chat", "explain")),
        ("explain the french new wave", ("chat", "explain")),
        ("suggest latest horror movies", ("retriever", "search")),
        ("movies like inception", ("retriever", "search")),
    ])
    def test_routes(self, text, expected):
        assert _heuristic_route(text) == expected


class TestInitialState:
    def test_user_id_extracted(self):
        st = _build_initial_state("hello", "user_42")
        assert st["user_id"] == 42
        assert st["entities"] == {}

    def test_anonymous_user_id_none(self):
        st = _build_initial_state("hello", "anon_session")
        assert st["user_id"] is None


class TestMyHistoryTool:
    def test_no_user_id_returns_error(self):
        from src.agents.tools import _CURRENT_USER_ID
        token = _CURRENT_USER_ID.set(None)
        try:
            from src.agents.tools import my_history
            result = my_history.invoke({"query": "ratings"})
            assert "error" in result
        finally:
            _CURRENT_USER_ID.reset(token)

    def test_query_user_history_shape(self, app, db, sample_user):
        from models import Review, MediaItem, User
        with app.app_context():
            uid = User.query.filter_by(username='testuser').first().id
            m = MediaItem(tmdb_id=777, media_type="movie", title="Test Film",
                          rating=8.0, genres="Action,Thriller")
            db.session.add(m)
            db.session.flush()
            db.session.add(Review(
                user_id=uid, media_id=m.id, media_type="movie",
                rating=4.5,
            ))
            db.session.commit()

            result = _query_user_history(uid, "everything")
            assert "recent_ratings" in result
            assert result["recent_ratings"][0]["title"] == "Test Film"
            assert result["recent_ratings"][0]["rating_10"] == 9.0
            assert "favorite_genres" in result

            # clean up my rows so sample_user teardown doesn't hit NOT NULL
            db.session.delete(Review.query.filter_by(user_id=uid).first())
            db.session.delete(m)
            db.session.commit()

    def test_query_user_history_watchlist_only(self, app, db, sample_user):
        from models import MediaItem, user_watchlist, User
        with app.app_context():
            uid = User.query.filter_by(username='testuser').first().id
            m = MediaItem(tmdb_id=888, media_type="tv", title="Wish Show")
            db.session.add(m)
            db.session.flush()
            db.session.execute(user_watchlist.insert().values(
                user_id=uid, media_id=m.id,
                media_type="tv", priority="high",
            ))
            db.session.commit()

            result = _query_user_history(uid, "my watchlist")
            assert "watchlist" in result
            assert result["watchlist"][0]["title"] == "Wish Show"
            assert result["watchlist"][0]["priority"] == "high"
            # trimmed sections absent for a focused query
            assert "diary" not in result

            # clean up my rows so sample_user teardown doesn't hit NOT NULL
            db.session.execute(user_watchlist.delete().where(
                user_watchlist.c.user_id == uid))
            db.session.delete(m)
            db.session.commit()
